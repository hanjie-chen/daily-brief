import json
import logging

import pytest

from daily_brief.generation import SourceCollectionError, pipeline, run_generate
from fakes import FakeClassifier, FakeSummarizer, story


def test_run_generate_writes_markdown_and_json(tmp_path):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    summarizer = FakeSummarizer()

    result = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[
            story(
                "1",
                "AI coding agent with Claude",
                points=40,
                comments=8,
                story_text="A new coding agent.",
            ),
            story("2", "Tiny AI mention", points=1, comments=0),
        ],
        hot_stories=[
            story(
                "3",
                "SQLite release notes",
                source="hn_official",
                points=350,
                comments=20,
            ),
            story(
                "4",
                "OpenAI launches a model",
                source="hn_official",
                points=500,
                comments=80,
            ),
        ],
        summarizer=summarizer,
        generated_at="2026-07-08T08:04:00+08:00",
    )

    assert result.brief_path == output_dir / "2026-07-08.md"
    assert result.data_path == data_dir / "2026-07-08-hn-candidates.json"
    assert result.public_json_path == output_dir / "2026-07-08.json"
    assert result.brief_path.exists()
    assert result.data_path.exists()
    assert result.public_json_path.exists()

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "# Daily Brief - 2026-07-08" in markdown
    assert "AI coding agent with Claude" in markdown
    assert "Summary for AI coding agent with Claude" in markdown
    assert "SQLite release notes" in markdown
    assert "Summary for SQLite release notes" in markdown
    assert "OpenAI launches a model" in markdown

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    by_id = {item["hn_item_id"]: item for item in candidate_data}
    assert by_id["1"]["selected"] is True
    assert by_id["1"]["section"] == "ai"
    assert by_id["2"]["selected"] is False
    assert by_id["2"]["rejection_reason"] == "below_ai_minimum"
    assert by_id["3"]["selected"] is True
    assert by_id["3"]["section"] == "ai"
    assert by_id["4"]["selected"] is True
    assert by_id["4"]["section"] == "ai"
    assert by_id["1"]["summary_generation"] == {
        "status": "success",
        "provider": "FakeSummarizer",
        "model": "",
        "attempts": 1,
        "provider_status": "completed",
        "input_tokens": 100,
        "output_tokens": 20,
        "thought_tokens": 60,
        "total_tokens": 180,
        "error_type": "",
        "error_code": "",
        "http_status": None,
        "error_message": "",
    }
    assert summarizer.titles == [
        "OpenAI launches a model",
        "SQLite release notes",
        "AI coding agent with Claude",
    ]

    public_payload = json.loads(result.public_json_path.read_text(encoding="utf-8"))
    assert public_payload["schema_version"] == 2
    assert public_payload["date"] == "2026-07-08"
    assert public_payload["generated_at"] == "2026-07-08T08:04:00+08:00"
    assert [
        item["hn_item_id"] for item in public_payload["sections"]["ai"]["items"]
    ] == [
        "4",
        "3",
        "1",
    ]
    assert [
        item["hn_item_id"] for item in public_payload["sections"]["non_ai_hot"]["items"]
    ] == []


def test_no_content_marker_and_public_json_replace_each_other_on_rerun(tmp_path):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    empty = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    assert empty.public_json_path is None
    assert empty.no_content_marker_path == output_dir / "2026-07-08.no-content"
    assert empty.no_content_marker_path.read_bytes() == b""
    assert not (output_dir / "2026-07-08.json").exists()
    assert "No publishable items selected today." in empty.brief_path.read_text(
        encoding="utf-8"
    )

    populated = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    assert populated.public_json_path == output_dir / "2026-07-08.json"
    assert populated.public_json_path.exists()
    assert populated.no_content_marker_path is None
    assert not (output_dir / "2026-07-08.no-content").exists()

    empty_again = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    assert empty_again.public_json_path is None
    assert empty_again.no_content_marker_path.exists()
    assert not (output_dir / "2026-07-08.json").exists()


def test_run_generate_fails_when_algolia_fetch_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(pipeline, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(
        pipeline,
        "fetch_hot_stories",
        lambda: pytest.fail("HN API must not run after Algolia fails"),
    )

    with pytest.raises(SourceCollectionError, match="algolia"):
        run_generate(
            output_dir=output_dir,
            data_dir=data_dir,
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
        )

    assert not output_dir.exists()
    assert not data_dir.exists()


def test_run_generate_fails_when_hot_fetch_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def raise_hot_error():
        raise RuntimeError("hn unavailable")

    monkeypatch.setattr(pipeline, "fetch_hot_stories", raise_hot_error)

    with pytest.raises(SourceCollectionError, match="hn_official"):
        run_generate(
            output_dir=output_dir,
            data_dir=data_dir,
            date_label="2026-07-08",
            algolia_stories=[
                story("1", "AI coding agent with Claude", points=40, comments=8)
            ],
            summarizer=FakeSummarizer(),
        )

    assert not output_dir.exists()
    assert not data_dir.exists()


def test_source_failure_does_not_replace_existing_date_artifacts(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    output_dir.mkdir()
    data_dir.mkdir()
    existing_artifacts = {
        output_dir / "2026-07-08.md": "existing markdown",
        output_dir / "2026-07-08.json": "existing public json",
        output_dir / "2026-07-08.no-content": "existing marker",
        data_dir / "2026-07-08-hn-candidates.json": "existing audit",
    }
    for path, content in existing_artifacts.items():
        path.write_text(content, encoding="utf-8")

    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(pipeline, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(
        pipeline,
        "fetch_hot_stories",
        lambda: pytest.fail("HN API must not run after Algolia fails"),
    )

    with pytest.raises(SourceCollectionError, match="algolia"):
        run_generate(
            output_dir=output_dir,
            data_dir=data_dir,
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
        )

    for path, content in existing_artifacts.items():
        assert path.read_text(encoding="utf-8") == content


def test_run_generate_logs_source_success_and_completion(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        pipeline,
        "fetch_algolia_stories",
        lambda window: [
            story("1", "AI coding agent with Claude", points=40, comments=8)
        ],
    )
    monkeypatch.setattr(pipeline, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 12.5, 20.0, 23.0, 30.0, 31.25]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief"):
        run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
            clock=clock,
        )

    assert "source=algolia status=success stories=1 duration=2.500s" in caplog.text
    assert "source=hn_official status=success stories=0 duration=3.000s" in caplog.text
    assert "component=exploration_router status=completed inspected=0" in caplog.text
    assert "status=completed ai_items=1 hot_items=0" in caplog.text


def test_run_generate_logs_terminal_source_failure(tmp_path, monkeypatch, caplog):
    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(pipeline, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(pipeline, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 100.0, 200.0, 201.0, 300.0, 301.0]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief"):
        with pytest.raises(SourceCollectionError):
            run_generate(
                output_dir=tmp_path / "briefs",
                data_dir=tmp_path / "data",
                date_label="2026-07-08",
                summarizer=FakeSummarizer(),
                clock=clock,
            )

    assert (
        "source=algolia status=failed duration=90.000s error=RuntimeError"
        in caplog.text
    )
    assert "source=hn_official" not in caplog.text
    assert not (tmp_path / "briefs").exists()


def test_run_generate_dedupes_hot_candidates_before_writing_json(tmp_path):
    duplicate_url = "https://example.com/shared"

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[],
        hot_stories=[
            story(
                "1",
                "Original SQLite writeup",
                source="hn_official",
                points=120,
                comments=30,
                url=duplicate_url,
            ),
            story(
                "2",
                "Popular SQLite discussion",
                source="hn_official",
                points=350,
                comments=80,
                url=duplicate_url,
            ),
        ],
        summarizer=FakeSummarizer(),
    )

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    duplicate_records = [
        item for item in candidate_data if item["source_url"] == duplicate_url
    ]

    assert len(duplicate_records) == 1
    assert duplicate_records[0]["hn_item_id"] == "2"
    assert duplicate_records[0]["title"] == "Popular SQLite discussion"
    assert duplicate_records[0]["selected"] is True
    assert duplicate_records[0]["section"] == "ai"

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert duplicate_records[0]["title"] in markdown


def test_recently_selected_story_is_excluded_and_recorded_in_snapshot(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "recommendation-history.json").write_text(
        json.dumps({"2026-07-19": ["1"]}),
        encoding="utf-8",
    )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=data_dir,
        date_label="2026-07-20",
        algolia_stories=[
            story("1", "Claude yesterday", points=500, comments=80),
            story("2", "OpenAI today", points=100, comments=20),
        ],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    records = {
        item["hn_item_id"]: item
        for item in json.loads(result.data_path.read_text(encoding="utf-8"))
    }
    assert records["1"]["selected"] is False
    assert records["1"]["rejection_reason"] == "recently_selected"
    assert records["2"]["selected"] is True
    assert "Claude yesterday" not in result.brief_path.read_text(encoding="utf-8")


def test_same_date_history_does_not_change_rerun_selection(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "recommendation-history.json").write_text(
        json.dumps({"2026-07-20": ["1"]}),
        encoding="utf-8",
    )

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=data_dir,
        date_label="2026-07-20",
        algolia_stories=[story("1", "Claude release", points=40, comments=8)],
        hot_stories=[],
        summarizer=FakeSummarizer(),
    )

    assert "Claude release" in result.brief_path.read_text(encoding="utf-8")


def test_run_generate_can_capture_exact_model_inputs(tmp_path):
    data_dir = tmp_path / "data"

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=data_dir,
        date_label="2026-07-20",
        algolia_stories=[
            story(
                "1",
                "Claude release",
                points=40,
                comments=8,
                url="https://example.com/selected",
            ),
            story("2", "History of typography", points=350, comments=30),
        ],
        hot_stories=[],
        classifier=FakeClassifier(),
        article_fetcher=lambda url, **kwargs: "Grounded article facts.",
        summarizer=FakeSummarizer(),
        capture_model_inputs=True,
    )

    assert result.model_input_path == data_dir / "model-eval-inputs/2026-07-20.json"
    payload = json.loads(result.model_input_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert [
        batch[0]["hn_item_id"]
        for batch in payload["exploration_classification_batches"]
    ] == ["2"]
    assert (
        payload["exploration_classification_batches"][0][0]["fetched_text"]
        == "Grounded article facts."
    )
    assert [item["hn_item_id"] for item in payload["summary_candidates"]] == [
        "1",
        "2",
    ]
    assert payload["summary_candidates"][0]["fetched_text"] == "Grounded article facts."
