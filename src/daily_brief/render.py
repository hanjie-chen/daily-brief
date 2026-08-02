from __future__ import annotations

import json

from .models import Candidate


def render_markdown(
    date_label: str,
    ai_items: list[Candidate],
    hot_items: list[Candidate],
    ai_note: str = "",
    hot_note: str = "",
) -> str:
    lines = [f"# Daily Brief - {date_label}", ""]
    lines.extend(_render_section("Hacker News: AI", ai_items, ai_note))
    lines.extend(_render_section("Hacker News: Non-AI Hot", hot_items, hot_note))
    return "\n".join(lines).rstrip() + "\n"


def render_candidates_json(candidates: list[Candidate]) -> str:
    payload = []
    for candidate in candidates:
        story = candidate.story
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
                "score": round(candidate.score, 4),
                "selected": candidate.selected,
                "section": candidate.section,
                "rejection_reason": candidate.rejection_reason,
                "article_retrieval": {
                    "status": candidate.article_retrieval.status,
                    "method": candidate.article_retrieval.method,
                    "fallback_attempted": (
                        candidate.article_retrieval.fallback_attempted
                    ),
                    "fallback_reason": candidate.article_retrieval.fallback_reason,
                    "error_type": candidate.article_retrieval.error_type,
                    "error_code": candidate.article_retrieval.error_code,
                    "error_message": candidate.article_retrieval.error_message,
                },
                "summary_basis": candidate.summary_basis,
                "summary_status": candidate.summary_status,
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
        "schema_version": 1,
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
            error_code = item.article_retrieval.error_code or "fetch_failed"
            lines.append(
                "- Content: Error — 原文抓取失败"
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
