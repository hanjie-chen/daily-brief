from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .public_schema import PublicBriefValidationError, validate_public_brief

LOGGER = logging.getLogger(__name__)
PUBLISH_URL_ENV = "DAILY_BRIEF_PUBLISH_URL"
PUBLISH_TOKEN_ENV = "DAILY_BRIEF_PUBLISH_TOKEN"
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 10
PUBLISH_USER_AGENT = "daily-brief-publisher/1.0"


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishResult:
    published: int
    skipped: int


def publish_brief(
    brief_dir,
    data_dir,
    date_label: str,
    force: bool = False,
    endpoint: str | None = None,
    token: str | None = None,
    opener=urlopen,
    sleeper=time.sleep,
) -> PublishResult:
    publish_url = (endpoint or os.environ.get(PUBLISH_URL_ENV, "")).strip()
    publish_token = token or os.environ.get(PUBLISH_TOKEN_ENV, "")
    if not publish_url:
        raise PublishError(f"{PUBLISH_URL_ENV} is required")
    if not publish_token:
        raise PublishError(f"{PUBLISH_TOKEN_ENV} is required")

    brief_path = _brief_path(Path(brief_dir), date_label)
    marker_path = _no_content_marker_path(Path(brief_dir), date_label)
    if not brief_path.exists():
        if marker_path.is_file():
            LOGGER.info("component=publisher status=no_content date=%s", date_label)
            return PublishResult(published=0, skipped=1)
        raise PublishError(f"brief JSON not found: {brief_path}")
    if not brief_path.is_file():
        raise PublishError(f"brief JSON is not a file: {brief_path}")
    state_path = Path(data_dir) / "publish-state.json"
    published_hashes = _load_state(state_path)
    payload_bytes = brief_path.read_bytes()
    _validate_local_payload(brief_path, payload_bytes)
    content_hash = hashlib.sha256(payload_bytes).hexdigest()
    if not force and published_hashes.get(date_label) == content_hash:
        return PublishResult(published=0, skipped=1)

    _post_with_retry(
        publish_url,
        publish_token,
        payload_bytes,
        opener=opener,
        sleeper=sleeper,
    )
    published_hashes[date_label] = content_hash
    _write_state(state_path, published_hashes)
    LOGGER.info("component=publisher status=success date=%s", date_label)
    return PublishResult(published=1, skipped=0)


def _brief_path(brief_dir: Path, date_label: str) -> Path:
    try:
        date.fromisoformat(date_label)
    except ValueError as exc:
        raise PublishError(f"invalid date: {date_label}") from exc
    return brief_dir / f"{date_label}.json"


def _no_content_marker_path(brief_dir: Path, date_label: str) -> Path:
    return brief_dir / f"{date_label}.no-content"


def _validate_local_payload(path: Path, payload_bytes: bytes) -> None:
    try:
        payload = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"invalid brief JSON: {path}") from exc

    try:
        validate_public_brief(payload)
    except PublicBriefValidationError as exc:
        raise PublishError(f"invalid public brief schema: {path}: {exc}") from exc
    if payload.get("date") != path.stem:
        raise PublishError(f"brief date does not match filename: {path}")


def _post_with_retry(url, token, payload_bytes, opener, sleeper) -> None:
    request = Request(
        url,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "User-Agent": PUBLISH_USER_AGENT,
            "X-Daily-Brief-Token": token,
        },
        method="POST",
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                status = response.getcode()
                if 200 <= status < 300:
                    return
                raise PublishError(f"website returned HTTP {status}")
        except HTTPError as exc:
            if exc.code < 500 or attempt == MAX_ATTEMPTS:
                raise PublishError(f"website returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == MAX_ATTEMPTS:
                raise PublishError(f"could not publish brief: {exc}") from exc

        sleeper(2 ** (attempt - 1))

    raise PublishError("could not publish brief")


def _load_state(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}

    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    published = payload.get("published")
    if not isinstance(published, dict):
        return {}
    return {
        key: value
        for key, value in published.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _write_state(path: Path, published_hashes: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {"version": 1, "published": published_hashes},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
