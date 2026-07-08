import json

from daily_brief import cli
from daily_brief.cli import build_parser, main, run_generate
from daily_brief.models import Story


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
    assert "OpenAI launches a model" not in markdown

    candidate_data = json.loads(result.data_path.read_text(encoding="utf-8"))
    by_id = {item["hn_item_id"]: item for item in candidate_data}
    assert by_id["1"]["selected"] is True
    assert by_id["1"]["section"] == "ai"
    assert by_id["2"]["selected"] is False
    assert by_id["2"]["rejection_reason"] == "below_ai_minimum"
    assert by_id["3"]["selected"] is True
    assert by_id["3"]["section"] == "non_ai_hot"
    assert "4" not in by_id
    assert summarizer.titles == ["AI coding agent with Claude", "SQLite release notes"]


def test_run_generate_uses_fallback_summary_when_summarizer_raises(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=[story("1", "AI coding agent with Claude", points=40, comments=8)],
        hot_stories=[],
        summarizer=RaisingSummarizer(),
    )

    markdown = result.brief_path.read_text(encoding="utf-8")
    assert "AI coding agent with Claude。" in markdown
    assert "摘要生成失败时保留此基础信息。" in markdown


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


class FakeSummarizer:
    def __init__(self):
        self.titles = []

    def summarize(self, candidate):
        self.titles.append(candidate.story.title)
        return f"Summary for {candidate.story.title}"


class RaisingSummarizer:
    def summarize(self, candidate):
        raise RuntimeError("boom")


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
