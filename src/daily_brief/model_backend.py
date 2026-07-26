from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from .models import Candidate
from .summarizer import CodexSummarizer
from .topic_classifier import CodexTopicClassifier


class Summarizer(Protocol):
    def summarize(self, candidate: Candidate) -> str: ...


class TopicClassifier(Protocol):
    def classify(self, candidates: list[Candidate]) -> set[str]: ...


class ModelBackend(Summarizer, TopicClassifier, Protocol):
    name: str


@dataclass
class CodexBackend:
    """Local fallback backend kept behind the provider-neutral contract."""

    name: ClassVar[str] = "codex"
    summarizer: Summarizer = field(default_factory=CodexSummarizer)
    classifier: TopicClassifier = field(default_factory=CodexTopicClassifier)

    def summarize(self, candidate: Candidate) -> str:
        return self.summarizer.summarize(candidate)

    def classify(self, candidates: list[Candidate]) -> set[str]:
        return self.classifier.classify(candidates)


def ensure_selected_ids(
    selected_ids: Sequence[str], candidates: list[Candidate]
) -> set[str]:
    """Constrain provider output to IDs present in the supplied batch."""

    allowed_ids = {candidate.story.hn_item_id for candidate in candidates}
    return set(selected_ids) & allowed_ids
