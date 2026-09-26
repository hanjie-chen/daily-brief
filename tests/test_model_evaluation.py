import json

import pytest

from daily_brief.llm.model_evaluation import (
    ModelEvaluationInputError,
    capture_model_evaluation_input,
    load_model_evaluation_input,
    run_model_evaluation,
)
from daily_brief.llm.summarizer import build_summary_prompt
from daily_brief.models import Candidate, Story


def candidate(item_id: str, title: str, *, fetched_text: str = "") -> Candidate:
    return Candidate(
        story=Story(
            source="test",
            hn_item_id=item_id,
            title=title,
            source_url=f"https://example.com/{item_id}",
            hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
            created_at="2026-07-20T00:00:00Z",
            points=10,
            comments=2,
            story_text="公开的 Story 文本",
            fetched_text=fetched_text,
        )
    )


def community_roundup(
    item_id: str, title: str = "What are you working on?"
) -> Candidate:
    item = candidate(item_id, title)
    item.story = Story(
        source=item.story.source,
        hn_item_id=item.story.hn_item_id,
        title=item.story.title,
        source_url=item.story.hn_discussion_url,
        hn_discussion_url=item.story.hn_discussion_url,
        created_at=item.story.created_at,
        points=item.story.points,
        comments=item.story.comments,
        story_text="What are you working on?",
    )
    item.content_kind = "community_roundup"
    item.summary_basis = "hn_comments"
    item.discussion_text = "A developer shares a useful project."
    return item


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.classifier_ids = []
        self.summary_ids = []

    def classify(self, candidates):
        self.classifier_ids.extend(item.story.hn_item_id for item in candidates)
        return {
            item.story.hn_item_id: (
                "ai" if item.story.hn_item_id == "1" else "core_non_ai"
            )
            for item in candidates
        }

    def summarize(self, item):
        self.summary_ids.append(item.story.hn_item_id)
        return f"中文摘要：{item.story.title}"


class PartiallyFailingBackend(FakeBackend):
    def classify(self, candidates):
        raise RuntimeError("classifier unavailable")

    def summarize(self, item):
        if item.story.hn_item_id == "2":
            raise RuntimeError("summary unavailable")
        return super().summarize(item)


class MixedScriptBackend(FakeBackend):
    def summarize(self, item):
        return "  Anthropic发布Claude 5模型。\n"


def test_capture_and_load_preserve_exact_unicode_model_inputs(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [[candidate("1", "分类标题", fetched_text="分类正文")]],
        [candidate("2", "摘要标题", fetched_text="抓取的中文与 English 正文")],
    )

    loaded = load_model_evaluation_input(input_path)

    assert loaded.date_label == "2026-07-20"
    assert loaded.exploration_classification_batches[0][0].story.title == "分类标题"
    assert (
        loaded.exploration_classification_batches[0][0].story.fetched_text == "分类正文"
    )
    assert (
        loaded.summary_candidates[0].story.fetched_text == "抓取的中文与 English 正文"
    )


def test_hn_discussion_prompt_is_identical_after_capture_and_replay(tmp_path):
    original = candidate("2", "付费文章")
    original.summary_basis = "hn_comments"
    original.discussion_text = "[评论 1]\n一种观点。\n\n[评论 2]\n另一种观点。"
    input_path = tmp_path / "input.json"

    capture_model_evaluation_input(input_path, "2026-07-20", [], [original])
    replayed = load_model_evaluation_input(input_path).summary_candidates[0]

    assert replayed.summary_basis == "hn_comments"
    assert replayed.discussion_text == original.discussion_text
    assert build_summary_prompt(replayed) == build_summary_prompt(original)


def test_research_prompt_is_identical_after_capture_and_replay(tmp_path):
    body = """Abstract
This study links enterprise account records to worker roles and financial data across more than 1,500 organizations, while distinguishing descriptive associations from causal effects.
1 Introduction
Background and related literature.
2 Methods
Detailed sample construction.
3 Results
Output tokens increased sevenfold, while an existing cohort increased fourfold. Adoption was concentrated among larger and more R&D-intensive firms, and early-career workers used the product more intensively.
4 Conclusion
The analysis covers only Enterprise accounts and does not measure downstream productivity or establish that adoption caused stronger financial outcomes.
References
A bibliography.
"""
    original = Candidate(
        story=Story(
            source="test",
            hn_item_id="1",
            title="How organizations use AI [pdf]",
            source_url="https://example.com/report.pdf",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-20T00:00:00Z",
            points=10,
            comments=2,
            fetched_text=body,
        )
    )
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], [original])

    replayed = load_model_evaluation_input(input_path).summary_candidates[0]

    assert build_summary_prompt(replayed) == build_summary_prompt(original)
    assert "[Summary mode: research_report]" in build_summary_prompt(replayed)


def test_evaluation_replays_one_input_without_touching_state_files(tmp_path):
    data_dir = tmp_path / "data"
    input_path = data_dir / "model-eval-inputs/2026-07-20.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [
            [candidate("1", "AI tool", fetched_text="AI article evidence")],
            [candidate("2", "Database", fetched_text="Database article evidence")],
        ],
        [candidate("1", "AI tool"), candidate("2", "Database")],
    )
    original_input = input_path.read_bytes()
    history_path = data_dir / "recommendation-history.json"
    publish_state_path = data_dir / "publish-state.json"
    history_path.write_text('{"sentinel":"history"}', encoding="utf-8")
    publish_state_path.write_text('{"sentinel":"publish"}', encoding="utf-8")
    backend = FakeBackend()

    result = run_model_evaluation(
        input_path,
        data_dir / "model-evaluations",
        backend,
        clock=iter([1.0, 1.5, 2.0, 2.25, 3.0, 3.75, 4.0, 4.5]).__next__,
        evaluated_at="2026-07-20T09:00:00+08:00",
    )

    assert result.failures == 0
    assert backend.classifier_ids == ["1", "2"]
    assert backend.summary_ids == ["1", "2"]
    assert input_path.read_bytes() == original_input
    assert history_path.read_text(encoding="utf-8") == '{"sentinel":"history"}'
    assert publish_state_path.read_text(encoding="utf-8") == '{"sentinel":"publish"}'
    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["backend"] == "fake"
    assert payload["evaluated_at"] == "2026-07-20T09:00:00+08:00"
    assert payload["exploration_classifications"] == [
        {
            "item_ids": ["1"],
            "status": "success",
            "duration_seconds": 0.5,
            "decisions": [{"id": "1", "label": "ai"}],
            "error": "",
        },
        {
            "item_ids": ["2"],
            "status": "success",
            "duration_seconds": 0.25,
            "decisions": [{"id": "2", "label": "core_non_ai"}],
            "error": "",
        },
    ]
    assert [item["summary"] for item in payload["summaries"]] == [
        "中文摘要：AI tool",
        "中文摘要：Database",
    ]


def test_evaluation_records_partial_failures_and_continues(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [[candidate("1", "AI tool", fetched_text="AI article evidence")]],
        [candidate("1", "AI tool"), candidate("2", "Database")],
    )

    result = run_model_evaluation(
        input_path,
        tmp_path / "results",
        PartiallyFailingBackend(),
        clock=iter([1.0, 1.1, 2.0, 2.2, 3.0, 3.3]).__next__,
    )

    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert result.failures == 2
    assert payload["exploration_classifications"][0]["status"] == "failed"
    assert payload["summaries"][0]["status"] == "success"
    assert payload["summaries"][1]["status"] == "failed"
    assert payload["summaries"][1]["summary"] == ""


def test_evaluation_normalizes_summary_before_writing_artifact(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [],
        [candidate("1", "AI tool")],
    )

    result = run_model_evaluation(
        input_path,
        tmp_path / "results",
        MixedScriptBackend(),
        clock=iter([1.0, 1.1, 2.0, 2.2]).__next__,
    )

    payload = json.loads(result.output_path.read_text(encoding="utf-8"))
    assert payload["summaries"][0]["summary"] == "Anthropic 发布 Claude 5 模型。"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": 3},
        {
            "schema_version": 1,
            "date": "20-07-2026",
            "exploration_classification_batches": [],
            "summary_candidates": [],
        },
    ],
)
def test_load_rejects_invalid_schema(tmp_path, payload):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelEvaluationInputError):
        load_model_evaluation_input(input_path)


def test_load_rejects_discussion_text_without_matching_summary_basis(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [],
        [candidate("1", "AI tool")],
    )
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    payload["summary_candidates"][0]["discussion_text"] = "Unexpected comments"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ModelEvaluationInputError,
        match="discussion_text must match summary_basis",
    ):
        load_model_evaluation_input(input_path)


def test_source_and_discussion_attempts_replay_independently(tmp_path):
    from copy import deepcopy
    from daily_brief.llm.summarizer import InsufficientSummaryMaterial

    source = candidate("1", "Interactive game", fetched_text="Click to start")
    source.summary_basis = "fetched_article"
    discussion = deepcopy(source)
    discussion.summary_basis = "hn_comments"
    discussion.discussion_text = "Readers describe the game's theme."
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], [source, discussion])
    original_bytes = input_path.read_bytes()
    loaded = load_model_evaluation_input(input_path)
    assert [build_summary_prompt(item) for item in loaded.summary_candidates] == [
        build_summary_prompt(source),
        build_summary_prompt(discussion),
    ]

    class EvidenceBackend(FakeBackend):
        def summarize(self, item):
            self.summary_ids.append((item.story.hn_item_id, item.summary_basis))
            if item.summary_basis != "hn_comments":
                raise InsufficientSummaryMaterial("Only an opening instruction")
            return "读者讨论了游戏的主题。"

    backend = EvidenceBackend()
    result = run_model_evaluation(input_path, tmp_path / "results", backend)
    payload = json.loads(result.output_path.read_text())
    assert result.failures == 0
    assert backend.summary_ids == [("1", "fetched_article"), ("1", "hn_comments")]
    assert [item["status"] for item in payload["summaries"]] == [
        "insufficient",
        "success",
    ]
    assert [item["summary_basis"] for item in payload["summaries"]] == [
        "fetched_article",
        "hn_comments",
    ]
    assert payload["summaries"][0]["reason"] == "Only an opening instruction"
    assert payload["summaries"][0]["error"] == ""
    assert input_path.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "bases",
    [
        ["fetched_article", "fetched_article"],
        ["hn_comments", "fetched_article"],
        ["fetched_article", "hn_comments", "hn_comments"],
    ],
)
def test_load_rejects_invalid_repeated_summary_attempts(tmp_path, bases):
    attempts = []
    for basis in bases:
        item = candidate("1", "Game")
        item.summary_basis = basis
        item.discussion_text = "Comment sample" if basis == "hn_comments" else ""
        attempts.append(item)
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], attempts)
    with pytest.raises(ModelEvaluationInputError, match="duplicate item IDs"):
        load_model_evaluation_input(input_path)


def test_load_accepts_version_three_and_keeps_its_unique_id_constraint(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [candidate("1", "Old input")]
    )
    payload = json.loads(input_path.read_text())
    payload["schema_version"] = 3
    for item in payload["summary_candidates"]:
        del item["content_kind"]
    input_path.write_text(json.dumps(payload))
    assert (
        load_model_evaluation_input(input_path).summary_candidates[0].story.title
        == "Old input"
    )
    payload["summary_candidates"] *= 2
    input_path.write_text(json.dumps(payload))
    with pytest.raises(ModelEvaluationInputError, match="duplicate item IDs"):
        load_model_evaluation_input(input_path)


def test_capture_supports_two_attempts_for_every_selected_item(tmp_path):
    from daily_brief.llm.model_evaluation import MAX_SUMMARY_ITEMS

    attempts = []
    for index in range(MAX_SUMMARY_ITEMS):
        source = candidate(str(index), "Game")
        source.summary_basis = "fetched_article"
        discussion = candidate(str(index), "Game")
        discussion.summary_basis = "hn_comments"
        discussion.discussion_text = "Comment sample"
        attempts.extend([source, discussion])
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], attempts)
    assert (
        len(load_model_evaluation_input(input_path).summary_candidates)
        == 2 * MAX_SUMMARY_ITEMS
    )


def test_schema_five_preserves_community_roundup_inputs_and_prompts(tmp_path):
    from daily_brief.llm.topic_classifier import build_topic_classifier_prompt

    original = community_roundup("7")
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [[original]], [original])

    payload = json.loads(input_path.read_text())
    replayed = load_model_evaluation_input(input_path)

    assert payload["schema_version"] == 5
    assert payload["summary_candidates"][0]["content_kind"] == "community_roundup"
    assert (
        replayed.exploration_classification_batches[0][0].content_kind
        == "community_roundup"
    )
    assert build_topic_classifier_prompt(
        replayed.exploration_classification_batches[0]
    ) == (build_topic_classifier_prompt([original]))
    assert build_summary_prompt(replayed.summary_candidates[0]) == build_summary_prompt(
        original
    )


@pytest.mark.parametrize("schema_version", [3, 4])
def test_old_schemas_default_content_kind_to_article(tmp_path, schema_version):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [candidate("1", "Old input")]
    )
    payload = json.loads(input_path.read_text())
    payload["schema_version"] = schema_version
    for item in payload["summary_candidates"]:
        del item["content_kind"]
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    assert (
        load_model_evaluation_input(input_path).summary_candidates[0].content_kind
        == "article"
    )


def test_old_schemas_reject_content_kind_field(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [candidate("1", "Old input")]
    )
    payload = json.loads(input_path.read_text())
    payload["schema_version"] = 4
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelEvaluationInputError, match="invalid item"):
        load_model_evaluation_input(input_path)


def test_legacy_schema_bounds_and_retries_are_preserved(tmp_path):
    from daily_brief.llm.model_evaluation import MAX_SUMMARY_ITEMS

    attempts = []
    for index in range(MAX_SUMMARY_ITEMS):
        source = candidate(str(index), "Game")
        source.summary_basis = "fetched_article"
        discussion = candidate(str(index), "Game")
        discussion.summary_basis = "hn_comments"
        discussion.discussion_text = "Comment sample"
        attempts.extend([source, discussion])
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], attempts)
    payload = json.loads(input_path.read_text())
    payload["schema_version"] = 4
    for item in payload["summary_candidates"]:
        del item["content_kind"]
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        len(load_model_evaluation_input(input_path).summary_candidates)
        == 2 * MAX_SUMMARY_ITEMS
    )

    payload["summary_candidates"].append(payload["summary_candidates"][0])
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelEvaluationInputError, match="at most"):
        load_model_evaluation_input(input_path)

    payload["schema_version"] = 3
    payload["summary_candidates"] = payload["summary_candidates"][:2]
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ModelEvaluationInputError, match="duplicate item IDs"):
        load_model_evaluation_input(input_path)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.__setitem__("content_kind", "unknown"),
        lambda item: item.__setitem__("source_url", "https://example.com/external"),
        lambda item: item.__setitem__("summary_basis", "story_text"),
    ],
)
def test_schema_five_rejects_invalid_community_roundup(tmp_path, mutate):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [community_roundup("1")]
    )
    payload = json.loads(input_path.read_text())
    mutate(payload["summary_candidates"][0])
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelEvaluationInputError):
        load_model_evaluation_input(input_path)


def test_schema_five_allows_bounded_article_then_roundup_classification(tmp_path):
    from daily_brief.config import EXPLORATION_CLASSIFIER_MAX_CANDIDATES

    batches = []
    for index in range(EXPLORATION_CLASSIFIER_MAX_CANDIDATES):
        article = candidate(str(index), "Ask HN")
        roundup = community_roundup(str(index))
        batches.extend([[article], [roundup]])
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", batches, [])

    assert len(
        load_model_evaluation_input(input_path).exploration_classification_batches
    ) == (2 * EXPLORATION_CLASSIFIER_MAX_CANDIDATES)

    payload = json.loads(input_path.read_text())
    (
        payload["exploration_classification_batches"][1],
        payload["exploration_classification_batches"][0],
    ) = (
        payload["exploration_classification_batches"][0],
        payload["exploration_classification_batches"][1],
    )
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ModelEvaluationInputError, match="article then community_roundup"
    ):
        load_model_evaluation_input(input_path)


def test_schema_five_rejects_excess_roundup_summaries_and_duplicate_roundup(tmp_path):
    from daily_brief.config import EXPLORATION_CLASSIFIER_MAX_CANDIDATES

    summaries = [
        community_roundup(str(index))
        for index in range(EXPLORATION_CLASSIFIER_MAX_CANDIDATES)
    ]
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], summaries)
    assert len(load_model_evaluation_input(input_path).summary_candidates) == len(
        summaries
    )

    summaries.append(community_roundup(str(EXPLORATION_CLASSIFIER_MAX_CANDIDATES)))
    capture_model_evaluation_input(input_path, "2026-07-20", [], summaries)
    with pytest.raises(ModelEvaluationInputError, match="community_roundup items"):
        load_model_evaluation_input(input_path)

    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [community_roundup("1"), community_roundup("1")]
    )
    with pytest.raises(ModelEvaluationInputError, match="duplicate community_roundup"):
        load_model_evaluation_input(input_path)


def test_schema_five_rejects_article_and_roundup_summary_for_same_item(tmp_path):
    article = candidate("1", "Ask HN")
    article.summary_basis = "story_text"
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path, "2026-07-20", [], [article, community_roundup("1")]
    )

    with pytest.raises(ModelEvaluationInputError, match="cannot mix article"):
        load_model_evaluation_input(input_path)


def test_schema_five_accepts_the_combined_summary_bound(tmp_path):
    from daily_brief.config import EXPLORATION_CLASSIFIER_MAX_CANDIDATES
    from daily_brief.llm.model_evaluation import MAX_SUMMARY_CANDIDATES, MAX_SUMMARY_ITEMS

    summaries = []
    for index in range(MAX_SUMMARY_ITEMS):
        source = candidate(str(index), "Article")
        source.summary_basis = "fetched_article"
        discussion = candidate(str(index), "Article")
        discussion.summary_basis = "hn_comments"
        discussion.discussion_text = "Comment sample"
        summaries.extend([source, discussion])
    summaries.extend(
        community_roundup(f"roundup-{index}")
        for index in range(EXPLORATION_CLASSIFIER_MAX_CANDIDATES)
    )
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(input_path, "2026-07-20", [], summaries)

    assert len(load_model_evaluation_input(input_path).summary_candidates) == (
        MAX_SUMMARY_CANDIDATES
    )


def test_long_source_replay_preserves_original_and_selected_prompt(tmp_path):
    body = 'Previous release: maintenance.\n' * 12000 + '\nNovaDB introduces snapshot reads, except on replicas.'
    item = candidate('1', 'NovaDB snapshot reads', fetched_text=body)
    item.summary_basis = 'fetched_article'
    path = tmp_path / 'long.json'
    capture_model_evaluation_input(path, '2026-09-19', [[item]], [item])
    loaded = load_model_evaluation_input(path)
    restored = loaded.summary_candidates[0]
    assert restored.story.fetched_text == body
    assert build_summary_prompt(restored) == build_summary_prompt(item)
    assert 'except on replicas' in build_summary_prompt(restored)
    assert len(build_summary_prompt(restored)) < 40000
