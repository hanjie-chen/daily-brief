from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.scoring import score_candidate
from daily_brief.selection import dedupe_candidates, select_sections


def story(item_id, title, points=50, comments=10, url=None):
    return Story(
        source="test",
        hn_item_id=str(item_id),
        title=title,
        source_url=url or f"https://example.com/{item_id}",
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at="2026-07-08T00:00:00+08:00",
        points=points,
        comments=comments,
    )


def match(keyword, weight, bonus):
    return KeywordMatch(keyword=keyword, weight=weight, source="text", bonus=bonus, start=0, end=len(keyword))


def test_score_uses_log_heat_and_bonus_caps():
    candidate = Candidate(
        story=story(1, "AI coding with Claude", points=100, comments=20),
        matched_keywords=[
            match("AI coding", "high", 4.0),
            match("Claude", "high", 4.0),
            match("AI agent", "high", 4.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 16.14
    assert scored.why == "keywords: AI coding, Claude, AI agent"


def test_select_ai_requires_score_and_minimum_points():
    low_heat = score_candidate(
        Candidate(story=story(1, "AI agent", points=0, comments=0), matched_keywords=[match("AI agent", "high", 4.0)])
    )
    enough_heat = score_candidate(
        Candidate(
            story=story(2, "AI agent", points=10, comments=0),
            matched_keywords=[match("AI agent", "high", 4.0)],
        )
    )

    ai_items, hot_items = select_sections([low_heat, enough_heat], [])

    assert [item.story.hn_item_id for item in ai_items] == ["2"]
    assert hot_items == []
    assert low_heat.rejection_reason == "below_ai_minimum"


def test_non_ai_hot_uses_threshold_or_fallback():
    hot = Candidate(story=story(1, "SQLite release", points=320, comments=12))
    fallback = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [fallback, hot])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].section == "non_ai_hot"


def test_non_ai_hot_fallback_selects_one_when_no_threshold_met():
    first = Candidate(story=story(1, "SQLite release", points=120, comments=12))
    second = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [second, first])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].why == "today's hottest non-AI story"


def test_dedupe_prefers_ai_candidate_by_hn_item_id():
    ai_candidate = Candidate(story=story(1, "AI story"), section="ai")
    hot_candidate = Candidate(story=story(1, "AI story"), section="non_ai_hot")

    deduped = dedupe_candidates([hot_candidate, ai_candidate])

    assert len(deduped) == 1
    assert deduped[0].section == "ai"
