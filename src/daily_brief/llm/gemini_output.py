"""Structured output for Gemini calls: the JSON schema each call requests and
the validation of each response before it reaches the pipeline."""

from __future__ import annotations

from ..models import Candidate
from .gemini_api import GeminiResponseError
from .summarizer import (
    HN_DISCUSSION_SUMMARY_PREFIX,
    MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS,
    MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS,
    MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS,
    MAX_INSUFFICIENT_REASON_CHARS,
    InsufficientSummaryMaterial,
    source_summary_prefix,
)
from .topic_classifier import TOPIC_LABELS

MAX_SUMMARY_CHARS = 1000


def classifier_labels(candidates: list[Candidate]) -> set[str]:
    labels = set(TOPIC_LABELS) - {"community_roundup"}
    if any(
        candidate.story.source_url == candidate.story.hn_discussion_url
        and candidate.content_kind != "community_roundup"
        for candidate in candidates
    ):
        labels.add("community_roundup")
    return labels


def classification_schema(allowed_ids: list[str], allowed_labels: set[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "description": "One topic decision for every supplied item.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": allowed_ids},
                        "label": {
                            "type": "string",
                            "enum": sorted(allowed_labels),
                        },
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
                "minItems": len(allowed_ids),
                "maxItems": len(allowed_ids),
            }
        },
        "required": ["decisions"],
        "additionalProperties": False,
    }


def validate_classification(
    output: dict,
    candidates: list[Candidate],
    allowed_ids: list[str],
    allowed_labels: set[str],
) -> dict[str, str]:
    if set(output) != {"decisions"} or not isinstance(output["decisions"], list):
        raise GeminiResponseError("Gemini classifier returned an invalid object")
    decisions = output["decisions"]
    if not all(
        isinstance(item, dict)
        and set(item) == {"id", "label"}
        and isinstance(item["id"], str)
        and isinstance(item["label"], str)
        for item in decisions
    ):
        raise GeminiResponseError("Gemini classifier returned invalid decisions")
    decision_ids = [item["id"] for item in decisions]
    if len(set(decision_ids)) != len(decision_ids):
        raise GeminiResponseError("Gemini classifier returned duplicate IDs")
    unknown_ids = set(decision_ids) - set(allowed_ids)
    if unknown_ids:
        raise GeminiResponseError("Gemini classifier returned unknown IDs")
    if set(decision_ids) != set(allowed_ids):
        raise GeminiResponseError("Gemini classifier omitted item IDs")
    if any(item["label"] not in allowed_labels for item in decisions):
        raise GeminiResponseError("Gemini classifier returned unknown labels")
    candidates_by_id = {candidate.story.hn_item_id: candidate for candidate in candidates}
    if any(
        item["label"] == "community_roundup"
        and (
            candidates_by_id[item["id"]].story.source_url
            != candidates_by_id[item["id"]].story.hn_discussion_url
            or candidates_by_id[item["id"]].content_kind == "community_roundup"
        )
        for item in decisions
    ):
        raise GeminiResponseError("Gemini classifier returned invalid community roundup")
    return {item["id"]: item["label"] for item in decisions}


def summary_schema(*, community_roundup: bool, combined: bool) -> dict:
    return (
        {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["sufficient", "insufficient"]},
                "introduction": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS},
                "entries": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS},
                            "description": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS},
                        },
                        "required": ["name", "description"],
                        "additionalProperties": False,
                    },
                },
                "reason": {"type": "string", "maxLength": MAX_INSUFFICIENT_REASON_CHARS},
            },
            "required": ["status", "introduction", "entries", "reason"],
            "additionalProperties": False,
        }
        if community_roundup
        else {
            "type": "object",
            "properties": {
                **({"source_summary": {"type": "string"}} if combined else {}),
                "status": {
                    "type": "string",
                    "enum": ["sufficient", "insufficient"],
                },
                "summary": {
                    "type": "string",
                    "description": "A concise Chinese summary, or empty if insufficient.",
                },
                "reason": {
                    "type": "string",
                    "maxLength": MAX_INSUFFICIENT_REASON_CHARS,
                    "description": "Why material is insufficient; empty if sufficient.",
                },
            },
            "required": ["status", "summary", "reason"] + (["source_summary"] if combined else []),
            "additionalProperties": False,
        }
    )


def validate_summary(output: dict, candidate: Candidate, *, combined: bool) -> str:
    if (
        set(output) != ({"status", "summary", "reason"} | ({"source_summary"} if combined else set()))
        or not all(isinstance(value, str) for value in output.values())
        or output["status"] not in {"sufficient", "insufficient"}
    ):
        raise GeminiResponseError("Gemini summarizer returned an invalid object")
    summary = output["summary"].strip()
    source_summary = output.get("source_summary", "").strip()
    reason = output["reason"].strip()
    if len(output["reason"]) > MAX_INSUFFICIENT_REASON_CHARS:
        raise GeminiResponseError("Gemini summarizer returned an oversized reason")
    if output["status"] == "insufficient":
        if summary or source_summary or not reason:
            raise GeminiResponseError("Gemini summarizer returned an inconsistent decision")
        raise InsufficientSummaryMaterial(reason)
    if reason:
        raise GeminiResponseError("Gemini summarizer returned an inconsistent decision")
    if not summary and not source_summary:
        raise GeminiResponseError("Gemini summarizer returned an empty summary")
    if combined:
        parts = []
        if source_summary:
            parts.append(source_summary_prefix(candidate) + source_summary)
        if summary:
            parts.append(HN_DISCUSSION_SUMMARY_PREFIX + summary)
        summary = " ".join(parts)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise GeminiResponseError("Gemini summarizer returned an oversized summary")
    return summary


def validate_and_format_community_roundup(output: object) -> str:
    expected_keys = {"status", "introduction", "entries", "reason"}
    if not isinstance(output, dict) or set(output) != expected_keys:
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if not isinstance(output["status"], str) or output["status"] not in {
        "sufficient",
        "insufficient",
    }:
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if not isinstance(output["introduction"], str) or not isinstance(output["reason"], str):
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if any("\n" in value or "\r" in value for value in (output["introduction"], output["reason"])):
        raise GeminiResponseError("Gemini community roundup returned a multiline field")

    introduction = output["introduction"].strip()
    reason = output["reason"].strip()
    entries = output["entries"]
    if len(output["reason"]) > MAX_INSUFFICIENT_REASON_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized reason")
    if len(introduction) > MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized introduction")
    if output["status"] == "insufficient":
        if introduction or entries != [] or not reason:
            raise GeminiResponseError("Gemini community roundup returned an inconsistent decision")
        raise InsufficientSummaryMaterial(reason)
    if reason or not introduction or not isinstance(entries, list) or not 2 <= len(entries) <= 3:
        raise GeminiResponseError("Gemini community roundup returned an inconsistent decision")

    formatted_entries = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "description"}:
            raise GeminiResponseError("Gemini community roundup returned an invalid entry")
        name = entry["name"]
        description = entry["description"]
        if not isinstance(name, str) or not isinstance(description, str):
            raise GeminiResponseError("Gemini community roundup returned an invalid entry")
        if "\n" in name or "\r" in name or "\n" in description or "\r" in description:
            raise GeminiResponseError("Gemini community roundup returned a multiline field")
        name = name.strip()
        description = description.strip()
        if not name or not description:
            raise GeminiResponseError("Gemini community roundup returned an empty entry")
        if len(name) > MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS or len(description) > MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS:
            raise GeminiResponseError("Gemini community roundup returned an oversized entry")
        formatted_entries.append(f"- {name}：{description}")

    summary = introduction + "\n\n" + "\n".join(formatted_entries)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized summary")
    return summary
