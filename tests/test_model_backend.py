import pytest

from daily_brief.model_backend import ensure_topic_decisions
from daily_brief.models import Candidate, Story


def candidate(item_id: str) -> Candidate:
    return Candidate(
        story=Story(
            source="test",
            hn_item_id=item_id,
            title=f"Story {item_id}",
            source_url="https://example.com/story",
            hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
            created_at="2026-07-20T00:00:00Z",
            points=1,
            comments=0,
        )
    )


def test_topic_decisions_cover_exactly_supplied_candidates():
    items = [candidate("1"), candidate("2")]

    assert ensure_topic_decisions({"1": "ai", "2": "outside"}, items) == {
        "1": "ai",
        "2": "outside",
    }


@pytest.mark.parametrize(
    "decisions",
    [
        {"1": "ai"},
        {"1": "ai", "2": "outside", "unknown": "outside"},
        {"1": "ai", "2": "unsupported"},
    ],
)
def test_topic_decisions_reject_missing_unknown_or_invalid_values(decisions):
    with pytest.raises(ValueError):
        ensure_topic_decisions(decisions, [candidate("1"), candidate("2")])
