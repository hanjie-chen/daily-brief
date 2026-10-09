"""Refresh existing brief items from the network without persisting raw material."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from pathlib import Path

from ..candidates import fetch_hn_story
from ..config import RUN_HOUR, TIMEZONE
from ..llm import create_model_backend
from ..models import ArticleRetrieval, Candidate, Story, SummaryGeneration
from ..output import (
    render_candidates_json,
    render_markdown,
    render_public_brief_json,
    validate_public_brief,
)
from ..output.artifacts import atomic_write_text
from ..time_window import daily_window
from .material import (
    bounded_error_message,
    prepare_candidate_material,
    prepare_hn_discussion_material,
)
from .summaries import (
    generate_candidate_summary,
    prepare_summary_context,
    summarize_selected_candidates,
)

LOGGER = logging.getLogger(__name__)
_DIAGNOSTICS = (
    "article_retrieval", "discussion_retrieval", "summary_mode", "summary_context",
    "summary_input_mode", "summary_sources_used", "summary_basis", "summary_status",
    "source_material", "summary_generation", "content_reason", "hn_post_retrieval",
)


class RetryError(ValueError):
    """The saved brief cannot safely be retried."""


@dataclass(frozen=True)
class RetryResult:
    attempted: int
    updated: int
    failed: int


def run_retry(
    output_dir,
    data_dir,
    *,
    date_label: str,
    item_ids: list[str] | None = None,
    model_backend=None,
    hn_story_fetcher=None,
    article_fetcher=None,
    hn_discussion_fetcher=None,
    syndicated_finder=None,
    alternate_reporting_finder=None,
    same_article_finder=None,
) -> RetryResult:
    """Retry failures, or explicitly named selected items, from fresh material.

    Selection, ordering, links, title, scores, history and publication state remain
    unchanged. Only successful attempts replace summaries; latest diagnostics are
    retained even on failure. Raw source text and model inputs stay in memory.
    """
    try:
        target_date = date.fromisoformat(date_label)
    except (ValueError, TypeError) as exc:
        raise RetryError("retry requires a date in YYYY-MM-DD format") from exc
    if target_date.isoformat() != date_label:
        raise RetryError("retry requires a date in YYYY-MM-DD format")
    if item_ids is not None and (
        not item_ids or any(not isinstance(i, str) or not i.isascii()
                            or not i.isdigit() or int(i) <= 0 for i in item_ids)
    ):
        raise RetryError("retry item IDs must be positive integers")

    public_path = Path(output_dir) / f"{date_label}.json"
    markdown_path = Path(output_dir) / f"{date_label}.md"
    audit_path = Path(data_dir) / f"{date_label}-hn-candidates.json"
    originals = {path: _read_text(path) for path in (public_path, audit_path)}
    originals[markdown_path] = _read_text(markdown_path) if markdown_path.exists() else None
    try:
        public = json.loads(originals[public_path])
        audit = json.loads(originals[audit_path])
        validate_public_brief(public)
    except (ValueError, TypeError) as exc:
        raise RetryError("retry requires a valid public brief and candidate audit") from exc
    if public["date"] != date_label:
        raise RetryError("saved brief date does not match requested date")
    selected, records = _index_saved_items(public, audit)
    if item_ids is not None and set(item_ids) - selected.keys():
        raise RetryError("requested item is not selected in this saved brief")
    targets = [item_id for item_id, (_, item) in selected.items()
               if (item_id in item_ids if item_ids is not None
                   else _needs_retry(item, records[item_id]))]
    if not targets:
        return RetryResult(0, 0, 0)

    backend = model_backend or create_model_backend()
    fetch_story = hn_story_fetcher or fetch_hn_story
    window = daily_window(datetime.combine(target_date, time(RUN_HOUR), tzinfo=TIMEZONE))
    updated = 0
    for item_id in targets:
        section, old_item = selected[item_id]
        record = records[item_id]
        candidate = _candidate(old_item, record, section)
        post_retrieval = {"status": "success", "error_code": "", "error_message": ""}
        try:
            fresh_story = fetch_story(item_id)
            if not isinstance(fresh_story, Story) or fresh_story.hn_item_id != item_id:
                raise ValueError("HN story fetch returned a mismatched item")
            candidate.story = replace(candidate.story, story_text=fresh_story.story_text)
            candidate.hn_post_retrieval_status = "success" if fresh_story.story_text.strip() else "empty"
        except Exception as exc:
            candidate.hn_post_retrieval_status = "failed"
            candidate.hn_post_retrieval_error_code = getattr(exc, "error_code", "hn_post_fetch_failed")
            post_retrieval = {
                "status": "failed",
                "error_code": getattr(exc, "error_code", "hn_post_fetch_failed"),
                "error_message": bounded_error_message(exc),
            }

        if candidate.content_kind == "community_roundup":
            prepare_candidate_material(candidate, article_fetcher, syndicated_finder,
                                       alternate_reporting_finder, window,
                                       same_article_finder=same_article_finder)
            if prepare_hn_discussion_material(candidate, hn_discussion_fetcher):
                prepare_summary_context(candidate)
                generate_candidate_summary(candidate, backend)
        else:
            summarize_selected_candidates(
                [candidate] if section == "ai" else [],
                [candidate] if section == "non_ai_hot" else [],
                backend, article_fetcher, hn_discussion_fetcher,
                syndicated_finder, alternate_reporting_finder, window,
                same_article_finder=same_article_finder,
            )

        new_record = json.loads(render_candidates_json([candidate]))[0]
        attempt = {"attempted_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
                   "status": "failed", "hn_post_retrieval": post_retrieval}
        attempt.update({key: new_record[key] for key in _DIAGNOSTICS})
        # Preserve the richer existing retry-only request diagnostic.
        attempt["hn_post_retrieval"] = post_retrieval
        if candidate.summary_status == "success":
            # Validate each replacement before admitting it to the saved brief.
            replacement_brief = json.loads(render_public_brief_json(
                date_label, public["generated_at"],
                [candidate] if section == "ai" else [],
                [candidate] if section == "non_ai_hot" else [],
            ))
            try:
                validate_public_brief(replacement_brief)
            except ValueError as exc:
                attempt["error_code"] = "invalid_public_summary"
                attempt["error_message"] = bounded_error_message(exc)
            else:
                old_item.update(replacement_brief["sections"][section]["items"][0])
                record.update({key: new_record[key] for key in _DIAGNOSTICS})
                attempt["status"] = "updated"
                updated += 1
        record["last_retry"] = attempt
        LOGGER.info("component=retry item_id=%s status=%s", item_id, attempt["status"])

    changes = {audit_path: _json_text(audit)}
    if updated:
        public["generated_at"] = datetime.now(TIMEZONE).isoformat(timespec="seconds")
        validate_public_brief(public)
        changes[public_path] = _json_text(public)
        changes[markdown_path] = _render_saved_markdown(public, records)
    _persist_updates(changes, originals)
    return RetryResult(len(targets), updated, len(targets) - updated)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RetryError(f"cannot read retry input: {path}") from exc


def _index_saved_items(public: dict, audit) -> tuple[dict, dict]:
    if not isinstance(audit, list) or any(not isinstance(row, dict) for row in audit):
        raise RetryError("candidate audit must be an array of objects")
    records = {}
    for row in audit:
        item_id = row.get("hn_item_id")
        if not isinstance(item_id, str) or item_id in records:
            raise RetryError("candidate audit contains invalid or duplicate item IDs")
        records[item_id] = row
    selected = {}
    for section, contents in public["sections"].items():
        for item in contents["items"]:
            item_id = item["hn_item_id"]
            if item_id in selected or item_id not in records:
                raise RetryError("selected item is duplicated or missing from candidate audit")
            record = records[item_id]
            if (record.get("selected") is not True or record.get("section") != section
                    or record.get("source_url") != item["source_url"]
                    or record.get("hn_discussion_url") != item["discussion_url"]):
                raise RetryError("public brief and candidate audit disagree on selected item")
            if record.get("content_kind", "article") not in {"article", "community_roundup"}:
                raise RetryError("unsupported saved content kind")
            for key in ("article_retrieval", "discussion_retrieval", "source_material",
                        "summary_generation", "last_retry"):
                if not isinstance(record.get(key, {}), dict):
                    raise RetryError(f"invalid candidate audit field: {key}")
            if not isinstance(record.get("last_retry", {}).get("hn_post_retrieval", {}), dict):
                raise RetryError("invalid retry HN post diagnostics")
            if record.get("content_kind") == "community_roundup" and (
                item["source_url"] != item["discussion_url"]
            ):
                raise RetryError("saved community roundup must be a self-post")
            selected[item_id] = (section, item)
    if {row["hn_item_id"] for row in audit if row.get("selected") is True} != set(selected):
        raise RetryError("public brief and candidate audit disagree on selected IDs")
    return selected, records


def _needs_retry(item: dict, record: dict) -> bool:
    return (
        item["content_status"] != "ok"
        or record.get("summary_status") in {"failed", "insufficient", "skipped", "not_generated"}
        or record.get("article_retrieval", {}).get("status") == "failed"
        or record.get("discussion_retrieval", {}).get("status") == "failed"
        or record.get("last_retry", {}).get("hn_post_retrieval", {}).get("status") == "failed"
    )


def _candidate(item: dict, record: dict, section: str) -> Candidate:
    return Candidate(
        story=Story(source=record.get("source", "hn_official"), hn_item_id=item["hn_item_id"],
                    title=item["title"], source_url=item["source_url"],
                    hn_discussion_url=item["discussion_url"],
                    created_at=record.get("created_at", ""), points=item["points"],
                    comments=item["comments"]),
        content_kind=record.get("content_kind", "article"), selected=True,
        section=section, why=item["why"],
    )


def _render_saved_markdown(public: dict, records: dict) -> str:
    sections = {}
    for section, contents in public["sections"].items():
        sections[section] = []
        for item in contents["items"]:
            record = records[item["hn_item_id"]]
            candidate = _candidate(item, record, section)
            candidate.summary = item["summary"]
            candidate.summary_status = record.get("summary_status", "success")
            candidate.summary_basis = record.get("summary_basis", "unknown")
            candidate.summary_input_mode = record.get("summary_input_mode", "legacy")
            candidate.source_material_status = record.get("source_material", {}).get("status", "not_assessed")
            retrieval = record.get("article_retrieval", {})
            candidate.article_retrieval = ArticleRetrieval(
                status=retrieval.get("status", "not_attempted"),
                fallback_reason=retrieval.get("fallback_reason", ""),
                error_code=retrieval.get("error_code", ""),
            )
            candidate.summary_generation = SummaryGeneration(
                error_code=record.get("summary_generation", {}).get("error_code", "")
            )
            sections[section].append(candidate)
    return render_markdown(public["date"], sections["ai"], sections["non_ai_hot"],
                           public["sections"]["ai"]["note"], public["sections"]["non_ai_hot"]["note"])


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _persist_updates(changes: dict, originals: dict) -> None:
    # A retry can take minutes. Do not overwrite edits or another generation that
    # completed while external services were being called.
    for path, original in originals.items():
        current = _read_text(path) if path.exists() else None
        if current != original:
            raise RetryError("brief changed during retry; no retry results were written")
    written = []
    try:
        for path, content in changes.items():
            atomic_write_text(path, content)
            written.append(path)
    except OSError as exc:
        for path in reversed(written):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, original)
        raise RetryError("could not save retry results") from exc
