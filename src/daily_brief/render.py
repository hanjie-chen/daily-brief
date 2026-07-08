from __future__ import annotations

import json

from .models import Candidate


def render_markdown(date_label: str, ai_items: list[Candidate], hot_items: list[Candidate]) -> str:
    lines = [f"# Daily Brief - {date_label}", ""]
    lines.extend(_render_section("Hacker News: AI", ai_items))
    lines.extend(_render_section("Hacker News: Non-AI Hot", hot_items))
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
                "matched_keywords": [match.keyword for match in candidate.matched_keywords],
                "score": round(candidate.score, 4),
                "selected": candidate.selected,
                "section": candidate.section,
                "rejection_reason": candidate.rejection_reason,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _render_section(title: str, items: list[Candidate]) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.extend(["No items selected.", ""])
        return lines

    for item in items:
        story = item.story
        lines.extend(
            [
                f"### {story.title}",
                "",
                f"- Summary: {item.summary}",
                f"- Why: {item.why}",
                f"- Source: {story.source_url}",
                f"- Discussion: {story.hn_discussion_url}",
                f"- Stats: {story.points} points / {story.comments} comments",
                "",
            ]
        )
    return lines
