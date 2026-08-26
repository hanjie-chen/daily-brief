from __future__ import annotations

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
    if not isinstance(item, dict) or set(item) != ITEM_KEYS:
        raise PublicBriefValidationError("item must contain the exact schema v2 fields")

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
