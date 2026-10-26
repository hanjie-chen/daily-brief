from __future__ import annotations

import json
from dataclasses import asdict

from ..models import Candidate
from .public_schema import PUBLIC_BRIEF_SCHEMA_VERSION
from .generation_info import public_generation_info


def render_candidates_json(candidates: list[Candidate]) -> str:
    payload = []
    for candidate in candidates:
        story = candidate.story
        alternate_recovery = (
            candidate.article_retrieval.alternate_reporting_recovery
        )
        payload.append(
            {
                "source": story.source,
                "hn_item_id": story.hn_item_id,
                "title": story.title,
                "source_url": story.source_url,
                "hn_discussion_url": story.hn_discussion_url,
                "created_at": story.created_at,
                "summary_reference_date": candidate.summary_reference_date,
                "webpage_published_at": candidate.webpage_published_at,
                "points": story.points,
                "comments": story.comments,
                "matched_keywords": [
                    match.keyword for match in candidate.matched_keywords
                ],
                "topic_route": candidate.topic_route,
                "content_kind": candidate.content_kind,
                "content_reason": candidate.content_reason,
                "summary_mode": candidate.summary_mode,
                "summary_context": {
                    "strategy": candidate.summary_context_strategy,
                    "source_chars": candidate.summary_context_source_chars,
                    "selected_chars": candidate.summary_context_selected_chars,
                    "sections": candidate.summary_context_sections,
                },
                "score": round(candidate.score, 4),
                "selected": candidate.selected,
                "section": candidate.section,
                "rejection_reason": candidate.rejection_reason,
                "content_deduplication": candidate.content_deduplication,
                "article_retrieval": {
                    "status": candidate.article_retrieval.status,
                    "method": candidate.article_retrieval.method,
                    "extractor": candidate.article_retrieval.extractor,
                    "attempts": candidate.article_retrieval.attempts,
                    "fallback_attempted": (
                        candidate.article_retrieval.fallback_attempted
                    ),
                    "fallback_reason": candidate.article_retrieval.fallback_reason,
                    "error_type": candidate.article_retrieval.error_type,
                    "error_code": candidate.article_retrieval.error_code,
                    "error_message": candidate.article_retrieval.error_message,
                    "retrieved_url": candidate.article_retrieval.retrieved_url,
                    "material_origin": candidate.article_retrieval.material_origin,
                    "same_article_recovery": asdict(
                        candidate.article_retrieval.same_article_recovery
                    ),
                    "origin_failure": _retrieval_failure_payload(
                        candidate.article_retrieval.origin_failure
                    ),
                    "syndicated_recovery": {
                        "status": (
                            candidate.article_retrieval.syndicated_recovery.status
                        ),
                        "provider": (
                            candidate.article_retrieval.syndicated_recovery.provider
                        ),
                        "discovered_candidates": (
                            candidate.article_retrieval.syndicated_recovery.discovered_candidates
                        ),
                        "attempted_candidates": (
                            candidate.article_retrieval.syndicated_recovery.attempted_candidates
                        ),
                        "rejection_reasons": (
                            candidate.article_retrieval.syndicated_recovery.rejection_reasons
                        ),
                        "error_code": (
                            candidate.article_retrieval.syndicated_recovery.error_code
                        ),
                    },
                    "alternate_reporting_recovery": {
                        "status": alternate_recovery.status,
                        "provider": alternate_recovery.provider,
                        "discovered_candidates": (
                            alternate_recovery.discovered_candidates
                        ),
                        "attempted_candidates": alternate_recovery.attempted_candidates,
                        "rejection_reasons": alternate_recovery.rejection_reasons,
                        "error_code": alternate_recovery.error_code,
                    },
                },
                "hn_post_retrieval": {
                    "status": candidate.hn_post_retrieval_status,
                    "error_code": candidate.hn_post_retrieval_error_code,
                },
                "discussion_retrieval": {
                    "status": candidate.discussion_retrieval.status,
                    "comments": candidate.discussion_retrieval.comments,
                    "chars": candidate.discussion_retrieval.chars,
                    "requested_items": (
                        candidate.discussion_retrieval.requested_items
                    ),
                    "failed_items": candidate.discussion_retrieval.failed_items,
                    "top_level_comments": candidate.discussion_retrieval.top_level_comments,
                    "reply_comments": candidate.discussion_retrieval.reply_comments,
                    "stop_reason": candidate.discussion_retrieval.stop_reason,
                    "error_type": candidate.discussion_retrieval.error_type,
                    "error_code": candidate.discussion_retrieval.error_code,
                    "error_message": candidate.discussion_retrieval.error_message,
                },
                "summary_basis": candidate.summary_basis,
                "summary_input_mode": candidate.summary_input_mode,
                "summary_sources_used": candidate.summary_sources_used,
                "summary_status": candidate.summary_status,
                "source_material": {
                    "status": candidate.source_material_status,
                    "reason": candidate.source_material_reason,
                    "summary_generation": (
                        asdict(candidate.source_summary_generation)
                        if candidate.source_summary_generation is not None else None
                    ),
                },
                "summary_generation": {
                    "status": candidate.summary_generation.status,
                    "provider": candidate.summary_generation.provider,
                    "model": candidate.summary_generation.model,
                    "reasoning_effort": candidate.summary_generation.reasoning_effort,
                    "attempts": candidate.summary_generation.attempts,
                    "provider_status": (
                        candidate.summary_generation.provider_status
                    ),
                    "input_tokens": candidate.summary_generation.input_tokens,
                    "output_tokens": candidate.summary_generation.output_tokens,
                    "thought_tokens": candidate.summary_generation.thought_tokens,
                    "total_tokens": candidate.summary_generation.total_tokens,
                    "error_type": candidate.summary_generation.error_type,
                    "error_code": candidate.summary_generation.error_code,
                    "http_status": candidate.summary_generation.http_status,
                    "error_message": candidate.summary_generation.error_message,
                },
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def render_public_brief_json(
    date_label: str,
    generated_at: str,
    ai_items: list[Candidate],
    hot_items: list[Candidate],
    ai_note: str = "",
    hot_note: str = "",
) -> str:
    payload = {
        "schema_version": PUBLIC_BRIEF_SCHEMA_VERSION,
        "date": date_label,
        "generated_at": generated_at,
        "timezone": "Asia/Singapore",
        "sections": {
            "ai": {
                "note": ai_note,
                "items": [_public_item(item) for item in ai_items],
            },
            "non_ai_hot": {
                "note": hot_note,
                "items": [_public_item(item) for item in hot_items],
            },
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _single_line_display_text(value: str) -> str:
    return " ".join(value.split())


def _summary_display_text(candidate: Candidate) -> str:
    if candidate.content_kind == "community_roundup" and candidate.summary_status == "success":
        # The roundup adapter owns these line boundaries; preserve its list.
        return "\n".join(
            _single_line_display_text(line) for line in candidate.summary.strip().splitlines()
        )
    return _single_line_display_text(candidate.summary)


def _retrieval_failure_payload(failure):
    if failure is None:
        return None
    return {
        "method": failure.method,
        "extractor": failure.extractor,
        "attempts": failure.attempts,
        "fallback_attempted": failure.fallback_attempted,
        "fallback_reason": failure.fallback_reason,
        "error_type": failure.error_type,
        "error_code": failure.error_code,
        "error_message": failure.error_message,
    }


def _public_item(candidate: Candidate) -> dict:
    story = candidate.story
    return {
        "hn_item_id": story.hn_item_id,
        "title": _single_line_display_text(story.title),
        "summary": _summary_display_text(candidate),
        "content_status": _public_content_status(candidate),
        "why": _single_line_display_text(candidate.why),
        "source_url": story.source_url,
        "discussion_url": story.hn_discussion_url,
        "points": story.points,
        "comments": story.comments,
        "generation_info": public_generation_info(candidate),
    }


def _public_content_status(candidate: Candidate) -> str:
    if candidate.article_retrieval.status == "failed":
        return "fetch_failed"
    if candidate.summary_status in {"failed", "insufficient"}:
        return "summary_failed"
    if candidate.summary_basis == "title_only":
        return "title_only"
    return "ok"
