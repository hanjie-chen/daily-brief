import json

from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.render import render_candidates_json, render_markdown


def candidate(section="ai", selected=True, title="AI coding agent"):
    item = Candidate(
        story=Story(
            source="algolia",
            hn_item_id="1",
            title=title,
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=30,
            comments=5,
        ),
        matched_keywords=[KeywordMatch("AI coding", "high", "text", 4.0, 0, 9)],
        score=9.2,
        selected=selected,
        section=section,
        summary="这是一个 AI coding agent 项目。",
        why="keywords: AI coding",
    )
    return item


def test_render_markdown_contains_required_fields():
    markdown = render_markdown("2026-07-08", [candidate()], [])

    assert "# Daily Brief - 2026-07-08" in markdown
    assert "## Hacker News: AI" in markdown
    assert "### AI coding agent" in markdown
    assert "Summary: 这是一个 AI coding agent 项目。" in markdown
    assert "Why: keywords: AI coding" in markdown
    assert "Source: https://example.com" in markdown
    assert "Discussion: https://news.ycombinator.com/item?id=1" in markdown
    assert "Stats: 30 points / 5 comments" in markdown
    assert "## Hacker News: Non-AI Hot" in markdown
    assert "No items selected." in markdown
    assert markdown.endswith("\n")


def test_render_markdown_shows_empty_ai_section():
    markdown = render_markdown("2026-07-08", [], [candidate(section="non_ai_hot")])

    assert "## Hacker News: AI\n\nNo items selected." in markdown
    assert "## Hacker News: Non-AI Hot" in markdown


def test_render_candidates_json_uses_snake_case_fields():
    item = candidate(selected=False, title="中文 AI coding agent")
    item.score = 9.23456
    item.rejection_reason = "not_selected"

    rendered = render_candidates_json([item])
    data = json.loads(rendered)

    assert rendered.endswith("\n")
    assert "中文 AI coding agent" in rendered
    assert set(data[0]) == {
        "source",
        "hn_item_id",
        "title",
        "source_url",
        "hn_discussion_url",
        "created_at",
        "points",
        "comments",
        "matched_keywords",
        "score",
        "selected",
        "section",
        "rejection_reason",
    }
    assert data[0]["source"] == "algolia"
    assert data[0]["hn_item_id"] == "1"
    assert data[0]["title"] == "中文 AI coding agent"
    assert data[0]["source_url"] == "https://example.com"
    assert data[0]["hn_discussion_url"] == "https://news.ycombinator.com/item?id=1"
    assert data[0]["created_at"] == "2026-07-08T00:00:00+08:00"
    assert data[0]["points"] == 30
    assert data[0]["comments"] == 5
    assert data[0]["matched_keywords"] == ["AI coding"]
    assert data[0]["score"] == 9.2346
    assert data[0]["selected"] is False
    assert data[0]["section"] == "ai"
    assert data[0]["rejection_reason"] == "not_selected"
