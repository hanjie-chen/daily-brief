from __future__ import annotations

from typing import Protocol

from .models import Candidate
from .topic_classifier import TOPIC_LABELS


class Summarizer(Protocol):
    def summarize(self, candidate: Candidate) -> str: ...


class TopicClassifier(Protocol):
    def classify(self, candidates: list[Candidate]) -> dict[str, str]: ...


class ModelBackend(Summarizer, TopicClassifier, Protocol):
    name: str


def ensure_topic_decisions(
    decisions: dict[str, str], candidates: list[Candidate]
) -> dict[str, str]:
    """Require one valid topic decision for every supplied candidate."""

    allowed_ids = {candidate.story.hn_item_id for candidate in candidates}
    if set(decisions) != allowed_ids:
        raise ValueError("topic decisions must cover exactly the supplied item IDs")
    if any(label not in TOPIC_LABELS for label in decisions.values()):
        raise ValueError("topic decisions contain an unsupported label")
    return dict(decisions)
