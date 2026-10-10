from __future__ import annotations

import re
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit

PUBLIC_BRIEF_SCHEMA_VERSION = 2
SECTION_LIMITS = {"ai": 5, "non_ai_hot": 2}
ROOT_KEYS = {"schema_version", "date", "generated_at", "timezone", "sections"}
SECTION_KEYS = {"note", "items"}
ITEM_KEYS = {
    "hn_item_id",
    "title",
    "summary",
    "content_status",
    "why",
    "source_url",
    "discussion_url",
    "points",
    "comments",
}
CONTENT_STATUSES = {"ok", "fetch_failed", "summary_failed", "title_only"}
# Optional additive v2 field. Only these public codes cross the audit boundary.
PROVENANCE_VALUES = {
    "summary_basis": {
        "article", "source_and_comments", "hn_comments", "hn_post",
        "video_captions", "none", "unknown",
    },
    "retrieval_method": {
        "direct", "jina", "wayback", "github_readme", "github_raw",
        "youtube_caption", "story_text", "none", "unknown",
    },
    "retrieval_status": {
        "success", "failed", "not_attempted", "not_needed", "unknown",
    },
    "material_origin": {
        "original", "archived_copy", "same_article", "syndicated_copy",
        "alternate_reporting", "unknown",
    },
    "fallback_reason": {
        "none", "challenge_page", "cloudflare_challenge", "datadome_challenge",
        "vercel_challenge", "empty_content", "network_timeout",
        "tls_issuer_unavailable", "source_material_insufficient", "unknown",
    },
}


MATERIAL_STATUSES = {"success", "empty", "failed", "not_attempted", "not_needed", "unknown"}
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
GENERATION_STATUSES = {"success", "insufficient", "failed", "not_attempted", "unknown"}
SUMMARY_SOURCES = {"web_metadata", "web_body", "hn_post", "hn_comments"}
GENERATION_REASONS = {
    "none", "unknown", "challenge_page", "cloudflare_challenge", "datadome_challenge",
    "vercel_challenge", "empty_content", "network_timeout", "tls_issuer_unavailable",
    "source_material_insufficient", "network_error", "http_error", "extraction_failed",
    "rate_limited", "authentication_failed", "provider_unavailable", "invalid_response",
    "no_materials",
}
MODEL_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}", re.ASCII)


class PublicBriefValidationError(ValueError):
    pass


class EmptyPublicBriefError(PublicBriefValidationError):
    pass


def validate_public_brief(payload) -> None:
    if not isinstance(payload, dict) or set(payload) != ROOT_KEYS:
        raise PublicBriefValidationError(
            "payload must contain the exact schema v2 fields"
        )
    if payload["schema_version"] != PUBLIC_BRIEF_SCHEMA_VERSION:
        raise PublicBriefValidationError("unsupported schema_version")

    _validate_date(payload["date"])
    _validate_generated_at(payload["generated_at"])
    if payload["timezone"] != "Asia/Singapore":
        raise PublicBriefValidationError("timezone must be Asia/Singapore")

    sections = payload["sections"]
    if not isinstance(sections, dict) or set(sections) != set(SECTION_LIMITS):
        raise PublicBriefValidationError("sections must contain ai and non_ai_hot")

    total_items = 0
    for section_name, item_limit in SECTION_LIMITS.items():
        section = sections[section_name]
        if not isinstance(section, dict) or set(section) != SECTION_KEYS:
            raise PublicBriefValidationError(f"invalid {section_name} section")
        _validate_text(section["note"], f"{section_name}.note", 500, allow_empty=True)
        items = section["items"]
        if not isinstance(items, list) or len(items) > item_limit:
            raise PublicBriefValidationError(f"{section_name} contains too many items")
        for item in items:
            _validate_item(item)
        total_items += len(items)

    if total_items == 0:
        raise EmptyPublicBriefError("brief must contain at least one item")


def _validate_item(item) -> None:
    if (not isinstance(item, dict) or not ITEM_KEYS <= set(item)
            or set(item) - ITEM_KEYS - {"provenance", "generation_info"}):
        raise PublicBriefValidationError("item must contain the exact schema v2 fields")

    if "provenance" in item:
        _validate_provenance(item["provenance"])

    if "generation_info" in item:
        _validate_generation_info(item["generation_info"])

    hn_item_id = _validate_text(item["hn_item_id"], "hn_item_id", 32)
    if not hn_item_id.isdigit():
        raise PublicBriefValidationError("hn_item_id must contain only digits")
    content_status = item["content_status"]
    if not isinstance(content_status, str) or content_status not in CONTENT_STATUSES:
        raise PublicBriefValidationError("unsupported content_status")

    _validate_text(item["title"], "title", 300)
    _validate_text(item["summary"], "summary", 4000)
    _validate_text(item["why"], "why", 1000)
    _validate_count(item["points"], "points")
    _validate_count(item["comments"], "comments")
    _validate_http_url(item["source_url"], "source_url")
    discussion_url = _validate_http_url(item["discussion_url"], "discussion_url")
    discussion = urlsplit(discussion_url)
    discussion_ids = parse_qs(discussion.query).get("id", [])
    if (
        discussion.hostname != "news.ycombinator.com"
        or discussion.path != "/item"
        or discussion_ids != [hn_item_id]
    ):
        raise PublicBriefValidationError("discussion_url must match hn_item_id")


def _validate_provenance(value) -> None:
    if not isinstance(value, dict) or set(value) != set(PROVENANCE_VALUES):
        raise PublicBriefValidationError("provenance must contain the exact public fields")
    for field, allowed in PROVENANCE_VALUES.items():
        if not isinstance(value[field], str) or value[field] not in allowed:
            raise PublicBriefValidationError(f"unsupported provenance.{field}")


def _validate_generation_info(value) -> None:
    def exact(obj, keys, name):
        if not isinstance(obj, dict) or set(obj) != set(keys):
            raise PublicBriefValidationError(f"invalid generation_info.{name} fields")

    def enum(code, allowed, name):
        if not isinstance(code, str) or code not in allowed:
            raise PublicBriefValidationError(f"unsupported generation_info.{name}")

    exact(value, {"materials", "summary_sources", "generation"}, "root")
    exact(value["materials"], {"webpage", "hn_post", "hn_comments"}, "materials")
    for name, material in value["materials"].items():
        keys = {"status", "reason", "method", "origin"} if name == "webpage" else {"status", "reason"}
        exact(material, keys, name)
        enum(material["status"], MATERIAL_STATUSES, f"{name}.status")
        enum(material["reason"], GENERATION_REASONS, f"{name}.reason")
        if name == "webpage":
            enum(material["method"], PROVENANCE_VALUES["retrieval_method"], "webpage.method")
            enum(material["origin"], PROVENANCE_VALUES["material_origin"], "webpage.origin")
    sources = value["summary_sources"]
    if sources is not None:
        if (not isinstance(sources, list) or len(sources) > 4
                or any(not isinstance(source, str) or source not in SUMMARY_SOURCES for source in sources)
                or len(sources) != len(set(sources))):
            raise PublicBriefValidationError("invalid generation_info.summary_sources")
    generation = value["generation"]
    required = {"status", "model", "reason"}
    if (not isinstance(generation, dict) or not required <= set(generation)
            or set(generation) - required - {"reasoning_effort"}):
        raise PublicBriefValidationError("invalid generation_info.generation fields")
    effort = generation.get("reasoning_effort")
    if effort is not None:
        enum(effort, REASONING_EFFORTS, "generation.reasoning_effort")
    enum(generation["status"], GENERATION_STATUSES, "generation.status")
    enum(generation["reason"], GENERATION_REASONS, "generation.reason")
    model = generation["model"]
    if model is not None and (not isinstance(model, str) or MODEL_IDENTIFIER.fullmatch(model) is None):
        raise PublicBriefValidationError("invalid generation_info.generation.model")


def _validate_date(value) -> None:
    if not isinstance(value, str):
        raise PublicBriefValidationError("date must be a string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PublicBriefValidationError("date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise PublicBriefValidationError("date must use canonical YYYY-MM-DD")


def _validate_generated_at(value) -> None:
    if not isinstance(value, str) or len(value) > 64:
        raise PublicBriefValidationError("generated_at must be an RFC3339 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PublicBriefValidationError(
            "generated_at must be an RFC3339 string"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicBriefValidationError("generated_at must include a timezone offset")


def _validate_text(value, field, max_length, allow_empty=False) -> str:
    if not isinstance(value, str):
        raise PublicBriefValidationError(f"{field} must be a string")
    cleaned = value.strip()
    if not allow_empty and not cleaned:
        raise PublicBriefValidationError(f"{field} must not be empty")
    if len(cleaned) > max_length:
        raise PublicBriefValidationError(f"{field} is too long")
    return cleaned


def _validate_http_url(value, field) -> str:
    url = _validate_text(value, field, 2048)
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PublicBriefValidationError(f"{field} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise PublicBriefValidationError(f"{field} must not contain credentials")
    return url


def _validate_count(value, field) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PublicBriefValidationError(f"{field} must be an integer")
    if value < 0 or value > 10_000_000:
        raise PublicBriefValidationError(f"{field} is out of range")
