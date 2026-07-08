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
    return _dedupe_candidates(
        candidates,
        key=lambda candidate, index: (_priority(candidate), candidate.score, -index),
    )


def _dedupe_ai_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return _dedupe_candidates(
        candidates,
        key=lambda candidate, index: (_meets_ai_minimum(candidate), _priority(candidate), candidate.score, -index),
    )


def _dedupe_candidates(candidates: list[Candidate], key) -> list[Candidate]:
    parent = list(range(len(candidates)))
    hn_item_groups: dict[str, int] = {}
    source_url_groups: dict[str, int] = {}

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for index, candidate in enumerate(candidates):
        if candidate.story.hn_item_id:
            if candidate.story.hn_item_id in hn_item_groups:
                union(index, hn_item_groups[candidate.story.hn_item_id])
            else:
                hn_item_groups[candidate.story.hn_item_id] = index
        if candidate.story.source_url:
            if candidate.story.source_url in source_url_groups:
                union(index, source_url_groups[candidate.story.source_url])
            else:
                source_url_groups[candidate.story.source_url] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        groups.setdefault(find(index), []).append(index)

    deduped: list[Candidate] = []
    for group in sorted(groups.values(), key=min):
        best_index = max(group, key=lambda index: key(candidates[index], index))
        deduped.append(candidates[best_index])
    return deduped


def select_sections(ai_candidates: list[Candidate], non_ai_candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    ai_pool = _dedupe_ai_candidates(ai_candidates)
    retained_ai_ids = {id(candidate) for candidate in ai_pool}
    ai_items = _select_ai(ai_pool)
    for candidate in ai_candidates:
        if id(candidate) not in retained_ai_ids:
            candidate.selected = False
            candidate.section = ""
            candidate.rejection_reason = "not_selected"
    hot_pool = [candidate for candidate in non_ai_candidates if not any(_same_story(candidate, item) for item in ai_items)]
    hot_items = _select_non_ai_hot(hot_pool)
    return ai_items, hot_items


def _select_ai(candidates: list[Candidate]) -> list[Candidate]:
    selected: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if not _meets_ai_minimum(candidate):
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


def _meets_ai_minimum(candidate: Candidate) -> bool:
    return candidate.score >= AI_MIN_SCORE and candidate.story.points >= AI_MIN_POINTS


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
