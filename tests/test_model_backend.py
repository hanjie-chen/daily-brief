from daily_brief.model_backend import CodexBackend, ensure_selected_ids
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


class RecordingSummarizer:
    def __init__(self):
        self.seen = []

    def summarize(self, item):
        self.seen.append(item.story.hn_item_id)
        return "摘要"


class RecordingClassifier:
    def __init__(self):
        self.seen = []

    def classify(self, items):
        self.seen = [item.story.hn_item_id for item in items]
        return {"1"}


def test_codex_backend_delegates_both_model_tasks():
    summarizer = RecordingSummarizer()
    classifier = RecordingClassifier()
    backend = CodexBackend(summarizer=summarizer, classifier=classifier)
    items = [candidate("1"), candidate("2")]

    assert backend.name == "codex"
    assert backend.classify(items) == {"1"}
    assert backend.summarize(items[0]) == "摘要"
    assert classifier.seen == ["1", "2"]
    assert summarizer.seen == ["1"]


def test_selected_ids_are_limited_to_supplied_candidates():
    items = [candidate("1"), candidate("2")]

    assert ensure_selected_ids(["2", "unknown"], items) == {"2"}
