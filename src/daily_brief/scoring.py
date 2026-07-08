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
    topic_bonus = min(sum(2.0 for match in candidate.matched_keywords if match.keyword in TOPIC_KEYWORDS), TOPIC_BONUS_CAP)
    candidate.score = heat + keyword_bonus + topic_bonus
    candidate.why = _why(candidate)
    return candidate


def _keyword_bonus(candidate: Candidate) -> float:
    total = 0.0
    for layer, cap in LAYER_CAPS.items():
        layer_total = sum(match.bonus for match in candidate.matched_keywords if match.weight == layer)
        total += min(layer_total, cap)
    return total


def _why(candidate: Candidate) -> str:
    if not candidate.matched_keywords:
        return ""
    keywords = ", ".join(match.keyword for match in candidate.matched_keywords[:5])
    return f"keywords: {keywords}"
