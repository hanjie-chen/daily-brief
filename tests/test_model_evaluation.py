import json

import pytest

from daily_brief.model_evaluation import (
    ModelEvaluationInputError,
    capture_model_evaluation_input,
    load_model_evaluation_input,
    run_model_evaluation,
)
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


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.classifier_ids = []
        self.summary_ids = []

    def classify(self, candidates):
        self.classifier_ids = [item.story.hn_item_id for item in candidates]
        return {"1", "unknown"}

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
        [candidate("1", "分类标题")],
        [candidate("2", "摘要标题", fetched_text="抓取的中文与 English 正文")],
    )

    loaded = load_model_evaluation_input(input_path)

    assert loaded.date_label == "2026-07-20"
    assert loaded.classifier_candidates[0].story.title == "分类标题"
    assert (
        loaded.summary_candidates[0].story.fetched_text
        == "抓取的中文与 English 正文"
    )


def test_evaluation_replays_one_input_without_touching_state_files(tmp_path):
    data_dir = tmp_path / "data"
    input_path = data_dir / "model-eval-inputs/2026-07-20.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [candidate("1", "AI tool"), candidate("2", "Database")],
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
        clock=iter([1.0, 1.5, 2.0, 2.25, 3.0, 3.75]).__next__,
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
    assert payload["classifier"] == {
        "status": "success",
        "duration_seconds": 0.5,
        "selected_ids": ["1"],
        "error": "",
    }
    assert [item["summary"] for item in payload["summaries"]] == [
        "中文摘要：AI tool",
        "中文摘要：Database",
    ]


def test_evaluation_records_partial_failures_and_continues(tmp_path):
    input_path = tmp_path / "input.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [candidate("1", "AI tool")],
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
    assert payload["classifier"]["status"] == "failed"
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
        {"schema_version": 2},
        {
            "schema_version": 1,
            "date": "20-07-2026",
            "classifier_candidates": [],
            "summary_candidates": [],
        },
    ],
)
def test_load_rejects_invalid_schema(tmp_path, payload):
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ModelEvaluationInputError):
        load_model_evaluation_input(input_path)
