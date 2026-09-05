from __future__ import annotations

import json

from .models import Candidate
from .public_schema import PUBLIC_BRIEF_SCHEMA_VERSION


def render_markdown(
    date_label: str,
    ai_items: list[Candidate],
    hot_items: list[Candidate],
    ai_note: str = "",
    hot_note: str = "",
) -> str:
    lines = [f"# Daily Brief - {date_label}", ""]
    if not ai_items and not hot_items:
        lines.extend(["No publishable items selected today.", ""])
    lines.extend(_render_section("Hacker News: Tech picks", ai_items, ai_note))
    lines.extend(_render_section("Hacker News: Beyond the Bubble", hot_items, hot_note))
    return "\n".join(lines).rstrip() + "\n"


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
                "points": story.points,
                "comments": story.comments,
                "matched_keywords": [
                    match.keyword for match in candidate.matched_keywords
                ],
                "topic_route": candidate.topic_route,
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
                "summary_basis": candidate.summary_basis,
                "summary_status": candidate.summary_status,
                "summary_generation": {
                    "status": candidate.summary_generation.status,
                    "provider": candidate.summary_generation.provider,
                    "model": candidate.summary_generation.model,
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


def _render_section(title: str, items: list[Candidate], note: str = "") -> list[str]:
    lines = [f"## {title}", ""]
    if note:
        lines.extend([f"Note: {_single_line_display_text(note)}", ""])
    if not items:
        lines.extend(["No items selected.", ""])
        return lines

    for item in items:
        story = item.story
        lines.extend(
            [
                f"### {_single_line_display_text(story.title)}",
                "",
                f"- Summary: {_single_line_display_text(item.summary)}",
            ]
        )
        if item.article_retrieval.status == "failed":
            if item.article_retrieval.origin_blocked:
                lines.append("- Content: Error — 来源网站阻止自动抓取。")
            else:
                error_code = item.article_retrieval.error_code or "fetch_failed"
                lines.append(
                    "- Content: Error — 原文抓取失败"
                    f"（{_single_line_display_text(error_code)}）。"
                )
        elif item.summary_status == "failed":
            error_code = item.summary_generation.error_code or "summary_failed"
            lines.append(
                "- Content: Error — 原文已抓取，但摘要生成失败"
                f"（{_single_line_display_text(error_code)}）。"
            )
        lines.extend(
            [
                f"- Why: {_single_line_display_text(item.why)}",
                f"- Source: {story.source_url}",
                f"- Discussion: {story.hn_discussion_url}",
                f"- Stats: {story.points} points / {story.comments} comments",
                "",
            ]
        )
    return lines


def _single_line_display_text(value: str) -> str:
    return " ".join(value.split())


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
        "summary": _single_line_display_text(candidate.summary),
        "content_status": _public_content_status(candidate),
        "why": _single_line_display_text(candidate.why),
        "source_url": story.source_url,
        "discussion_url": story.hn_discussion_url,
        "points": story.points,
        "comments": story.comments,
    }


def _public_content_status(candidate: Candidate) -> str:
    if candidate.article_retrieval.status == "failed":
        return "fetch_failed"
    if candidate.summary_status == "failed":
        return "summary_failed"
    if candidate.summary_basis == "title_only":
        return "title_only"
    return "ok"
