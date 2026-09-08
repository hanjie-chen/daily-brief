from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request

from .challenges import _is_challenge_page
from .contracts import (
    ArticleFetchError,
    DEFAULT_MAX_EXTRACTED_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
)
from .http_safety import (
    _build_safe_opener,
    _enforce_extracted_limit,
    _read_bounded,
    _validate_public_http_url,
)
from .responses import _normalize_document_text


JINA_READER_BASE_URL = "https://r.jina.ai/"
JINA_CACHE_TOLERANCE_SECONDS = 5 * 60
JINA_JSON_CONTENT_TYPES = {"application/json", "text/json"}


@dataclass(frozen=True)
class _JinaReaderResult:
    text: str
    origin_url: str


def fetch_jina_reader_text(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> str:
    """Fetch and validate one bounded Jina Reader JSON response."""
    return _fetch_jina_reader(
        url,
        opener=opener,
        resolver=resolver,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    ).text


def _fetch_jina_reader(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> _JinaReaderResult:
    _validate_public_http_url(url, resolver)
    reader_url = f"{JINA_READER_BASE_URL}{url}"
    _validate_public_http_url(reader_url, resolver)
    request = Request(
        reader_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "application/json",
            "X-Cache-Tolerance": str(JINA_CACHE_TOLERANCE_SECONDS),
        },
    )
    open_request = opener or _build_safe_opener(resolver).open

    try:
        with open_request(request, timeout=timeout_seconds) as response:
            _validate_public_http_url(response.geturl(), resolver)
            content_type = response.headers.get_content_type().lower()
            if content_type not in JINA_JSON_CONTENT_TYPES:
                raise ArticleFetchError(
                    f"Jina Reader returned an unsupported content type: {content_type}",
                    error_code="jina_unsupported_content_type",
                )
            payload = _read_bounded(response, max_bytes)
            charset = response.headers.get_content_charset() or "utf-8"

        try:
            envelope = json.loads(payload.decode(charset))
        except (LookupError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArticleFetchError(
                "Jina Reader returned malformed JSON",
                error_code="jina_malformed_json",
            ) from exc
        if not isinstance(envelope, dict):
            raise ArticleFetchError(
                "Jina Reader JSON envelope is not an object",
                error_code="jina_invalid_envelope",
            )

        code = envelope.get("code")
        status = envelope.get("status")
        if not (_is_json_integer(code) and 200 <= code < 300):
            raise ArticleFetchError(
                "Jina Reader envelope code does not indicate success",
                error_code="jina_provider_status",
            )
        if not (
            _is_json_integer(status)
            and (200 <= status < 300 or 20000 <= status < 20100)
        ):
            raise ArticleFetchError(
                "Jina Reader envelope status does not indicate success",
                error_code="jina_provider_status",
            )

        data = envelope.get("data")
        if not isinstance(data, dict):
            raise ArticleFetchError(
                "Jina Reader envelope data is not an object",
                error_code="jina_invalid_envelope",
            )
        http_status = data.get("httpStatus")
        if not (_is_json_integer(http_status) and 200 <= http_status < 300):
            raise ArticleFetchError(
                "Jina Reader origin status does not indicate success",
                error_code="jina_origin_status",
            )

        origin_url = data.get("url")
        if not isinstance(origin_url, str):
            raise ArticleFetchError(
                "Jina Reader origin URL is invalid",
                error_code="jina_invalid_url",
            )
        try:
            _validate_public_http_url(origin_url, resolver)
        except ArticleFetchError as exc:
            raise ArticleFetchError(
                "Jina Reader origin URL is not a safe public HTTP destination",
                error_code="jina_invalid_url",
            ) from exc

        content = data.get("content")
        if not isinstance(content, str):
            raise ArticleFetchError(
                "Jina Reader content is not a string",
                error_code="jina_invalid_content",
            )
        text = _normalize_document_text(content)
        if not text:
            raise ArticleFetchError(
                "Jina Reader content is empty",
                error_code="jina_invalid_content",
            )
        if _is_challenge_page(origin_url, text=text):
            raise ArticleFetchError(
                "Jina Reader returned a browser verification challenge page",
                error_code="challenge_page",
            )
        _enforce_extracted_limit(text, max_bytes, extractor="jina")
        return _JinaReaderResult(text=text, origin_url=origin_url)
    except HTTPError as exc:
        raise ArticleFetchError(
            f"Jina Reader request failed: {exc}",
            error_code=f"http_{exc.code}",
            method="jina",
            extractor="jina",
        ) from exc
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            str(exc),
            error_code=exc.error_code,
            method="jina",
            extractor="jina",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"Jina Reader request failed: {exc}",
            error_code="request_failed",
            method="jina",
            extractor="jina",
        ) from exc


def _is_json_integer(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
