from __future__ import annotations

import logging
from copy import deepcopy

from ..alternate_reporting import AlternateReportingFinder
from ..models import Candidate, SummaryGeneration
from ..same_article import SameArticleFinder
from ..summarizer import (
    InsufficientSummaryMaterial,
    article_fetch_failure_summary,
    build_summary_context,
    fallback_summary,
    has_discussion_source,
    ALTERNATE_REPORTING_SUMMARY_PREFIX,
    HN_DISCUSSION_SUMMARY_PREFIX,
    normalize_summary_text,
    route_summary_mode,
)
from ..syndicated_copy import SyndicatedCopyFinder
from ..time_window import TimeWindow
from .material import (
    RETRIEVAL_MODE_SUMMARY,
    bounded_error_message,
    prepare_candidate_material,
    prepare_hn_discussion_material,
)

LOGGER = logging.getLogger(__name__)


def prepare_summary_context(candidate: Candidate) -> None:
    candidate.summary_mode = route_summary_mode(candidate)
    context = build_summary_context(candidate)
    candidate.summary_context_strategy = context.strategy
    candidate.summary_context_source_chars = context.source_chars
    candidate.summary_context_selected_chars = context.selected_chars
    candidate.summary_context_sections = list(context.sections)


def summarize_selected_candidates(
    ai_items: list[Candidate],
    selected_hot_items: list[Candidate],
    summary_client,
    article_fetcher,
    hn_discussion_fetcher,
    syndicated_finder: SyndicatedCopyFinder | None,
    alternate_reporting_finder: AlternateReportingFinder | None,
    window: TimeWindow,
    same_article_finder: SameArticleFinder | None = None,
) -> list[Candidate]:
    summary_candidates = [*ai_items, *selected_hot_items]
    summarization_inputs: list[Candidate] = []
    for candidate in summary_candidates:
        if candidate.content_kind == "community_roundup":
            # Already assessed before selection; reuse the successful overview.
            continue
        if candidate.article_retrieval.status == "not_attempted":
            prepare_candidate_material(
                candidate,
                article_fetcher,
                syndicated_finder,
                alternate_reporting_finder,
                window,
                retrieval_mode=RETRIEVAL_MODE_SUMMARY,
                same_article_finder=same_article_finder,
            )
        if candidate.article_retrieval.status == "failed":
            if not prepare_hn_discussion_material(
                candidate,
                hn_discussion_fetcher,
            ):
                candidate.summary_generation = SummaryGeneration(status="skipped")
                continue
        # At most one source call and one discussion call; never recurse on comments.
        for _ in range(2):
            prepare_summary_context(candidate)
            summarization_inputs.append(deepcopy(candidate))
            insufficient = generate_candidate_summary(candidate, summary_client)
            if not insufficient or candidate.summary_basis == "hn_comments":
                break
            if not prepare_hn_discussion_material(candidate, hn_discussion_fetcher):
                break

    return summarization_inputs


def _unavailable_material_summary(candidate: Candidate) -> str:
    if candidate.source_material_status == "insufficient":
        return "页面材料不足，未生成可靠摘要；请查看原文或讨论。"
    return article_fetch_failure_summary(candidate)


def generate_candidate_summary(candidate: Candidate, summary_client) -> bool:
    """Return whether the model explicitly found the supplied material insufficient."""
    provider = _summary_provider(summary_client)
    try:
        candidate.summary = normalize_summary_text(
            summary_client.summarize(candidate)
        )
        if not candidate.summary:
            raise ValueError("summarizer returned an empty summary")
        if candidate.summary_basis == "hn_comments":
            if candidate.content_kind == "community_roundup":
                candidate.summary = "根据 Hacker News 部分评论：" + candidate.summary
            elif not has_discussion_source(candidate):
                candidate.summary = HN_DISCUSSION_SUMMARY_PREFIX + candidate.summary
        elif candidate.article_retrieval.material_origin == "alternate_reporting":
            candidate.summary = (
                ALTERNATE_REPORTING_SUMMARY_PREFIX + candidate.summary
            )
        if candidate.summary_basis != "hn_comments":
            candidate.source_material_status = "sufficient"
        candidate.summary_status = "success"
        model = _summary_model(summary_client)
        summary_usage = _summary_usage(summary_client)
        candidate.summary_generation = SummaryGeneration(
            status="success",
            provider=provider,
            model=model,
            attempts=_summary_attempts(summary_client),
            provider_status=_summary_provider_status(summary_client),
            **summary_usage,
        )
        LOGGER.info(
            "component=summary_generation item_id=%s status=success "
            "provider=%s model=%s attempts=%d",
            candidate.story.hn_item_id,
            provider or "unknown",
            model or "unknown",
            candidate.summary_generation.attempts,
        )
    except InsufficientSummaryMaterial as exc:
        model = _summary_model(summary_client)
        if candidate.content_kind == "community_roundup":
            candidate.content_reason = exc.reason
        candidate.summary_generation = SummaryGeneration(
            status="insufficient",
            provider=provider,
            model=model,
            attempts=_summary_attempts(summary_client),
            provider_status=_summary_provider_status(summary_client),
            **_summary_usage(summary_client),
        )
        if candidate.summary_basis != "hn_comments":
            candidate.source_material_status = "insufficient"
            candidate.source_material_reason = exc.reason
            candidate.source_summary_generation = deepcopy(candidate.summary_generation)
        candidate.summary = _unavailable_material_summary(candidate)
        candidate.summary_status = "insufficient"
        LOGGER.info(
            "component=summary_generation item_id=%s status=insufficient basis=%s",
            candidate.story.hn_item_id, candidate.summary_basis,
        )
        return True
    except Exception as exc:
        model = _summary_model(summary_client)
        error_message = bounded_error_message(exc)
        summary_usage = _summary_usage(summary_client)
        candidate.summary_generation = SummaryGeneration(
            status="failed",
            provider=provider,
            model=model,
            attempts=_summary_attempts(summary_client),
            provider_status=_summary_provider_status(summary_client, exc),
            **summary_usage,
            error_type=type(exc).__name__,
            error_code=_summary_error_code(exc),
            http_status=_summary_http_status(exc),
            error_message=error_message,
        )
        LOGGER.error(
            "component=summary_generation item_id=%s status=failed "
            "provider=%s model=%s attempts=%d error=%s code=%s "
            "http_status=%s message=%s",
            candidate.story.hn_item_id,
            provider or "unknown",
            model or "unknown",
            candidate.summary_generation.attempts,
            candidate.summary_generation.error_type,
            candidate.summary_generation.error_code,
            candidate.summary_generation.http_status or "none",
            error_message,
        )
        candidate.summary = (
            _unavailable_material_summary(candidate)
            if candidate.summary_basis == "hn_comments"
            else fallback_summary(candidate)
        )
        candidate.summary_status = "failed"

    return False


def _summary_provider(summary_client) -> str:
    provider = getattr(summary_client, "name", "")
    if not isinstance(provider, str) or not provider.strip():
        provider = type(summary_client).__name__
    return " ".join(provider.split())[:128]


def _summary_model(summary_client) -> str:
    model = getattr(
        summary_client, "last_summary_model",
        getattr(summary_client, "summarizer_model", ""),
    )
    if not isinstance(model, str):
        return ""
    return " ".join(model.split())[:128]


def _summary_attempts(summary_client) -> int:
    attempts = getattr(summary_client, "last_summary_attempts", 1)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return 1
    return attempts


def _summary_provider_status(summary_client, exc: Exception | None = None) -> str:
    status = getattr(exc, "provider_status", "") if exc is not None else ""
    if not isinstance(status, str) or not status.strip():
        status = getattr(summary_client, "last_summary_provider_status", "")
    if not isinstance(status, str) or not status.strip():
        return ""
    return " ".join(status.split())[:128]


def _summary_usage(summary_client) -> dict[str, int | None]:
    usage = getattr(summary_client, "last_summary_usage", {})
    if not isinstance(usage, dict):
        usage = {}
    return {
        name: _summary_token_count(usage.get(name))
        for name in (
            "input_tokens",
            "output_tokens",
            "thought_tokens",
            "total_tokens",
        )
    }


def _summary_token_count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _summary_error_code(exc: Exception) -> str:
    error_code = getattr(exc, "error_code", "")
    if not isinstance(error_code, str) or not error_code.strip():
        return "unexpected_error"
    return " ".join(error_code.split())[:128]


def _summary_http_status(exc: Exception) -> int | None:
    http_status = getattr(exc, "http_status", None)
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        return None
    if not 100 <= http_status <= 599:
        return None
    return http_status
