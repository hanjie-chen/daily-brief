"""Conservative same-day comparison, followed by bounded same-section refills."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..config import AI_MAX_ITEMS, CONTENT_DEDUP_MAX_REPLACEMENTS, NON_AI_MAX_ITEMS
from ..llm import create_model_backend, ensure_content_decisions
from ..models import Candidate
from ..network_recovery import suspend_network_recovery
from .classification import SelectionResult

LOGGER = logging.getLogger(__name__)


def deduplicate_and_refill(
    selection: SelectionResult,
    deduplicator,
    summarize: Callable[[list[Candidate]], list[Candidate]],
) -> list[Candidate]:
    """Mutate final selection and return only new summary inputs for optional capture.

    Every round can replace a weaker item with a more informative representative,
    including a newly summarized reserve. Failed summaries are never compared;
    their existing reader-facing behavior is kept.
    Only qualified reserves are visited, without another classification pass.
    """
    summary_inputs: list[Candidate] = []
    pending = [*selection.ai_items, *selection.selected_hot_items]
    replacements = 0
    round_number = 0
    reserves = {
        "ai": iter(selection.ai_reserve),
        "non_ai_hot": iter(selection.hot_reserve),
    }
    while pending:
        round_number += 1
        compared = [
            item for item in [*selection.ai_items, *selection.selected_hot_items]
            if item.summary_status == "success" and item.summary.strip()
        ]
        pending_ids = {item.story.hn_item_id for item in pending}
        compared_ids = {item.story.hn_item_id for item in compared}
        for item in pending:
            if item.story.hn_item_id not in compared_ids:
                _record(item, round_number, "skipped", reason="summary_unavailable")
        if len(compared) < 2:
            for item in pending:
                if item.story.hn_item_id in compared_ids:
                    _record(item, round_number, "skipped", reason="fewer_than_two_summaries")
            break
        # Refills with only failed summaries cannot create a new comparison.
        if not pending_ids & compared_ids:
            break
        started = time.monotonic()
        try:
            deduplicator = deduplicator or create_model_backend()
            # This optional quality check must not spend the scheduled run's
            # ten-minute network pauses or prevent publication on exhaustion.
            with suspend_network_recovery():
                decisions = ensure_content_decisions(deduplicator.deduplicate(compared), compared)
        except Exception as exc:
            for item in compared:
                _record(item, round_number, "failed", error_type=type(exc).__name__,
                        model=_model(deduplicator))
            LOGGER.warning("component=content_deduplication status=failed round=%d duration=%.3fs error=%s",
                           round_number, time.monotonic() - started, type(exc).__name__)
            break

        dropped = set()
        for item, decision in zip(compared, decisions, strict=True):
            status = "duplicate" if decision.duplicate_of else "retained"
            _record(item, round_number, status, duplicate_of=decision.duplicate_of,
                    reason=decision.reason, model=_model(deduplicator))
            if decision.duplicate_of:
                dropped.add(item.story.hn_item_id)
                item.selected = False
                item.section = ""
                item.rejection_reason = "content_duplicate"
        LOGGER.info("component=content_deduplication status=success round=%d compared=%d removed=%d duration=%.3fs",
                    round_number, len(compared), len(dropped), time.monotonic() - started)
        if not dropped:
            break
        selection.ai_items[:] = [i for i in selection.ai_items if i.story.hn_item_id not in dropped]
        selection.selected_hot_items[:] = [
            i for i in selection.selected_hot_items if i.story.hn_item_id not in dropped
        ]
        pending = []
        for section, selected, quota in (
            ("ai", selection.ai_items, AI_MAX_ITEMS),
            ("non_ai_hot", selection.selected_hot_items, NON_AI_MAX_ITEMS),
        ):
            while len(selected) < quota and replacements < CONTENT_DEDUP_MAX_REPLACEMENTS:
                candidate = next(reserves[section], None)
                if candidate is None:
                    break
                replacements += 1
                candidate.selected = True
                candidate.section = section
                candidate.rejection_reason = ""
                if candidate.content_kind == "community_roundup":
                    candidate.why = "HN 部分评论提供了具体项目、工具或实践经验；按热度入选"
                elif section == "non_ai_hot":
                    candidate.why = "HN-wide hot story outside core interests"
                selected.append(candidate)
                pending.append(candidate)
        if pending:
            summary_inputs.extend(summarize(pending))
        elif replacements >= CONTENT_DEDUP_MAX_REPLACEMENTS:
            LOGGER.info("component=content_deduplication status=refill_limit replacements=%d",
                        replacements)
    return summary_inputs


def _model(deduplicator) -> str:
    value = getattr(deduplicator, "last_deduplication_model", "")
    return value if isinstance(value, str) else ""


def _record(candidate, round_number, status, *, duplicate_of=None, reason="",
            error_type="", model=""):
    candidate.content_deduplication.append({
        "round": round_number,
        "status": status,
        "duplicate_of": duplicate_of,
        "reason": reason,
        "error_type": error_type,
        "model": model,
    })
