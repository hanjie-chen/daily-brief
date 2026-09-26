from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from ..alternate_reporting import AlternateReportingFinder
from ..candidates import (
    apply_article_evidence_bonus,
    meets_exploration_minimum,
    rank_exploration_candidates,
    select_ai_candidates,
    select_exploration_candidates,
)
from ..config import EXPLORATION_CLASSIFIER_MAX_CANDIDATES
from ..model_backend import ensure_topic_decisions
from ..models import Candidate
from ..syndicated_copy import SyndicatedCopyFinder
from ..time_window import TimeWindow
from .material import (
    RETRIEVAL_MODE_CLASSIFICATION,
    prepare_candidate_material,
    prepare_hn_discussion_material,
)
from .summaries import prepare_summary_context, generate_candidate_summary

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SelectionResult:
    ai_items: list[Candidate]
    selected_hot_items: list[Candidate]
    classification_batches: list[list[Candidate]]
    summary_inputs: list[Candidate]


def classify_and_select_candidates(
    eligible_candidates: list[Candidate],
    topic_classifier,
    article_fetcher,
    syndicated_finder: SyndicatedCopyFinder | None,
    alternate_reporting_finder: AlternateReportingFinder | None,
    window: TimeWindow,
    clock: Callable[[], float],
    *,
    summary_client,
    hn_discussion_fetcher,
) -> SelectionResult:
    known_core_candidates = [
        candidate
        for candidate in eligible_candidates
        if _has_non_weak_keyword_match(candidate) and not _is_self_post(candidate)
    ]
    for candidate in known_core_candidates:
        candidate.topic_route = "keyword"
    unmatched_candidates = [
        candidate
        for candidate in eligible_candidates
        if not _has_non_weak_keyword_match(candidate) or _is_self_post(candidate)
    ]
    core_candidates = list(known_core_candidates)
    ranked_exploration = rank_exploration_candidates(unmatched_candidates)
    outside_candidates: list[Candidate] = []
    classification_batches: list[list[Candidate]] = []
    summary_inputs: list[Candidate] = []
    inspected_exploration = 0
    for candidate in ranked_exploration[:EXPLORATION_CLASSIFIER_MAX_CANDIDATES]:
        inspected_exploration += 1
        if not prepare_candidate_material(
            candidate,
            article_fetcher,
            syndicated_finder,
            alternate_reporting_finder,
            window,
            retrieval_mode=RETRIEVAL_MODE_CLASSIFICATION,
        ):
            candidate.topic_route = "topic_unknown"
            candidate.rejection_reason = "topic_unknown"
            continue
        if (
            not (candidate.story.fetched_text or candidate.story.story_text).strip()
            and not _is_self_post(candidate)
        ):
            candidate.topic_route = "article_uncertain"
            candidate.rejection_reason = "topic_uncertain"
            continue

        classification_batch = [candidate]
        classification_batches.append(deepcopy(classification_batch))
        classification_started = clock()
        try:
            decisions = ensure_topic_decisions(
                topic_classifier.classify(classification_batch),
                classification_batch,
            )
            label_decision = decisions[candidate.story.hn_item_id]
            if label_decision == "community_roundup":
                if not _is_self_post(candidate):
                    raise ValueError("community roundup must be an HN self-post")
                candidate.content_kind = "community_roundup"
                if not prepare_hn_discussion_material(candidate, hn_discussion_fetcher):
                    candidate.topic_route = "discussion_unavailable"
                    candidate.rejection_reason = "roundup_discussion_unavailable"
                    continue
                classification_batches.append(deepcopy(classification_batch))
                decisions = ensure_topic_decisions(
                    topic_classifier.classify(classification_batch), classification_batch,
                )
                label_decision = decisions[candidate.story.hn_item_id]
                if label_decision == "community_roundup":
                    raise ValueError("roundup comments require a topical decision")
        except Exception as exc:
            classification_duration = clock() - classification_started
            candidate.topic_route = "classifier_failed"
            candidate.rejection_reason = "classifier_failed"
            LOGGER.error(
                "component=topic_classifier status=failed item_id=%s "
                "duration=%.3fs error=%s message=%s",
                candidate.story.hn_item_id,
                classification_duration,
                type(exc).__name__,
                exc,
            )
            continue

        classification_duration = clock() - classification_started
        candidate.topic_route = f"article_{label_decision}"
        LOGGER.info(
            "component=topic_classifier status=success item_id=%s label=%s "
            "duration=%.3fs",
            candidate.story.hn_item_id,
            label_decision,
            classification_duration,
        )
        if candidate.content_kind == "community_roundup" and label_decision != "uncertain":
            prepare_summary_context(candidate)
            summary_inputs.append(deepcopy(candidate))
            generate_candidate_summary(candidate, summary_client)
            if candidate.summary_status != "success":
                candidate.rejection_reason = f"roundup_summary_{candidate.summary_status}"
                continue
        if label_decision == "outside":
            if meets_exploration_minimum(candidate):
                outside_candidates.append(candidate)
            else:
                candidate.rejection_reason = "below_exploration_minimum"
        elif label_decision == "uncertain":
            candidate.rejection_reason = "topic_uncertain"
        else:
            if not _has_non_weak_keyword_match(candidate):
                apply_article_evidence_bonus(candidate)
            core_candidates.append(candidate)
            LOGGER.info(
                "component=exploration_router item_id=%s status=article_%s "
                "score=%.4f",
                candidate.story.hn_item_id,
                label_decision,
                candidate.score,
            )

    ai_items = select_ai_candidates(core_candidates)
    selected_hot_items = select_exploration_candidates(outside_candidates)
    for candidate in [*ai_items, *selected_hot_items]:
        if candidate.content_kind == "community_roundup":
            candidate.why = "HN 部分评论提供了具体项目、工具或实践经验；按热度入选"
    LOGGER.info(
        "component=exploration_router status=completed inspected=%d outside=%d "
        "selected=%d limit=%d",
        inspected_exploration,
        len(outside_candidates),
        len(selected_hot_items),
        EXPLORATION_CLASSIFIER_MAX_CANDIDATES,
    )
    return SelectionResult(
        ai_items=ai_items,
        selected_hot_items=selected_hot_items,
        classification_batches=classification_batches,
        summary_inputs=summary_inputs,
    )


def _is_self_post(candidate: Candidate) -> bool:
    return (
        bool(candidate.story.source_url)
        and candidate.story.source_url == candidate.story.hn_discussion_url
    )


def _has_non_weak_keyword_match(candidate: Candidate) -> bool:
    return any(match.weight != "weak" for match in candidate.matched_keywords)
