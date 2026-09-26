import json
import logging

import pytest

from daily_brief.article_fetcher import ArticleFetchError
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import run_generate
from fakes import FakeClassifier, FakeSummarizer, RaisingClassifier, story


def test_exploration_walk_selects_only_confirmed_outside_topics(tmp_path):
    classifier = FakeClassifier(
        {
            "1": "ai",
            "2": "core_non_ai",
            "3": "uncertain",
            "4": "outside",
            "5": "outside",
        }
    )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story(str(item_id), f"Ambiguous hot story {item_id}", points=600 - item_id)
            for item_id in range(1, 6)
        ],
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url, **kwargs: "Grounded article evidence.",
        summarizer=FakeSummarizer(),
    )

    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert classifier.seen_ids == ["1", "2", "3", "4", "5"]
    assert records["1"]["topic_route"] == "article_ai"
    assert records["2"]["topic_route"] == "article_core_non_ai"
    assert records["3"]["topic_route"] == "article_uncertain"
    assert records["3"]["rejection_reason"] == "topic_uncertain"
    assert records["4"]["topic_route"] == "article_outside"
    assert records["5"]["topic_route"] == "article_outside"
    assert [
        item["hn_item_id"]
        for item in json.loads(result.public_json_path.read_text(encoding="utf-8"))[
            "sections"
        ]["non_ai_hot"]["items"]
    ] == ["4", "5"]


def test_exploration_walk_continues_after_two_outside_and_reranks_by_hn_heat(
    tmp_path,
):
    classifier = FakeClassifier(default_label="outside")

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "Discussion-heavy", points=300, comments=1000),
            story("2", "Most points", points=500, comments=0),
            story("3", "Second-most points", points=400, comments=0),
            story("4", "Third-most points", points=350, comments=0),
        ],
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url, **kwargs: "Grounded outside evidence.",
        summarizer=FakeSummarizer(),
    )

    assert classifier.seen_ids == ["1", "2", "3", "4"]
    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    assert [
        item["hn_item_id"]
        for item in public_payload["sections"]["non_ai_hot"]["items"]
    ] == ["2", "3"]


def test_article_core_uses_bonus_while_low_heat_outside_is_rejected(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "Unmatched technical article", points=150, comments=20),
            story("2", "Unmatched outside article", points=150, comments=20),
        ],
        hot_stories=[],
        classifier=FakeClassifier({"1": "core_non_ai", "2": "outside"}),
        article_fetcher=lambda url, **kwargs: "Grounded article evidence.",
        summarizer=FakeSummarizer(),
    )

    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    core_item = public_payload["sections"]["ai"]["items"][0]
    assert core_item["hn_item_id"] == "1"
    assert core_item["why"] == "正文确认属于计算与软件领域；按 HN 热度入选"
    assert records["1"]["topic_route"] == "article_core_non_ai"
    assert records["1"]["score"] > records["2"]["score"]
    assert records["2"]["rejection_reason"] == "below_exploration_minimum"
    assert public_payload["sections"]["non_ai_hot"]["items"] == []


def test_exploration_fetch_failure_is_unknown_and_does_not_block_backfill(tmp_path):
    fetched_urls = []

    def fetch(url, **kwargs):
        fetched_urls.append(url)
        if url.endswith("/1"):
            raise ArticleFetchError(
                "unavailable", error_code="request_failed", method="direct"
            )
        return "Grounded outside article evidence."

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "First hot story", points=500, comments=20),
            story("2", "Second hot story", points=400, comments=20),
        ],
        hot_stories=[],
        classifier=FakeClassifier(),
        article_fetcher=fetch,
        summarizer=FakeSummarizer(),
    )

    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert records["1"]["topic_route"] == "topic_unknown"
    assert records["1"]["rejection_reason"] == "topic_unknown"
    assert records["2"]["selected"] is True
    assert fetched_urls == ["https://example.com/1", "https://example.com/2"]


def test_exploration_self_post_uses_story_text_without_external_fetch(tmp_path):
    discussion_url = "https://news.ycombinator.com/item?id=1"
    classifier = FakeClassifier()

    def fail_if_fetched(url, **kwargs):
        raise AssertionError("self-post exploration must not trigger external fetch")

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story(
                "1",
                "A year restoring historic footpaths",
                points=500,
                comments=20,
                story_text="A field report about rural history and public footpaths.",
                url=discussion_url,
            )
        ],
        hot_stories=[],
        classifier=classifier,
        article_fetcher=fail_if_fetched,
        summarizer=FakeSummarizer(),
    )

    records = json.loads(result.data_path.read_text(encoding="utf-8"))
    assert classifier.seen_ids == ["1"]
    assert records[0]["selected"] is True
    assert records[0]["topic_route"] == "article_outside"
    retrieval = records[0]["article_retrieval"]
    assert retrieval["status"] == "not_needed"
    assert retrieval["method"] == "story_text"
    assert retrieval["extractor"] == "plain_text"
    assert retrieval["attempts"] == 0
    assert records[0]["summary_basis"] == "story_text"


def test_run_generate_routes_approved_core_keyword_to_ai_schema_section(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "SQLite release notes", points=1200, comments=300),
        ],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    by_id = {item["hn_item_id"]: item for item in candidate_data}
    assert by_id["1"]["matched_keywords"] == ["SQLite"]
    assert by_id["1"]["selected"] is True
    assert by_id["1"]["section"] == "ai"

    markdown = result.brief_path.read_text(encoding="utf-8")
    ai_section = markdown.split("## Hacker News: Tech picks", 1)[1].split(
        "## Hacker News: Beyond the Bubble", 1
    )[0]
    assert "SQLite release notes" in ai_section


def test_run_generate_treats_weak_only_matches_as_non_ai_hot_candidates(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "Database model migration guide", points=1200, comments=300),
        ],
        hot_stories=[
            story(
                "2",
                "New workflow engine reaches stable release",
                source="hn_official",
                points=1300,
                comments=350,
            ),
        ],
        summarizer=FakeSummarizer(),
    )

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    by_id = {item["hn_item_id"]: item for item in candidate_data}

    assert by_id["1"]["matched_keywords"] == ["model"]
    assert by_id["1"]["selected"] is True
    assert by_id["1"]["section"] == "non_ai_hot"

    assert by_id["2"]["matched_keywords"] == ["workflow"]
    assert by_id["2"]["selected"] is True
    assert by_id["2"]["section"] == "non_ai_hot"

    markdown = result.brief_path.read_text(encoding="utf-8")
    ai_section = markdown.split("## Hacker News: Tech picks", 1)[1].split(
        "## Hacker News: Beyond the Bubble", 1
    )[0]
    assert "Database model migration guide" not in ai_section
    assert "New workflow engine reaches stable release" not in ai_section


def test_article_ai_enters_core_pool_with_article_evidence_bonus(tmp_path, caplog):
    classifier = FakeClassifier({"2": "ai"})

    with caplog.at_level(logging.INFO, logger="daily_brief"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-20",
            algolia_stories=[
                story("1", "Claude release", points=40, comments=8),
                story(
                    "2",
                    "Everything I own, owned",
                    points=1350,
                    comments=335,
                ),
            ],
            hot_stories=[],
            classifier=classifier,
            article_fetcher=lambda url, **kwargs: (
                "The author used Claude for agent-driven firmware reverse "
                "engineering and analyzed security risks."
            ),
            summarizer=FakeSummarizer(),
            clock=iter([10.0, 12.5]).__next__,
        )

    markdown = result.brief_path.read_text(encoding="utf-8")
    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert "Claude release" in markdown
    assert "Everything I own, owned" in markdown
    assert classifier.seen_ids == ["2"]
    assert records["2"]["topic_route"] == "article_ai"
    assert records["2"]["selected"] is True
    assert records["2"]["section"] == "ai"
    assert records["2"]["score"] == pytest.approx(14.1172)
    assert records["2"]["article_retrieval"]["status"] == "success"
    assert (
        "component=topic_classifier status=success item_id=2 label=ai duration=2.500s"
        in caplog.text
    )


def test_classifier_failure_preserves_keyword_routing(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="daily_brief"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-20",
            algolia_stories=[
                story("1", "Claude release", points=40, comments=8),
                story("2", "Unseen Neural Product", points=750, comments=500),
            ],
            hot_stories=[],
            classifier=RaisingClassifier(),
            article_fetcher=lambda url, **kwargs: "Grounded article evidence.",
            summarizer=FakeSummarizer(),
        )

    markdown = result.brief_path.read_text(encoding="utf-8")
    ai_section = markdown.split("## Hacker News: Tech picks", 1)[1].split(
        "## Hacker News: Beyond the Bubble", 1
    )[0]
    assert "Claude release" in ai_section
    assert "Unseen Neural Product" not in ai_section
    assert "component=topic_classifier status=failed" in caplog.text
    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert records["1"]["topic_route"] == "keyword"
    assert records["2"]["topic_route"] == "classifier_failed"
    assert records["2"]["rejection_reason"] == "classifier_failed"


def test_classifier_inspects_at_most_twenty_five_highest_scoring_candidates(tmp_path):
    classifier = FakeClassifier(default_label="core_non_ai")
    candidates = [
        story(
            str(item_id),
            f"Unmatched story {item_id}",
            points=300 + item_id,
            comments=0,
        )
        for item_id in range(1, 27)
    ]

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=candidates,
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url, **kwargs: "Grounded article evidence.",
        summarizer=FakeSummarizer(),
    )

    assert classifier.seen_ids == [str(item_id) for item_id in range(26, 1, -1)]
    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert records["26"]["topic_route"] == "article_core_non_ai"
    assert records["21"]["rejection_reason"] == "core_not_selected"
    assert records["21"]["section"] == ""
    assert records["1"]["topic_route"] == "not_evaluated"


def test_exploration_classifier_orders_candidates_by_score(tmp_path):
    classifier = FakeClassifier(default_label="core_non_ai")

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[
            story("1", "Higher points", points=301, comments=0),
            story("2", "Hot discussion", points=300, comments=1000),
        ],
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url, **kwargs: "Grounded article evidence.",
        summarizer=FakeSummarizer(),
    )

    assert classifier.seen_ids == ["2", "1"]


@pytest.mark.parametrize("keyword_title", [False, True])
@pytest.mark.parametrize("outcome", ["success", "insufficient", "model_error", "too_short", "fetch_error", "uncertain"])
def test_roundup_is_assessed_before_selection_and_replaced_when_unusable(
    tmp_path, keyword_title, outcome,
):
    from daily_brief.llm.model_evaluation import load_model_evaluation_input
    from daily_brief.llm.summarizer import InsufficientSummaryMaterial

    calls = []
    overview = "一位开发者的体素引擎支持 GPU 地形编辑；另一位开发者的日程工具可在本地运行。"
    comments = (
        "[Comment 11]\nI built a voxel engine with GPU terrain editing. " * 6
        + "[Comment 12]\nI built a local-first calendar with offline event search. " * 6
        + "[Comment 13]\nI recommend measuring redraw time before changing the renderer. " * 6
    )

    class RoundupClassifier:
        def classify(self, candidates):
            item = candidates[0]
            calls.append(("classify", item.story.hn_item_id, item.content_kind))
            if item.story.hn_item_id == "49686380":
                if item.content_kind == "article":
                    assert not item.discussion_text
                    return {"49686380": "community_roundup"}
                assert item.discussion_text == comments
                return {"49686380": "uncertain" if outcome == "uncertain" else "core_non_ai"}
            return {item.story.hn_item_id: "core_non_ai"}

    class RoundupSummarizer:
        def summarize(self, item):
            calls.append(("summarize", item.story.hn_item_id, item.selected))
            if item.content_kind == "community_roundup":
                assert not item.selected
                assert item.summary_basis == "hn_comments"
                assert item.summary_mode == "community_roundup"
                if outcome == "insufficient":
                    raise InsufficientSummaryMaterial("Only project names and promotional links.")
                if outcome == "model_error":
                    raise RuntimeError("provider unavailable")
                return overview
            return "工具新增了离线搜索功能。"

    def fetch_comments(item_id):
        calls.append(("comments", item_id))
        if outcome == "fetch_error":
            raise RuntimeError("HN unavailable")
        return HNDiscussionResult(
            text=comments if outcome != "too_short" else "Nice!",
            comments=3 if outcome != "too_short" else 1,
            chars=len(comments) if outcome != "too_short" else 5,
            requested_items=4, failed_items=0,
        )

    result = run_generate(
        output_dir=tmp_path / "briefs", data_dir=tmp_path / "data",
        date_label="2026-09-16",
        algolia_stories=[
            story(
                "49686380",
                "Ask HN: What AI tools are you working on?" if keyword_title
                else "Ask HN: What are you working on? (September 2026)",
                url="https://news.ycombinator.com/item?id=49686380",
                story_text="What are you working on? What have you been curious about lately?",
                points=362, comments=1125,
            ),
            *[story(str(i), "SQLite update", points=100 - i, comments=10) for i in range(1, 6)],
        ],
        hot_stories=[], classifier=RoundupClassifier(), summarizer=RoundupSummarizer(),
        hn_discussion_fetcher=fetch_comments,
        article_fetcher=lambda url, **kwargs: "SQLite now supports offline search.",
        capture_model_inputs=True,
    )
    audit = {item["hn_item_id"]: item for item in json.loads(result.data_path.read_text())}
    roundup = audit["49686380"]
    items = json.loads(result.public_json_path.read_text())["sections"]["ai"]["items"]
    assert len(items) == 5
    assert roundup["content_kind"] == "community_roundup"
    assert calls.count(("comments", "49686380")) == 1
    assert audit["5"]["selected"] == (outcome != "success")
    if outcome == "success":
        assert roundup["selected"] is True
        assert next(item for item in items if item["title"].startswith("Ask HN:"))["summary"] == (
            "根据 Hacker News 部分评论：" + overview
        )
        assert calls.count(("summarize", "49686380", False)) == 1
        assert roundup["summary_context"]["strategy"] == "community_roundup"
    else:
        assert roundup["selected"] is False
        assert roundup["rejection_reason"]
        assert not any(item["title"].startswith("Ask HN:") for item in items)
        if outcome == "insufficient":
            assert "promotional" in roundup["content_reason"]

    captured = load_model_evaluation_input(result.model_input_path)
    roundup_batches = [batch[0] for batch in captured.exploration_classification_batches
                       if batch[0].story.hn_item_id == "49686380"]
    assert roundup_batches[0].content_kind == "article"
    assert not roundup_batches[0].discussion_text
    if outcome not in {"fetch_error", "too_short"}:
        assert roundup_batches[1].content_kind == "community_roundup"
        assert roundup_batches[1].discussion_text == comments
    roundup_summaries = [item for item in captured.summary_candidates
                        if item.story.hn_item_id == "49686380"]
    assert len(roundup_summaries) == (1 if outcome in {"success", "insufficient", "model_error"} else 0)


def test_roundup_route_cannot_be_used_for_an_external_article(tmp_path):
    calls = []
    result = run_generate(
        output_dir=tmp_path / "briefs", data_dir=tmp_path / "data",
        date_label="2026-09-16", algolia_stories=[],
        hot_stories=[story("1", "What are you working on?", points=362, comments=1125)],
        classifier=FakeClassifier(default_label="community_roundup"),
        summarizer=FakeSummarizer(),
        hn_discussion_fetcher=lambda item_id: calls.append(item_id),
    )
    audit = json.loads(result.data_path.read_text())[0]
    assert audit["selected"] is False
    assert audit["rejection_reason"] == "classifier_failed"
    assert calls == []
