from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import Candidate


class Summarizer(Protocol):
    def summarize(self, candidate: Candidate) -> str: ...


class TopicClassifier(Protocol):
    def classify(self, candidates: list[Candidate]) -> set[str]: ...


class ModelBackend(Summarizer, TopicClassifier, Protocol):
    name: str


def ensure_selected_ids(
    selected_ids: Sequence[str], candidates: list[Candidate]
) -> set[str]:
    """Constrain provider output to IDs present in the supplied batch."""

    allowed_ids = {candidate.story.hn_item_id for candidate in candidates}
    return set(selected_ids) & allowed_ids
