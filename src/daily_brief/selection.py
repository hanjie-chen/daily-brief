from __future__ import annotations

from .config import (
    AI_MAX_ITEMS,
    AI_MIN_POINTS,
    AI_MIN_SCORE,
    NON_AI_COMMENTS_THRESHOLD,
    NON_AI_MAX_ITEMS,
    NON_AI_POINTS_THRESHOLD,
)
from .models import Candidate


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    by_key: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.story.hn_item_id or candidate.story.source_url
        existing = by_key.get(key)
        if existing is None or _priority(candidate) > _priority(existing):
            by_key[key] = candidate
    return list(by_key.values())


def select_sections(ai_candidates: list[Candidate], non_ai_candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    ai_items = _select_ai(ai_candidates)
    ai_keys = {item.story.hn_item_id or item.story.source_url for item in ai_items}
    hot_pool = [candidate for candidate in non_ai_candidates if (candidate.story.hn_item_id or candidate.story.source_url) not in ai_keys]
    hot_items = _select_non_ai_hot(hot_pool)
    return ai_items, hot_items


def _select_ai(candidates: list[Candidate]) -> list[Candidate]:
    selected: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if candidate.score < AI_MIN_SCORE or candidate.story.points < AI_MIN_POINTS:
            candidate.selected = False
            candidate.section = ""
            candidate.rejection_reason = "below_ai_minimum"
            continue
        candidate.selected = True
        candidate.section = "ai"
        candidate.rejection_reason = ""
        selected.append(candidate)
        if len(selected) == AI_MAX_ITEMS:
            break
    return selected


def _select_non_ai_hot(candidates: list[Candidate]) -> list[Candidate]:
    sorted_candidates = sorted(candidates, key=lambda item: (item.story.points, item.story.comments), reverse=True)
    over_threshold = [
        candidate
        for candidate in sorted_candidates
        if candidate.story.points >= NON_AI_POINTS_THRESHOLD or candidate.story.comments >= NON_AI_COMMENTS_THRESHOLD
    ]
    selected = over_threshold[:NON_AI_MAX_ITEMS] if over_threshold else sorted_candidates[:1]
    for candidate in selected:
        candidate.selected = True
        candidate.section = "non_ai_hot"
        candidate.rejection_reason = ""
        candidate.why = "HN-wide hot story" if over_threshold else "today's hottest non-AI story"
    for candidate in sorted_candidates:
        if candidate not in selected:
            candidate.selected = False
            candidate.rejection_reason = "not_selected"
    return selected


def _priority(candidate: Candidate) -> int:
    if candidate.section == "ai" or candidate.matched_keywords:
        return 2
    if candidate.section == "non_ai_hot":
        return 1
    return 0
