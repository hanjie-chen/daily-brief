from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.config import AI_MIN_POINTS, AI_MIN_SCORE
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


def test_score_topic_bonus_adds_two_per_topic_keyword_up_to_cap():
    candidate = Candidate(
        story=story(1, "AI coding with AI workflow", points=100, comments=20),
        matched_keywords=[
            match("AI coding", "high", 4.0),
            match("AI workflow", "medium_high", 2.5),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 16.64


def test_score_topic_bonus_ignores_non_topic_high_match():
    candidate = Candidate(
        story=story(1, "Claude release", points=100, comments=20),
        matched_keywords=[match("Claude", "high", 4.0)],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 10.14


def test_score_caps_combined_keyword_bonus_and_topic_bonus_independently():
    candidate = Candidate(
        story=story(1, "AI coding with Gemini, AI workflow, and AI benchmark", points=100, comments=20),
        matched_keywords=[
            match("AI coding", "high", 4.0),
            match("AI agent", "high", 4.0),
            match("Gemini", "medium_high", 2.5),
            match("AI workflow", "medium_high", 2.5),
            match("AI benchmark", "medium", 1.5),
            match("workflow", "weak", 0.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 20.14


def test_score_developer_tools_alone_gets_no_topic_or_keyword_bonus():
    candidate = Candidate(
        story=story(1, "developer tools", points=100, comments=20),
        matched_keywords=[match("developer tools", "weak", 0.0)],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 6.14


def test_score_developer_tools_gets_topic_bonus_with_high_signal():
    candidate = Candidate(
        story=story(1, "Claude developer tools", points=100, comments=20),
        matched_keywords=[
            match("Claude", "high", 4.0),
            match("developer tools", "weak", 0.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 13.14


def test_score_weak_keywords_add_no_bonus_with_medium_match():
    candidate = Candidate(
        story=story(1, "AI model workflow", points=100, comments=20),
        matched_keywords=[
            match("AI", "medium", 1.5),
            match("model", "weak", 0.0),
            match("workflow", "weak", 0.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 7.64


def test_score_weak_keywords_add_one_bonus_max_with_high_match():
    candidate = Candidate(
        story=story(1, "Claude model workflow", points=100, comments=20),
        matched_keywords=[
            match("Claude", "high", 4.0),
            match("model", "weak", 0.0),
            match("workflow", "weak", 0.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 11.14


def test_score_weak_keywords_add_one_bonus_max_with_medium_high_match():
    candidate = Candidate(
        story=story(1, "Gemini model workflow", points=100, comments=20),
        matched_keywords=[
            match("Gemini", "medium_high", 2.5),
            match("model", "weak", 0.0),
            match("workflow", "weak", 0.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 9.64


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


def test_select_ai_rejects_candidate_below_minimum_score_with_enough_points():
    low_score = Candidate(
        story=story(1, "AI mention without enough signal", points=AI_MIN_POINTS, comments=0),
        matched_keywords=[match("AI", "weak", 0.0)],
        score=AI_MIN_SCORE - 0.01,
    )

    ai_items, hot_items = select_sections([low_score], [])

    assert ai_items == []
    assert hot_items == []
    assert low_score.selected is False
    assert low_score.section == ""
    assert low_score.rejection_reason == "below_ai_minimum"


def test_select_ai_marks_overflow_qualifying_candidates_not_selected():
    candidates = [
        score_candidate(
            Candidate(
                story=story(item_id, "AI agent", points=100 - item_id, comments=10),
                matched_keywords=[match("AI agent", "high", 4.0)],
            )
        )
        for item_id in range(1, 7)
    ]

    ai_items, hot_items = select_sections(candidates, [])

    assert [item.story.hn_item_id for item in ai_items] == ["1", "2", "3", "4", "5"]
    assert hot_items == []
    assert sum(candidate.selected for candidate in candidates) == 5
    assert candidates[5].selected is False
    assert candidates[5].section == ""
    assert candidates[5].rejection_reason == "not_selected"


def test_select_ai_dedupes_duplicate_ai_candidates_before_selection():
    original = score_candidate(
        Candidate(
            story=story(1, "AI agent", points=100, comments=10),
            matched_keywords=[match("AI agent", "high", 4.0)],
        )
    )
    duplicate = score_candidate(
        Candidate(
            story=story(1, "AI agent duplicate", points=95, comments=8),
            matched_keywords=[match("AI agent", "high", 4.0)],
        )
    )

    ai_items, hot_items = select_sections([original, duplicate], [])

    assert [item.story.hn_item_id for item in ai_items] == ["1"]
    assert hot_items == []
    assert duplicate.selected is False
    assert duplicate.section == ""
    assert duplicate.rejection_reason == "not_selected"


def test_select_ai_dedupe_prefers_eligible_duplicate_before_score():
    ineligible_high_score = Candidate(
        story=story(1, "AI agent low points", points=AI_MIN_POINTS - 1, comments=100),
        matched_keywords=[match("AI agent", "high", 4.0)],
        score=AI_MIN_SCORE + 10.0,
    )
    eligible_lower_score = Candidate(
        story=story(1, "AI agent eligible duplicate", points=AI_MIN_POINTS, comments=0),
        matched_keywords=[match("AI agent", "high", 4.0)],
        score=AI_MIN_SCORE + 1.0,
    )

    ai_items, hot_items = select_sections([ineligible_high_score, eligible_lower_score], [])

    assert ai_items == [eligible_lower_score]
    assert hot_items == []
    assert eligible_lower_score.selected is True
    assert eligible_lower_score.section == "ai"
    assert ineligible_high_score.selected is False
    assert ineligible_high_score.section == ""
    assert ineligible_high_score.rejection_reason == "not_selected"


def test_non_ai_hot_uses_threshold_or_fallback():
    hot = Candidate(story=story(1, "SQLite release", points=320, comments=12))
    fallback = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [fallback, hot])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].section == "non_ai_hot"


def test_non_ai_hot_selects_two_when_multiple_stories_meet_threshold():
    highest_points = Candidate(story=story(1, "SQLite release", points=330, comments=12))
    highest_comments = Candidate(story=story(2, "Compiler notes", points=90, comments=180))

    ai_items, hot_items = select_sections([], [highest_comments, highest_points])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1", "2"]
    assert highest_points.selected is True
    assert highest_comments.selected is True


def test_non_ai_hot_fallback_selects_one_when_no_threshold_met():
    first = Candidate(story=story(1, "SQLite release", points=120, comments=12))
    second = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [second, first])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].why == "today's hottest non-AI story"


def test_non_ai_hot_suppresses_transitive_duplicate_of_selected_ai():
    selected_ai = Candidate(
        story=story(1, "AI agent", points=AI_MIN_POINTS, comments=10, url="https://example.com/ai"),
        matched_keywords=[match("AI agent", "high", 4.0)],
        score=AI_MIN_SCORE + 2.0,
    )
    ai_bridge = Candidate(
        story=story(1, "AI agent bridge", points=AI_MIN_POINTS - 1, comments=10, url="https://example.com/bridge"),
        matched_keywords=[match("AI agent", "high", 4.0)],
        score=AI_MIN_SCORE + 1.0,
    )
    transitive_hot = Candidate(
        story=story(2, "Bridge duplicate hot story", points=320, comments=20, url="https://example.com/bridge")
    )
    fallback = Candidate(story=story(3, "Other hot story", points=250, comments=20))

    ai_items, hot_items = select_sections([selected_ai, ai_bridge], [transitive_hot, fallback])

    assert ai_items == [selected_ai]
    assert transitive_hot not in hot_items
    assert transitive_hot.selected is False
    assert transitive_hot.rejection_reason == "not_selected"


def test_dedupe_prefers_ai_candidate_by_hn_item_id():
    ai_candidate = Candidate(story=story(1, "AI story"), section="ai")
    hot_candidate = Candidate(story=story(1, "AI story"), section="non_ai_hot")

    deduped = dedupe_candidates([hot_candidate, ai_candidate])

    assert len(deduped) == 1
    assert deduped[0].section == "ai"


def test_dedupe_prefers_ai_candidate_by_source_url_with_different_hn_item_ids():
    shared_url = "https://example.com/shared-story"
    ai_candidate = Candidate(story=story(1, "AI story", url=shared_url), section="ai")
    hot_candidate = Candidate(story=story(2, "AI story duplicate", url=shared_url), section="non_ai_hot")

    deduped = dedupe_candidates([hot_candidate, ai_candidate])

    assert len(deduped) == 1
    assert deduped[0].section == "ai"
    assert deduped[0].story.hn_item_id == "1"


def test_dedupe_collapses_transitive_duplicate_chain_and_prefers_ai_candidate():
    shared_url = "https://example.com/transitive-story"
    hn_duplicate = Candidate(story=story(1, "HN duplicate", url="https://example.com/hn"), score=20.0)
    bridge = Candidate(story=story(1, "Bridge duplicate", url=shared_url), score=30.0)
    ai_candidate = Candidate(
        story=story(2, "AI duplicate", url=shared_url),
        matched_keywords=[match("AI agent", "high", 4.0)],
        score=10.0,
    )

    deduped = dedupe_candidates([hn_duplicate, bridge, ai_candidate])

    assert deduped == [ai_candidate]
