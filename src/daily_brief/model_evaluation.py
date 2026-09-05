from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .config import (
    AI_MAX_ITEMS,
    EXPLORATION_CLASSIFIER_MAX_CANDIDATES,
    NON_AI_MAX_ITEMS,
    TIMEZONE,
)
from .model_backend import ModelBackend, ensure_topic_decisions
from .models import Candidate, Story
from .summarizer import normalize_summary_text

SCHEMA_VERSION = 3
MAX_SUMMARY_CANDIDATES = AI_MAX_ITEMS + NON_AI_MAX_ITEMS
MAX_TEXT_LENGTH = 256 * 1024
BACKEND_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SUMMARY_BASES = frozenset(
    {
        "not_generated",
        "none",
        "fetched_article",
        "youtube_caption",
        "story_text",
        "title_only",
        "hn_comments",
    }
)


class ModelEvaluationInputError(ValueError):
    pass


@dataclass(frozen=True)
class ModelEvaluationInput:
    date_label: str
    exploration_classification_batches: list[list[Candidate]]
    summary_candidates: list[Candidate]


@dataclass(frozen=True)
class ModelEvaluationResult:
    output_path: Path
    failures: int


def capture_model_evaluation_input(
    path: Path,
    date_label: str,
    exploration_classification_batches: list[list[Candidate]],
    summary_candidates: list[Candidate],
) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "date": date_label,
        "exploration_classification_batches": [
            [_serialize_candidate(candidate) for candidate in batch]
            for batch in exploration_classification_batches
        ],
        "summary_candidates": [
            _serialize_candidate(candidate) for candidate in summary_candidates
        ],
    }
    _atomic_write_json(path, payload)


def load_model_evaluation_input(path: Path) -> ModelEvaluationInput:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ModelEvaluationInputError(f"cannot read evaluation input: {exc}") from exc

    if not isinstance(payload, dict):
        raise ModelEvaluationInputError("evaluation input must be a JSON object")
    expected_keys = {
        "schema_version",
        "date",
        "exploration_classification_batches",
        "summary_candidates",
    }
    if set(payload) != expected_keys or payload["schema_version"] != SCHEMA_VERSION:
        raise ModelEvaluationInputError("unsupported evaluation input schema")

    date_label = payload["date"]
    if not isinstance(date_label, str):
        raise ModelEvaluationInputError("evaluation date must be a string")
    try:
        if date.fromisoformat(date_label).isoformat() != date_label:
            raise ValueError
    except ValueError as exc:
        raise ModelEvaluationInputError("evaluation date must use YYYY-MM-DD") from exc

    classification_batches = _parse_classification_batches(
        payload["exploration_classification_batches"],
    )
    summary_candidates = _parse_candidate_list(
        payload["summary_candidates"],
        "summary_candidates",
        MAX_SUMMARY_CANDIDATES,
    )
    return ModelEvaluationInput(
        date_label=date_label,
        exploration_classification_batches=classification_batches,
        summary_candidates=summary_candidates,
    )


def run_model_evaluation(
    input_path: Path,
    output_dir: Path,
    backend: ModelBackend,
    *,
    clock: Callable[[], float] = time.monotonic,
    evaluated_at: str | None = None,
) -> ModelEvaluationResult:
    if not BACKEND_NAME_PATTERN.fullmatch(backend.name):
        raise ValueError("backend name must be a safe lowercase identifier")

    evaluation_input = load_model_evaluation_input(input_path)
    failures = 0
    classification_results = []
    for batch in evaluation_input.exploration_classification_batches:
        classifier_started = clock()
        try:
            decisions = ensure_topic_decisions(backend.classify(batch), batch)
        except Exception as exc:
            failures += 1
            classification_results.append(
                {
                    "item_ids": [item.story.hn_item_id for item in batch],
                    "status": "failed",
                    "duration_seconds": round(clock() - classifier_started, 3),
                    "decisions": [],
                    "error": _error_text(exc),
                }
            )
        else:
            classification_results.append(
                {
                    "item_ids": [item.story.hn_item_id for item in batch],
                    "status": "success",
                    "duration_seconds": round(clock() - classifier_started, 3),
                    "decisions": [
                        {"id": item_id, "label": decisions[item_id]}
                        for item_id in sorted(decisions)
                    ],
                    "error": "",
                }
            )

    summary_results = []
    for candidate in evaluation_input.summary_candidates:
        summary_started = clock()
        try:
            summary = normalize_summary_text(backend.summarize(candidate))
            if not summary:
                raise RuntimeError("model backend returned an empty summary")
        except Exception as exc:
            failures += 1
            summary_results.append(
                {
                    "hn_item_id": candidate.story.hn_item_id,
                    "status": "failed",
                    "duration_seconds": round(clock() - summary_started, 3),
                    "summary": "",
                    "error": _error_text(exc),
                }
            )
        else:
            summary_results.append(
                {
                    "hn_item_id": candidate.story.hn_item_id,
                    "status": "success",
                    "duration_seconds": round(clock() - summary_started, 3),
                    "summary": summary,
                    "error": "",
                }
            )

    output_path = output_dir / f"{evaluation_input.date_label}-{backend.name}.json"
    _atomic_write_json(
        output_path,
        {
            "schema_version": SCHEMA_VERSION,
            "source_date": evaluation_input.date_label,
            "backend": backend.name,
            "evaluated_at": evaluated_at
            or datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "exploration_classifications": classification_results,
            "summaries": summary_results,
        },
    )
    return ModelEvaluationResult(output_path=output_path, failures=failures)


def _serialize_candidate(candidate: Candidate) -> dict:
    story = candidate.story
    return {
        "source": story.source,
        "hn_item_id": story.hn_item_id,
        "title": story.title,
        "source_url": story.source_url,
        "hn_discussion_url": story.hn_discussion_url,
        "created_at": story.created_at,
        "points": story.points,
        "comments": story.comments,
        "story_text": story.story_text,
        "fetched_text": story.fetched_text,
        "summary_basis": candidate.summary_basis,
        "discussion_text": candidate.discussion_text,
    }


def _parse_candidate_list(value, field_name: str, maximum: int) -> list[Candidate]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelEvaluationInputError(
            f"{field_name} must be an array with at most {maximum} items"
        )
    candidates = [_parse_candidate(item, field_name) for item in value]
    item_ids = [candidate.story.hn_item_id for candidate in candidates]
    if len(set(item_ids)) != len(item_ids):
        raise ModelEvaluationInputError(f"{field_name} contains duplicate item IDs")
    return candidates


def _parse_classification_batches(value) -> list[list[Candidate]]:
    field_name = "exploration_classification_batches"
    if (
        not isinstance(value, list)
        or len(value) > EXPLORATION_CLASSIFIER_MAX_CANDIDATES
    ):
        raise ModelEvaluationInputError(
            f"{field_name} must contain at most "
            f"{EXPLORATION_CLASSIFIER_MAX_CANDIDATES} batches"
        )
    batches = [_parse_candidate_list(batch, field_name, 1) for batch in value]
    if any(len(batch) != 1 for batch in batches):
        raise ModelEvaluationInputError(
            f"each {field_name} batch must contain exactly one item"
        )
    item_ids = [batch[0].story.hn_item_id for batch in batches]
    if len(set(item_ids)) != len(item_ids):
        raise ModelEvaluationInputError(f"{field_name} contains duplicate item IDs")
    return batches


def _parse_candidate(value, field_name: str) -> Candidate:
    expected_keys = {
        "source",
        "hn_item_id",
        "title",
        "source_url",
        "hn_discussion_url",
        "created_at",
        "points",
        "comments",
        "story_text",
        "fetched_text",
        "summary_basis",
        "discussion_text",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ModelEvaluationInputError(f"invalid item in {field_name}")

    text_limits = {
        "source": 64,
        "hn_item_id": 64,
        "title": 1000,
        "source_url": 4096,
        "hn_discussion_url": 4096,
        "created_at": 128,
        "story_text": MAX_TEXT_LENGTH,
        "fetched_text": MAX_TEXT_LENGTH,
        "summary_basis": 64,
        "discussion_text": MAX_TEXT_LENGTH,
    }
    for key, maximum in text_limits.items():
        if not isinstance(value[key], str) or len(value[key]) > maximum:
            raise ModelEvaluationInputError(f"invalid {key} in {field_name}")
    if not value["hn_item_id"] or not value["title"]:
        raise ModelEvaluationInputError(
            f"item ID and title are required in {field_name}"
        )
    for key in ("points", "comments"):
        if (
            not isinstance(value[key], int)
            or isinstance(value[key], bool)
            or value[key] < 0
        ):
            raise ModelEvaluationInputError(f"invalid {key} in {field_name}")
    if value["summary_basis"] not in SUMMARY_BASES:
        raise ModelEvaluationInputError(f"invalid summary_basis in {field_name}")
    if bool(value["discussion_text"]) != (
        value["summary_basis"] == "hn_comments"
    ):
        raise ModelEvaluationInputError(
            f"discussion_text must match summary_basis in {field_name}"
        )

    story_fields = {
        key: value[key]
        for key in (
            "source",
            "hn_item_id",
            "title",
            "source_url",
            "hn_discussion_url",
            "created_at",
            "points",
            "comments",
            "story_text",
            "fetched_text",
        )
    }
    return Candidate(
        story=Story(**story_fields),
        summary_basis=value["summary_basis"],
        discussion_text=value["discussion_text"],
    )


def _error_text(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message}"[:1000]


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(serialized)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
