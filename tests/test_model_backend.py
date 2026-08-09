from daily_brief.model_backend import ensure_selected_ids
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


def test_selected_ids_are_limited_to_supplied_candidates():
    items = [candidate("1"), candidate("2")]

    assert ensure_selected_ids(["2", "unknown"], items) == {"2"}
