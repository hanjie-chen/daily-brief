from __future__ import annotations

import math

from .config import (
    HIGH_WEIGHT_BONUS_CAP,
    KEYWORD_BONUS_CAP,
    LOW_WEIGHT_BONUS_CAP,
    MEDIUM_HIGH_WEIGHT_BONUS_CAP,
    MEDIUM_WEIGHT_BONUS_CAP,
    TOPIC_BONUS_CAP,
    TOPIC_KEYWORDS,
)
from .models import Candidate

LAYER_CAPS = {
    "high": HIGH_WEIGHT_BONUS_CAP,
    "medium_high": MEDIUM_HIGH_WEIGHT_BONUS_CAP,
    "medium": MEDIUM_WEIGHT_BONUS_CAP,
    "low": LOW_WEIGHT_BONUS_CAP,
}


def score_candidate(candidate: Candidate) -> Candidate:
    heat = math.log(candidate.story.points + 1) + 0.5 * math.log(candidate.story.comments + 1)
    keyword_bonus = min(_keyword_bonus(candidate), KEYWORD_BONUS_CAP)
    topic_bonus = _topic_bonus(candidate)
    candidate.score = heat + keyword_bonus + topic_bonus
    candidate.why = _why(candidate)
    return candidate


def _keyword_bonus(candidate: Candidate) -> float:
    matches = list(_unique_matches(candidate))
    total = 0.0
    for layer, cap in LAYER_CAPS.items():
        layer_total = sum(match.bonus for match in matches if match.weight == layer)
        total += min(layer_total, cap)
    return total


def _topic_bonus(candidate: Candidate) -> float:
    return min(
        sum(1.0 for match in _unique_matches(candidate) if _gets_topic_bonus(match.keyword, candidate)),
        TOPIC_BONUS_CAP,
    )


def _gets_topic_bonus(keyword: str, candidate: Candidate) -> bool:
    if keyword in TOPIC_KEYWORDS:
        return True
    return keyword == "developer tools" and _has_high_or_medium_high_match(candidate)


def _has_high_or_medium_high_match(candidate: Candidate) -> bool:
    return any(match.weight in {"high", "medium_high"} for match in _unique_matches(candidate))


def _why(candidate: Candidate) -> str:
    if not candidate.matched_keywords:
        return ""
    keywords = ", ".join(match.keyword for match in list(_unique_matches(candidate))[:5])
    return f"keywords: {keywords}"


def _unique_matches(candidate: Candidate):
    seen: set[tuple[str, str]] = set()
    for match in candidate.matched_keywords:
        key = match.keyword, match.weight
        if key in seen:
            continue
        seen.add(key)
        yield match
