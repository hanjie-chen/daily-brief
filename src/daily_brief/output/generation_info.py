"""Bounded public generation diagnostics; raw diagnostics stay in the audit."""
from __future__ import annotations

from ..models import Candidate
from .public_schema import (
    GENERATION_REASONS, GENERATION_STATUSES, MATERIAL_STATUSES, MODEL_IDENTIFIER,
    PROVENANCE_VALUES, REASONING_EFFORTS, SUMMARY_SOURCES,
)


def _reason(code: str, http_status: int | None = None, *, model_service: bool = False) -> str:
    if code.startswith("http_") and code[5:].isdigit():
        http_status = int(code[5:])
    # A recorded HTTP response is more specific than a generic provider error.
    if http_status == 429:
        return "rate_limited"
    if http_status == 408:
        return "network_timeout"
    if isinstance(http_status, int) and 400 <= http_status <= 599:
        if model_service and http_status in {401, 403}:
            return "authentication_failed"
        if model_service and 500 <= http_status <= 599:
            return "provider_unavailable"
        return "http_error"
    if not code:
        return "none"
    if code in GENERATION_REASONS:
        return code
    aliases = {
        "timeout": "network_timeout", "quota_exceeded": "rate_limited",
        "daily_quota_exceeded": "rate_limited", "budget_exceeded": "rate_limited",
        "insufficient_comments": "source_material_insufficient",
        "html_extraction_failed": "extraction_failed", "pdf_extraction_failed": "extraction_failed",
        "invalid_json": "invalid_response",
    }
    return aliases.get(code, "unknown")


def public_generation_info(candidate: Candidate) -> dict:
    retrieval = candidate.article_retrieval
    page_status = retrieval.status if retrieval.status in MATERIAL_STATUSES else "unknown"
    if page_status == "failed" and retrieval.error_code == "empty_content":
        page_status = "empty"
    page_method = retrieval.method
    if page_method in {"story_text", "title"}:
        page_method = "none"
    page_method = page_method if page_method in PROVENANCE_VALUES["retrieval_method"] else "unknown"
    origin = retrieval.material_origin
    origin = origin if origin in PROVENANCE_VALUES["material_origin"] else "unknown"
    origin_failure = retrieval.origin_failure
    if page_status == "success":
        page_reason = _reason(origin_failure.fallback_reason if origin_failure else retrieval.fallback_reason)
    elif page_status in {"failed", "empty"}:
        page_reason = _reason(retrieval.error_code or retrieval.fallback_reason or "unknown")
    else:
        page_reason = "unknown" if page_status == "unknown" else "none"

    post_status = candidate.hn_post_retrieval_status
    post_status = post_status if post_status in MATERIAL_STATUSES else "unknown"
    post_reason = _reason(candidate.hn_post_retrieval_error_code or "unknown") if post_status == "failed" else (
        "unknown" if post_status == "unknown" else "none"
    )
    discussion = candidate.discussion_retrieval
    comment_status = discussion.status
    if comment_status == "insufficient":
        # Insufficient for roundup qualification is not an acquisition failure.
        comment_status = "success" if discussion.chars > 0 else "empty"
    elif comment_status == "success" and discussion.chars == 0 and not candidate.discussion_text.strip():
        comment_status = "empty"
    comment_status = comment_status if comment_status in MATERIAL_STATUSES else "unknown"
    comment_reason = _reason(discussion.error_code or "unknown") if comment_status == "failed" else (
        "unknown" if comment_status == "unknown" else "none"
    )

    generation = candidate.summary_generation
    status = candidate.summary_status if candidate.summary_status in {"success", "insufficient", "failed"} else generation.status
    if status == "skipped":
        status = "not_attempted"
    status = status if status in GENERATION_STATUSES else "unknown"
    model = generation.model
    model = model if isinstance(model, str) and MODEL_IDENTIFIER.fullmatch(model) else None
    if status == "insufficient":
        reason = "source_material_insufficient"
    elif status == "failed":
        reason = _reason(generation.error_code or "unknown", generation.http_status, model_service=True)
    elif generation.status == "skipped":
        reason = "no_materials"
    else:
        reason = "unknown" if status == "unknown" else "none"
    sources = candidate.summary_sources_used
    if status == "success":
        sources = list(sources) if (isinstance(sources, list) and 0 < len(sources) <= 4
                                  and all(isinstance(source, str) and source in SUMMARY_SOURCES for source in sources)
                                  and len(set(sources)) == len(sources)) else None
    else:
        sources = None if status == "unknown" else []
    public_generation = {"status": status, "model": model, "reason": reason}
    effort = generation.reasoning_effort
    if isinstance(effort, str) and effort in REASONING_EFFORTS:
        public_generation["reasoning_effort"] = effort
    return {
        "materials": {
            "webpage": {"status": page_status, "method": page_method, "origin": origin, "reason": page_reason},
            "hn_post": {"status": post_status, "reason": post_reason},
            "hn_comments": {"status": comment_status, "reason": comment_reason},
        },
        "summary_sources": sources,
        "generation": public_generation,
    }
