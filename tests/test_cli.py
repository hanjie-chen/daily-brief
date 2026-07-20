import json
import logging

import pytest

from daily_brief import cli
from daily_brief.cli import build_parser, main, run_generate
from daily_brief.models import Story


@pytest.fixture(autouse=True)
def prevent_live_classifier_and_article_calls(monkeypatch):
    monkeypatch.setattr(cli, "CodexTopicClassifier", lambda: FakeClassifier())
    monkeypatch.setattr(cli, "fetch_article_text", lambda url: "")


def test_parser_defaults_to_generate_command():
    parser = build_parser()

    args = parser.parse_args([])

    assert args.command == "generate"
    assert args.output_dir == "briefs"
    assert args.data_dir == "data"
    assert args.dry_run is False


def test_run_generate_writes_markdown_and_json(tmp_path):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"
    summarizer = FakeSummarizer()

    result = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "AI coding agent with Claude", points=40, comments=8, story_text="A new coding agent."),
            story("2", "Tiny AI mention", points=1, comments=0),
        ],
        hot_stories=[
            story("3", "SQLite release notes", source="hn_official", points=350, comments=20),
            story("4", "OpenAI launches a model", source="hn_official", points=500, comments=80),
        ],
        summarizer=summarizer,
    )

    assert result.brief_path == output_dir / "2026-07-08.md"
    assert result.data_path == data_dir / "2026-07-08-hn-candidates.json"
    assert result.brief_path.exists()
    assert result.data_path.exists()

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
    assert by_id["3"]["section"] == "non_ai_hot"
    assert by_id["4"]["selected"] is True
    assert by_id["4"]["section"] == "ai"
    assert summarizer.titles == [
        "OpenAI launches a model",
        "AI coding agent with Claude",
        "SQLite release notes",
    ]


def test_run_generate_uses_fallback_summary_when_summarizer_raises(tmp_path, capsys):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "AI coding agent with Claude", points=40, comments=8)],
        hot_stories=[],
        summarizer=RaisingSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "未能生成可靠摘要，请查看原文或讨论。" in markdown
    assert "Summary failed for AI coding agent with Claude: boom" in capsys.readouterr().err


def test_run_generate_writes_files_when_algolia_fetch_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(cli, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(
        cli,
        "fetch_hot_stories",
        lambda: [story("3", "SQLite release notes", source="hn_official", points=350, comments=20)],
    )

    result = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        summarizer=FakeSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")

    assert result.brief_path.exists()
    assert result.data_path.exists()
    assert "AI data source failed" in markdown
    assert "Algolia" in markdown
    assert "SQLite release notes" in markdown


def test_run_generate_writes_files_when_hot_fetch_fails(tmp_path, monkeypatch):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def raise_hot_error():
        raise RuntimeError("hn unavailable")

    monkeypatch.setattr(cli, "fetch_hot_stories", raise_hot_error)

    result = run_generate(
        output_dir=output_dir,
        data_dir=data_dir,
        date_label="2026-07-08",
        algolia_stories=[story("1", "AI coding agent with Claude", points=40, comments=8)],
        summarizer=FakeSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")

    assert result.brief_path.exists()
    assert result.data_path.exists()
    assert "HN hot data source failed" in markdown
    assert "Non-AI Hot" in markdown
    assert "AI coding agent with Claude" in markdown


def test_run_generate_logs_source_success_and_completion(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        cli,
        "fetch_algolia_stories",
        lambda window: [story("1", "AI coding agent with Claude", points=40, comments=8)],
    )
    monkeypatch.setattr(cli, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 12.5, 20.0, 23.0]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief.cli"):
        run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
            clock=clock,
        )

    assert "source=algolia status=success stories=1 duration=2.500s" in caplog.text
    assert "source=hn_official status=success stories=0 duration=3.000s" in caplog.text
    assert "status=completed ai_items=1 hot_items=0" in caplog.text


def test_run_generate_logs_terminal_source_failure(tmp_path, monkeypatch, caplog):
    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(cli, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(cli, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 100.0, 200.0, 201.0]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief.cli"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
            clock=clock,
        )

    assert "source=algolia status=failed duration=90.000s error=RuntimeError" in caplog.text
    assert "source=hn_official status=success stories=0 duration=1.000s" in caplog.text
    assert "AI data source failed" in result.brief_path.read_text(encoding="utf-8")


def test_run_generate_keeps_non_ai_algolia_story_out_of_ai_section(tmp_path):
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
    assert by_id["1"]["matched_keywords"] == []
    assert by_id["1"]["selected"] is True
    assert by_id["1"]["section"] == "non_ai_hot"

    markdown = result.brief_path.read_text(encoding="utf-8")
    ai_section = markdown.split("## Hacker News: AI", 1)[1].split("## Hacker News: Non-AI Hot", 1)[0]
    assert "SQLite release notes" not in ai_section
    assert "## Hacker News: Non-AI Hot" in markdown
    assert "SQLite release notes" in markdown


def test_run_generate_treats_weak_only_matches_as_non_ai_hot_candidates(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[
            story("1", "Database model migration guide", points=1200, comments=300),
        ],
        hot_stories=[
            story("2", "New workflow engine reaches stable release", source="hn_official", points=1300, comments=350),
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
    ai_section = markdown.split("## Hacker News: AI", 1)[1].split("## Hacker News: Non-AI Hot", 1)[0]
    assert "Database model migration guide" not in ai_section
    assert "New workflow engine reaches stable release" not in ai_section


def test_run_generate_dedupes_hot_candidates_before_writing_json(tmp_path):
    duplicate_url = "https://example.com/shared"

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[],
        hot_stories=[
            story("1", "Original SQLite writeup", source="hn_official", points=120, comments=30, url=duplicate_url),
            story("2", "Popular SQLite discussion", source="hn_official", points=350, comments=80, url=duplicate_url),
        ],
        summarizer=FakeSummarizer(),
    )

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    duplicate_records = [item for item in candidate_data if item["source_url"] == duplicate_url]

    assert len(duplicate_records) == 1
    assert duplicate_records[0]["hn_item_id"] == "2"
    assert duplicate_records[0]["title"] == "Popular SQLite discussion"
    assert duplicate_records[0]["selected"] is True
    assert duplicate_records[0]["section"] == "non_ai_hot"

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert duplicate_records[0]["title"] in markdown


def test_main_dry_run_does_not_create_output_directories_or_files(tmp_path, monkeypatch):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def fail_if_called(**kwargs):
        raise AssertionError("dry-run should not generate files")

    monkeypatch.setattr(cli, "run_generate", fail_if_called)

    exit_code = main(["generate", "--output-dir", str(output_dir), "--data-dir", str(data_dir), "--dry-run"])

    assert exit_code == 0
    assert not output_dir.exists()
    assert not data_dir.exists()


def test_classifier_promotes_high_heat_unmatched_story_to_ai(tmp_path):
    classifier = FakeClassifier({"1"})

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Unseen Neural Product", points=750, comments=500)],
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url: "",
        summarizer=FakeSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "Unseen Neural Product" in markdown.split("## Hacker News: AI", 1)[1].split(
        "## Hacker News: Non-AI Hot", 1
    )[0]
    assert "Why: topic classifier: AI" in markdown
    assert classifier.seen_ids == ["1"]


def test_classifier_failure_preserves_keyword_routing(tmp_path, caplog):
    with caplog.at_level(logging.ERROR, logger="daily_brief.cli"):
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
            article_fetcher=lambda url: "",
            summarizer=FakeSummarizer(),
        )

    markdown = result.brief_path.read_text(encoding="utf-8")
    ai_section = markdown.split("## Hacker News: AI", 1)[1].split("## Hacker News: Non-AI Hot", 1)[0]
    assert "Claude release" in ai_section
    assert "Unseen Neural Product" not in ai_section
    assert "component=topic_classifier status=failed" in caplog.text


def test_classifier_receives_only_thirty_hottest_unmatched_candidates(tmp_path):
    classifier = FakeClassifier()
    candidates = [
        story(str(item_id), f"Unmatched story {item_id}", points=item_id, comments=0)
        for item_id in range(1, 36)
    ]

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=candidates,
        hot_stories=[],
        classifier=classifier,
        article_fetcher=lambda url: "",
        summarizer=FakeSummarizer(),
    )

    assert classifier.seen_ids == [str(item_id) for item_id in range(35, 5, -1)]


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

    records = {item["hn_item_id"]: item for item in json.loads(result.data_path.read_text(encoding="utf-8"))}
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


def test_selected_external_article_text_reaches_summarizer(tmp_path):
    summarizer = CapturingSummarizer()
    fetched_urls = []

    def fetch_article(url):
        fetched_urls.append(url)
        return "Grounded article facts."

    run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[
            story("1", "Claude release", points=40, comments=8, url="https://example.com/selected"),
            story("2", "OpenAI tiny", points=1, comments=0, url="https://example.com/rejected"),
        ],
        hot_stories=[],
        article_fetcher=fetch_article,
        summarizer=summarizer,
    )

    assert fetched_urls == ["https://example.com/selected"]
    assert summarizer.fetched_texts == ["Grounded article facts."]


def test_article_failure_does_not_prevent_brief_generation(tmp_path, caplog):
    def raise_fetch_error(url):
        raise RuntimeError("article unavailable")

    with caplog.at_level(logging.ERROR, logger="daily_brief.cli"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-20",
            algolia_stories=[story("1", "Claude release", points=40, comments=8)],
            hot_stories=[],
            article_fetcher=raise_fetch_error,
            summarizer=FakeSummarizer(),
        )

    assert result.brief_path.exists()
    assert "component=article_fetch item_id=1 status=failed" in caplog.text


class FakeSummarizer:
    def __init__(self):
        self.titles = []

    def summarize(self, candidate):
        self.titles.append(candidate.story.title)
        return f"Summary for {candidate.story.title}"


class RaisingSummarizer:
    def summarize(self, candidate):
        raise RuntimeError("boom")


class CapturingSummarizer:
    def __init__(self):
        self.fetched_texts = []

    def summarize(self, candidate):
        self.fetched_texts.append(candidate.story.fetched_text)
        return "Captured summary"


class FakeClassifier:
    def __init__(self, selected_ids=None):
        self.selected_ids = set(selected_ids or set())
        self.seen_ids = []

    def classify(self, candidates):
        self.seen_ids = [candidate.story.hn_item_id for candidate in candidates]
        return self.selected_ids


class RaisingClassifier:
    def classify(self, candidates):
        raise RuntimeError("classifier unavailable")


def story(
    item_id,
    title,
    *,
    source="algolia",
    points=30,
    comments=5,
    story_text="",
    url=None,
):
    return Story(
        source=source,
        hn_item_id=str(item_id),
        title=title,
        source_url=url or f"https://example.com/{item_id}",
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at="2026-07-08T00:00:00+08:00",
        points=points,
        comments=comments,
        story_text=story_text,
    )
