from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request

from .contracts import ArticleFetchError, ArticleFetchResult
from .http_safety import _read_bounded, _validate_public_http_url
from .jina import JINA_JSON_CONTENT_TYPES
from .responses import _fetch_direct_response, _reject_encoded_wayback_response


WAYBACK_CDX_BASE_URL = "https://web.archive.org/cdx/search/cdx"
WAYBACK_REPLAY_BASE_URL = "https://web.archive.org/web"
WAYBACK_DEFAULT_LOOKBACK = timedelta(days=2)
WAYBACK_METADATA_MAX_BYTES = 64 * 1024
WAYBACK_TIMESTAMP_PATTERN = re.compile(r"^\d{14}$")


@dataclass(frozen=True)
class _WaybackCapture:
    timestamp: str
    original_url: str


def _find_wayback_capture(
    url: str,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    not_before: datetime | None,
    not_after: datetime | None,
) -> _WaybackCapture:
    lower_bound, upper_bound = _wayback_capture_window(not_before, not_after)
    fields = [
        "timestamp",
        "original",
        "mimetype",
        "statuscode",
        "digest",
        "length",
    ]
    query = urlencode(
        [
            ("url", url),
            ("matchType", "exact"),
            ("output", "json"),
            ("fl", ",".join(fields)),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("from", _wayback_timestamp(lower_bound)),
            ("to", _wayback_timestamp(upper_bound)),
            ("limit", "-5"),
            ("gzip", "false"),
        ]
    )
    request_url = f"{WAYBACK_CDX_BASE_URL}?{query}"
    _validate_public_http_url(request_url, resolver)
    request = Request(
        request_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )

    try:
        with opener(request, timeout=timeout_seconds) as response:
            _validate_wayback_cdx_response_url(response.geturl())
            _reject_encoded_wayback_response(response.headers)
            content_type = response.headers.get_content_type().lower()
            if content_type not in JINA_JSON_CONTENT_TYPES:
                raise ArticleFetchError(
                    "Wayback CDX returned an unsupported content type",
                    error_code="wayback_unsupported_content_type",
                    method="wayback",
                )
            payload = _read_bounded(response, WAYBACK_METADATA_MAX_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise ArticleFetchError(
            f"Wayback CDX request failed: {exc}",
            error_code=f"wayback_http_{exc.code}",
            method="wayback",
        ) from exc
    except ArticleFetchError:
        raise
    except Exception as exc:
        raise ArticleFetchError(
            f"Wayback CDX request failed: {exc}",
            error_code="wayback_request_failed",
            method="wayback",
        ) from exc

    try:
        rows = json.loads(payload.decode(charset))
    except (LookupError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArticleFetchError(
            "Wayback CDX returned malformed JSON",
            error_code="wayback_malformed_json",
            method="wayback",
        ) from exc
    if not isinstance(rows, list) or not rows or rows[0] != fields:
        raise ArticleFetchError(
            "Wayback CDX returned an invalid result envelope",
            error_code="wayback_invalid_index",
            method="wayback",
        )

    if len(rows) == 1:
        raise ArticleFetchError(
            "Wayback CDX found no capture in the allowed time window",
            error_code="wayback_no_capture",
            method="wayback",
        )

    captures = []
    for row in rows[1:]:
        capture = _parse_wayback_index_row(
            row,
            source_url=url,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            resolver=resolver,
        )
        captures.append(capture)
    return max(captures, key=lambda capture: capture.timestamp)


def _fetch_wayback_capture(
    capture: _WaybackCapture,
    *,
    source_url: str,
    opener,
    resolver,
    timeout_seconds: int,
    html_max_bytes: int,
    pdf_max_bytes: int,
    extracted_max_bytes: int,
    pdf_max_pages: int,
    pdf_parse_timeout_seconds: int,
    pdf_address_space_bytes: int,
) -> ArticleFetchResult:
    replay_url = (
        f"{WAYBACK_REPLAY_BASE_URL}/{capture.timestamp}id_/"
        f"{capture.original_url}"
    )
    _validate_public_http_url(replay_url, resolver)
    request = Request(
        replay_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/html",
            "Accept-Encoding": "identity",
        },
    )
    try:
        result = _fetch_direct_response(
            request,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            html_max_bytes=html_max_bytes,
            pdf_max_bytes=pdf_max_bytes,
            extracted_max_bytes=extracted_max_bytes,
            pdf_max_pages=pdf_max_pages,
            pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
            pdf_address_space_bytes=pdf_address_space_bytes,
            require_identity_encoding=True,
        )
    except HTTPError as exc:
        raise ArticleFetchError(
            f"Wayback replay request failed: {exc}",
            error_code=f"wayback_http_{exc.code}",
            method="wayback",
        ) from exc
    except ArticleFetchError as exc:
        error_code = exc.error_code
        if exc.error_code in {"challenge_page", "vercel_challenge"}:
            error_code = "wayback_challenge_page"
        elif not exc.error_code.startswith("wayback_"):
            error_code = f"wayback_{exc.error_code}"
        raise ArticleFetchError(
            f"Wayback replay failed: {exc}",
            error_code=error_code,
            method="wayback",
            extractor=exc.extractor,
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"Wayback replay request failed: {exc}",
            error_code="wayback_request_failed",
            method="wayback",
        ) from exc

    _validate_wayback_replay_url(
        result.retrieved_url,
        capture=capture,
        source_url=source_url,
    )
    return ArticleFetchResult(
        text=result.text,
        method="wayback",
        extractor=result.extractor,
        retrieved_url=result.retrieved_url,
        material_origin="archived_copy",
    )


def _wayback_capture_window(
    not_before: datetime | None,
    not_after: datetime | None,
) -> tuple[datetime, datetime]:
    upper_bound = _utc_datetime(not_after or datetime.now(UTC))
    lower_bound = _utc_datetime(
        not_before or (upper_bound - WAYBACK_DEFAULT_LOOKBACK)
    )
    if lower_bound > upper_bound:
        raise ArticleFetchError(
            "Wayback capture time window is invalid",
            error_code="wayback_invalid_window",
            method="wayback",
        )
    return lower_bound, upper_bound


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArticleFetchError(
            "Wayback capture bounds must include a timezone",
            error_code="wayback_invalid_window",
            method="wayback",
        )
    return value.astimezone(UTC)


def _wayback_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def _parse_wayback_index_row(
    row,
    *,
    source_url: str,
    lower_bound: datetime,
    upper_bound: datetime,
    resolver,
) -> _WaybackCapture:
    if not isinstance(row, list) or len(row) != 6:
        raise _invalid_wayback_index_row()
    timestamp, original, mimetype, statuscode, digest, length = row
    if not all(isinstance(value, str) for value in row):
        raise _invalid_wayback_index_row()
    if not WAYBACK_TIMESTAMP_PATTERN.fullmatch(timestamp):
        raise _invalid_wayback_index_row()
    try:
        captured_at = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        captured_length = int(length)
    except ValueError:
        raise _invalid_wayback_index_row()
    if not lower_bound <= captured_at <= upper_bound:
        raise _invalid_wayback_index_row()
    if mimetype != "text/html" or statuscode != "200" or not digest:
        raise _invalid_wayback_index_row()
    if captured_length <= 0:
        raise _invalid_wayback_index_row()
    try:
        _validate_public_http_url(original, resolver)
    except ArticleFetchError:
        raise _invalid_wayback_index_row()
    if _archive_url_identity(original) != _archive_url_identity(source_url):
        raise _invalid_wayback_index_row()
    return _WaybackCapture(timestamp=timestamp, original_url=original)


def _invalid_wayback_index_row() -> ArticleFetchError:
    return ArticleFetchError(
        "Wayback CDX returned an invalid capture row",
        error_code="wayback_invalid_index",
        method="wayback",
    )


def _archive_url_identity(url: str) -> tuple[str, str, int | None, str, str]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    return scheme, hostname, port, parsed.path or "/", parsed.query


def _validate_wayback_cdx_response_url(url: str) -> None:
    parsed = urlparse(url)
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.hostname.lower() == "web.archive.org"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path == "/cdx/search/cdx"
    ):
        raise ArticleFetchError(
            "Wayback CDX redirected to an unexpected URL",
            error_code="wayback_invalid_index_url",
            method="wayback",
        )


def _validate_wayback_replay_url(
    url: str,
    *,
    capture: _WaybackCapture,
    source_url: str,
) -> None:
    parsed = urlparse(url)
    replay_prefix = f"/web/{capture.timestamp}id_/"
    if not (
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.hostname.lower() == "web.archive.org"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
        and parsed.path.startswith(replay_prefix)
    ):
        raise ArticleFetchError(
            "Wayback replay redirected to an unexpected URL",
            error_code="wayback_invalid_replay_url",
            method="wayback",
        )
    embedded_original = parsed.path[len(replay_prefix) :]
    if parsed.query:
        embedded_original = f"{embedded_original}?{parsed.query}"
    if _archive_url_identity(embedded_original) != _archive_url_identity(source_url):
        raise ArticleFetchError(
            "Wayback replay URL does not match the requested source",
            error_code="wayback_identity_mismatch",
            method="wayback",
        )
