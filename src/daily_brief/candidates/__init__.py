"""Rule-based candidate handling: collection, matching, scoring, history, and selection."""

from .history import load_history, recent_ids, save_history
from .hn_client import (
    HNDiscussionFetchError,
    HNDiscussionResult,
    fetch_algolia_stories,
    fetch_hn_discussion,
    fetch_hot_stories,
)
from .keywords import match_keywords
from .scoring import apply_article_evidence_bonus, score_candidate
from .selection import (
    dedupe_candidates,
    meets_exploration_minimum,
    rank_exploration_candidates,
    select_ai_candidates,
    select_exploration_candidates,
)


__all__ = [
    "HNDiscussionFetchError",
    "HNDiscussionResult",
    "apply_article_evidence_bonus",
    "dedupe_candidates",
    "fetch_algolia_stories",
    "fetch_hn_discussion",
    "fetch_hot_stories",
    "load_history",
    "match_keywords",
    "meets_exploration_minimum",
    "rank_exploration_candidates",
    "recent_ids",
    "save_history",
    "score_candidate",
    "select_ai_candidates",
    "select_exploration_candidates",
]
