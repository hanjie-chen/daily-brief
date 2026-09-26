from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from functools import partial

from ..article_fetcher import (
    CLASSIFICATION_FETCH_POLICY,
    CLASSIFICATION_HTTP_TIMEOUT_SECONDS,
    CLASSIFICATION_PDF_PARSE_TIMEOUT_SECONDS,
    SUMMARY_FETCH_POLICY,
    fetch_article,
)
from ..candidates import HNDiscussionFetchError, HNDiscussionResult, fetch_hn_discussion
from ..models import (
    AlternateReportingRecovery,
    ArticleRetrieval,
    Candidate,
    DiscussionRetrieval,
    RetrievalFailure,
    SameArticleRecovery,
    SummaryGeneration,
    SyndicatedRecovery,
    is_origin_block_reason,
)
from ..recovery import (
    AlternateReportingFinder,
    AlternateReportingOutcome,
    SameArticleFinder,
    SameArticleOutcome,
    SyndicatedCopyFinder,
    SyndicatedOutcome,
    attempt_alternate_reporting_recovery,
    attempt_same_article_recovery,
    attempt_syndicated_recovery,
    coerce_fetched_material,
    is_reuters_url,
)
from ..summarizer import article_fetch_failure_summary

LOGGER = logging.getLogger(__name__)
RETRIEVAL_MODE_CLASSIFICATION = "classification"
RETRIEVAL_MODE_SUMMARY = "summary"
MIN_DISCUSSION_COMMENTS = 3
MIN_DISCUSSION_CHARS = 500


def prepare_candidate_material(
    candidate: Candidate,
    article_fetcher_fn,
    syndicated_finder: SyndicatedCopyFinder | None,
    alternate_reporting_finder: AlternateReportingFinder | None,
    window,
    *,
    retrieval_mode: str = RETRIEVAL_MODE_SUMMARY,
    same_article_finder: SameArticleFinder | None = None,
) -> bool:
    if (
        candidate.story.source_url
        and candidate.story.source_url != candidate.story.hn_discussion_url
    ):
        if retrieval_mode not in {
            RETRIEVAL_MODE_CLASSIFICATION,
            RETRIEVAL_MODE_SUMMARY,
        }:
            raise ValueError(f"unknown retrieval mode: {retrieval_mode}")
        active_fetcher = (
            article_fetcher_fn
            if article_fetcher_fn is not None
            else fetch_article
        )
        if retrieval_mode == RETRIEVAL_MODE_CLASSIFICATION:
            article_client = partial(
                active_fetcher,
                timeout_seconds=CLASSIFICATION_HTTP_TIMEOUT_SECONDS,
                pdf_parse_timeout_seconds=(
                    CLASSIFICATION_PDF_PARSE_TIMEOUT_SECONDS
                ),
                policy=CLASSIFICATION_FETCH_POLICY,
            )
        else:
            article_client = partial(
                active_fetcher,
                policy=SUMMARY_FETCH_POLICY,
                wayback_not_before=_wayback_not_before(
                    candidate.story.created_at,
                    window.start,
                ),
                wayback_not_after=window.end,
            )
        try:
            material = coerce_fetched_material(
                article_client(candidate.story.source_url),
                candidate.story.source_url,
            )
        except Exception as exc:
            original_failure = _retrieval_failure(exc)
            recovery = SyndicatedOutcome(
                material=None,
                audit=SyndicatedRecovery(),
            )
            alternate_recovery = AlternateReportingOutcome(
                material=None,
                audit=AlternateReportingRecovery(),
            )
            material_origin = ""
            same_recovery = SameArticleOutcome(None, SameArticleRecovery())
            if (
                retrieval_mode == RETRIEVAL_MODE_SUMMARY
                and original_failure.fallback_attempted
                and is_origin_block_reason(original_failure.fallback_reason)
            ):
                same_recovery = attempt_same_article_recovery(
                    candidate, article_client, same_article_finder
                )
                if same_recovery.material is not None:
                    material_origin = "same_article"
            if (
                same_recovery.material is None
                and retrieval_mode == RETRIEVAL_MODE_SUMMARY
                and _should_attempt_reuters_recovery(candidate, original_failure)
            ):
                recovery = attempt_syndicated_recovery(
                    candidate,
                    article_client,
                    syndicated_finder,
                )
                material_origin = "syndicated_copy"
            elif (
                same_recovery.material is None
                and retrieval_mode == RETRIEVAL_MODE_SUMMARY
                and _should_attempt_alternate_reporting(
                    candidate, original_failure
                )
            ):
                alternate_recovery = attempt_alternate_reporting_recovery(
                    candidate,
                    article_client,
                    alternate_reporting_finder,
                )
                material_origin = "alternate_reporting"
            recovered_material = (
                same_recovery.material or recovery.material or alternate_recovery.material
            )
            if recovered_material is None:
                candidate.article_retrieval = ArticleRetrieval(
                    status="failed",
                    method=original_failure.method,
                    extractor=original_failure.extractor,
                    attempts=original_failure.attempts,
                    fallback_attempted=original_failure.fallback_attempted,
                    fallback_reason=original_failure.fallback_reason,
                    error_type=original_failure.error_type,
                    error_code=original_failure.error_code,
                    error_message=original_failure.error_message,
                    same_article_recovery=same_recovery.audit,
                    syndicated_recovery=recovery.audit,
                    alternate_reporting_recovery=alternate_recovery.audit,
                )
                candidate.summary = article_fetch_failure_summary(candidate)
                candidate.summary_basis = "none"
                candidate.summary_status = "skipped"
                candidate.summary_generation = SummaryGeneration(status="skipped")
                LOGGER.error(
                    "component=article_fetch item_id=%s status=failed method=%s "
                    "extractor=%s error=%s code=%s attempts=%d message=%s",
                    candidate.story.hn_item_id,
                    original_failure.method,
                    original_failure.extractor or "none",
                    original_failure.error_type,
                    original_failure.error_code,
                    original_failure.attempts,
                    original_failure.error_message,
                )
                return False

            material = recovered_material
            candidate.story = replace(candidate.story, fetched_text=material.text)
            candidate.article_retrieval = ArticleRetrieval(
                status="success",
                method=material.method,
                extractor=material.extractor,
                attempts=material.attempts,
                fallback_attempted=bool(material.fallback_reason),
                fallback_reason=material.fallback_reason,
                retrieved_url=material.retrieved_url,
                material_origin=material_origin,
                origin_failure=original_failure,
                same_article_recovery=same_recovery.audit,
                syndicated_recovery=recovery.audit,
                alternate_reporting_recovery=alternate_recovery.audit,
            )
            candidate.summary_basis = (
                "youtube_caption" if material.method == "youtube_caption" else "fetched_article"
            )
            LOGGER.info(
                "component=article_fetch item_id=%s status=success "
                "material_origin=%s method=%s extractor=%s "
                "fallback_reason=%s attempts=%d",
                candidate.story.hn_item_id,
                material_origin,
                material.method,
                material.extractor or "none",
                material.fallback_reason or "none",
                material.attempts,
            )
            return True

        candidate.story = replace(candidate.story, fetched_text=material.text)
        candidate.article_retrieval = ArticleRetrieval(
            status="success",
            method=material.method,
            extractor=material.extractor,
            attempts=material.attempts,
            fallback_attempted=bool(material.fallback_reason),
            fallback_reason=material.fallback_reason,
            retrieved_url=material.retrieved_url,
            material_origin=material.material_origin,
        )
        candidate.summary_basis = (
            "youtube_caption"
            if material.method == "youtube_caption"
            else "fetched_article"
        )
        LOGGER.info(
            "component=article_fetch item_id=%s status=success method=%s "
            "extractor=%s fallback_reason=%s attempts=%d",
            candidate.story.hn_item_id,
            material.method,
            material.extractor or "none",
            material.fallback_reason or "none",
            material.attempts,
        )
        return True

    if candidate.story.story_text.strip():
        candidate.article_retrieval = ArticleRetrieval(
            status="not_needed",
            method="story_text",
            extractor="plain_text",
        )
        candidate.summary_basis = "story_text"
    else:
        candidate.article_retrieval = ArticleRetrieval(
            status="not_needed",
            method="title",
        )
        candidate.summary_basis = "title_only"
    return True


def prepare_hn_discussion_material(
    candidate: Candidate,
    discussion_fetcher,
) -> bool:
    active_fetcher = discussion_fetcher or fetch_hn_discussion
    try:
        result = active_fetcher(candidate.story.hn_item_id)
        if not isinstance(result, HNDiscussionResult):
            raise TypeError("discussion fetcher returned an invalid result")
    except Exception as exc:
        candidate.discussion_retrieval = DiscussionRetrieval(
            status="failed",
            error_type=type(exc).__name__,
            error_code=(
                exc.error_code
                if isinstance(exc, HNDiscussionFetchError)
                else "unexpected_error"
            ),
            error_message=bounded_error_message(exc),
        )
        LOGGER.error(
            "component=hn_discussion_fetch item_id=%s status=failed "
            "error=%s code=%s message=%s",
            candidate.story.hn_item_id,
            candidate.discussion_retrieval.error_type,
            candidate.discussion_retrieval.error_code,
            candidate.discussion_retrieval.error_message,
        )
        return False

    status = (
        "success"
        if result.comments >= MIN_DISCUSSION_COMMENTS
        and result.chars >= MIN_DISCUSSION_CHARS
        else "insufficient"
    )
    candidate.discussion_retrieval = DiscussionRetrieval(
        status=status,
        comments=result.comments,
        chars=result.chars,
        requested_items=result.requested_items,
        failed_items=result.failed_items,
        error_code="" if status == "success" else "insufficient_comments",
    )
    if status != "success":
        LOGGER.info(
            "component=hn_discussion_fetch item_id=%s status=insufficient "
            "comments=%d chars=%d requested_items=%d failed_items=%d",
            candidate.story.hn_item_id,
            result.comments,
            result.chars,
            result.requested_items,
            result.failed_items,
        )
        return False

    candidate.discussion_text = result.text
    candidate.summary_basis = "hn_comments"
    candidate.summary_status = "not_generated"
    candidate.summary_generation = SummaryGeneration()
    LOGGER.info(
        "component=hn_discussion_fetch item_id=%s status=success "
        "comments=%d chars=%d requested_items=%d failed_items=%d",
        candidate.story.hn_item_id,
        result.comments,
        result.chars,
        result.requested_items,
        result.failed_items,
    )
    return True


def bounded_error_message(exc: Exception, max_chars: int = 500) -> str:
    return " ".join(str(exc).split())[:max_chars]


def _wayback_not_before(created_at: str, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return default
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return default
    return parsed - timedelta(days=1)


def _retrieval_failure(exc: Exception) -> RetrievalFailure:
    return RetrievalFailure(
        method=getattr(exc, "method", "") or "direct",
        extractor=getattr(exc, "extractor", ""),
        attempts=getattr(exc, "attempts", 1),
        fallback_attempted=getattr(exc, "fallback_attempted", False),
        fallback_reason=getattr(exc, "fallback_reason", ""),
        error_type=type(exc).__name__,
        error_code=getattr(exc, "error_code", "fetch_failed"),
        error_message=bounded_error_message(exc),
    )


def _should_attempt_reuters_recovery(
    candidate: Candidate,
    failure: RetrievalFailure,
) -> bool:
    return (
        is_reuters_url(candidate.story.source_url)
        and failure.method in {"jina", "wayback"}
        and failure.fallback_attempted
        and failure.fallback_reason == "datadome_challenge"
    )


def _should_attempt_alternate_reporting(
    candidate: Candidate,
    failure: RetrievalFailure,
) -> bool:
    return (
        not is_reuters_url(candidate.story.source_url)
        and failure.fallback_attempted is True
        and is_origin_block_reason(failure.fallback_reason)
    )
