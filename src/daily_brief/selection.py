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
    deduped: list[Candidate] = []
    for candidate in candidates:
        existing_index = next(
            (index for index, existing in enumerate(deduped) if _same_story(candidate, existing)),
            None,
        )
        if existing_index is None:
            deduped.append(candidate)
            continue
        if _priority(candidate) > _priority(deduped[existing_index]):
            deduped[existing_index] = candidate
    return deduped


def select_sections(ai_candidates: list[Candidate], non_ai_candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    ai_items = _select_ai(ai_candidates)
    hot_pool = [candidate for candidate in non_ai_candidates if not any(_same_story(candidate, item) for item in ai_items)]
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
        if len(selected) == AI_MAX_ITEMS:
            candidate.selected = False
            candidate.section = ""
            candidate.rejection_reason = "not_selected"
            continue
        candidate.selected = True
        candidate.section = "ai"
        candidate.rejection_reason = ""
        selected.append(candidate)
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


def _same_story(left: Candidate, right: Candidate) -> bool:
    same_hn_item = bool(left.story.hn_item_id and right.story.hn_item_id) and left.story.hn_item_id == right.story.hn_item_id
    same_source_url = (
        bool(left.story.source_url and right.story.source_url)
        and left.story.source_url == right.story.source_url
    )
    return same_hn_item or same_source_url
