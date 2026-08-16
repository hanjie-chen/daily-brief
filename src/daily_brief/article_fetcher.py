from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from http.client import HTTPConnection, HTTPSConnection
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import trafilatura

from .adobe_pdf_extractor import (
    DEFAULT_CONNECT_TIMEOUT_MS as ADOBE_CONNECT_TIMEOUT_MS,
    DEFAULT_READ_TIMEOUT_MS as ADOBE_READ_TIMEOUT_MS,
    credentials_status as adobe_credentials_status,
)
from .youtube_captions import (
    YoutubeCaptionError,
    fetch_youtube_caption,
    youtube_video_id,
)

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_EXTRACTED_BYTES = 256 * 1024
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PDF_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 100
DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 60
DEFAULT_PDF_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS = 300
# Kept as a compatibility name for direct helper callers and tests.
DEFAULT_MAX_BYTES = DEFAULT_MAX_EXTRACTED_BYTES
JINA_READER_BASE_URL = "https://r.jina.ai/"
JINA_CACHE_TOLERANCE_SECONDS = 5 * 60
JINA_JSON_CONTENT_TYPES = {"application/json", "text/json"}
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_RAW_CONTENT_TYPE = "application/vnd.github.raw+json"
GITHUB_REPOSITORY_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
CHALLENGE_TEXT_MARKER_GROUPS = (
    ("verifying your browser", "complete the check below"),
    ("complete the check below to continue", "complete the verification above"),
    ("checking your browser", "enable javascript and cookies to continue"),
)
CHALLENGE_HTML_MARKER_GROUPS = (
    ("challenges.cloudflare.com", "cf-turnstile"),
    ("challenges.cloudflare.com/turnstile", "complete the check below"),
)
PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
}
LOGGER = logging.getLogger(__name__)


class ArticleFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "fetch_failed",
        method: str = "",
        extractor: str = "",
        fallback_attempted: bool = False,
        fallback_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.method = method
        self.extractor = extractor
        self.fallback_attempted = fallback_attempted
        self.fallback_reason = fallback_reason


@dataclass(frozen=True)
class ArticleFetchResult:
    text: str
    method: str
    fallback_reason: str = ""
    extractor: str = ""


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl, self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, *args, resolver, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = partial(
            _create_public_connection,
            resolver=resolver,
        )


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, resolver, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = partial(
            _create_public_connection,
            resolver=resolver,
        )


class _PinnedHTTPHandler(HTTPHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def http_open(self, request):
        connection = partial(_PinnedHTTPConnection, resolver=self.resolver)
        return self.do_open(connection, request)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def https_open(self, request):
        connection = partial(_PinnedHTTPSConnection, resolver=self.resolver)
        return self.do_open(connection, request, context=self._context)


def extract_html(markup: str) -> str:
    """Extract article body text from HTML with the production settings."""
    extracted = trafilatura.extract(
        markup,
        include_comments=False,
        favor_precision=True,
        include_tables=True,
    )
    return _normalize_extracted_blocks(extracted or "")


def fetch_article_text(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int | None = None,
    html_max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    pdf_max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    extracted_max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    pdf_max_pages: int = DEFAULT_MAX_PDF_PAGES,
    pdf_parse_timeout_seconds: int = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    pdf_address_space_bytes: int = DEFAULT_PDF_ADDRESS_SPACE_BYTES,
) -> str:
    """Fetch article text while preserving the original string-returning API."""
    return fetch_article(
        url,
        opener=opener,
        resolver=resolver,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        html_max_bytes=html_max_bytes,
        pdf_max_bytes=pdf_max_bytes,
        extracted_max_bytes=extracted_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
    ).text


def fetch_article(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int | None = None,
    html_max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    pdf_max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    extracted_max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    pdf_max_pages: int = DEFAULT_MAX_PDF_PAGES,
    pdf_parse_timeout_seconds: int = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    pdf_address_space_bytes: int = DEFAULT_PDF_ADDRESS_SPACE_BYTES,
) -> ArticleFetchResult:
    """Fetch an article and report transport and extraction provenance."""
    if max_bytes is not None:
        html_max_bytes = max_bytes
        pdf_max_bytes = max_bytes
        extracted_max_bytes = max_bytes

    _validate_public_http_url(url, resolver)
    if youtube_video_id(url) is not None:
        try:
            caption = fetch_youtube_caption(
                url,
                max_text_bytes=extracted_max_bytes,
            )
        except YoutubeCaptionError as exc:
            raise ArticleFetchError(
                str(exc),
                error_code=exc.error_code,
                method="youtube_caption",
                extractor="yt_dlp",
            ) from exc
        LOGGER.info(
            "component=article_fetch method=youtube_caption extractor=yt_dlp "
            "status=success language=%s generated=%s",
            caption.language,
            str(caption.generated).lower(),
        )
        return ArticleFetchResult(
            text=caption.text,
            method="youtube_caption",
            extractor="yt_dlp",
        )

    github_repository = _github_repository(url)
    if github_repository is not None:
        owner, repository = github_repository
        text = fetch_github_readme_text(
            owner,
            repository,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=extracted_max_bytes,
        )
        LOGGER.info(
            "component=article_fetch method=github_readme extractor=plain_text "
            "status=success"
        )
        return ArticleFetchResult(
            text=text,
            method="github_readme",
            extractor="plain_text",
        )

    github_blob = _github_blob(url)
    if github_blob is not None:
        owner, repository, ref, path = github_blob
        return fetch_github_blob(
            owner,
            repository,
            ref,
            path,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            html_max_bytes=html_max_bytes,
            pdf_max_bytes=pdf_max_bytes,
            extracted_max_bytes=extracted_max_bytes,
            pdf_max_pages=pdf_max_pages,
            pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
            pdf_address_space_bytes=pdf_address_space_bytes,
        )

    direct_request = Request(
        url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/html,text/plain,application/pdf;q=0.9",
        },
    )
    open_request = opener or _build_safe_opener(resolver).open

    try:
        result = _fetch_direct_response(
            direct_request,
            opener=open_request,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            html_max_bytes=html_max_bytes,
            pdf_max_bytes=pdf_max_bytes,
            extracted_max_bytes=extracted_max_bytes,
            pdf_max_pages=pdf_max_pages,
            pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
            pdf_address_space_bytes=pdf_address_space_bytes,
        )
    except HTTPError as exc:
        if not _is_cloudflare_challenge(exc):
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code=f"http_{exc.code}",
                method="direct",
            ) from exc
        LOGGER.warning(
            "component=article_fetch method=direct status=cloudflare_challenge "
            "fallback=jina"
        )
        return _fetch_jina_fallback(
            url,
            direct_failure="cloudflare challenge",
            fallback_reason="cloudflare_challenge",
            opener=open_request,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=extracted_max_bytes,
        )
    except ArticleFetchError as exc:
        if exc.error_code == "challenge_page":
            LOGGER.warning(
                "component=article_fetch method=direct status=challenge_page "
                "fallback=jina"
            )
            return _fetch_jina_fallback(
                url,
                direct_failure="browser verification challenge page",
                fallback_reason="challenge_page",
                opener=open_request,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                max_bytes=extracted_max_bytes,
            )
        if exc.error_code == "empty_content" and exc.extractor == "trafilatura":
            LOGGER.warning(
                "component=article_fetch method=direct extractor=trafilatura "
                "status=empty_content fallback=jina"
            )
            return _fetch_jina_fallback(
                url,
                direct_failure="trafilatura empty_content",
                fallback_reason="empty_content",
                opener=open_request,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                max_bytes=extracted_max_bytes,
            )
        raise ArticleFetchError(
            f"direct article retrieval failed: {exc}",
            error_code=exc.error_code,
            method="direct",
            extractor=exc.extractor,
            fallback_attempted=exc.fallback_attempted,
            fallback_reason=exc.fallback_reason,
        ) from exc
    except URLError as exc:
        if _is_tls_issuer_unavailable(exc):
            LOGGER.warning(
                "component=article_fetch method=direct "
                "status=tls_issuer_unavailable fallback=jina"
            )
            return _fetch_jina_fallback(
                url,
                direct_failure="TLS issuer unavailable",
                fallback_reason="tls_issuer_unavailable",
                opener=open_request,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                max_bytes=extracted_max_bytes,
            )
        raise ArticleFetchError(
            f"direct article request failed: {exc}",
            error_code="request_failed",
            method="direct",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"direct article request failed: {exc}",
            error_code="request_failed",
            method="direct",
        ) from exc

    LOGGER.info(
        "component=article_fetch method=direct extractor=%s status=success",
        result.extractor,
    )
    return result


def _fetch_jina_fallback(
    url: str,
    *,
    direct_failure: str,
    fallback_reason: str,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
) -> ArticleFetchResult:
    try:
        text = fetch_jina_reader_text(
            url,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except ArticleFetchError as jina_exc:
        raise ArticleFetchError(
            f"article retrieval failed: direct={direct_failure}; jina={jina_exc}",
            error_code=jina_exc.error_code,
            method="jina",
            extractor="jina",
            fallback_attempted=True,
            fallback_reason=fallback_reason,
        ) from jina_exc
    LOGGER.info(
        "component=article_fetch method=jina extractor=jina status=success "
        "fallback_reason=%s",
        fallback_reason,
    )
    return ArticleFetchResult(
        text=text,
        method="jina",
        extractor="jina",
        fallback_reason=fallback_reason,
    )


def fetch_github_readme_text(
    owner: str,
    repository: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> str:
    """Fetch the preferred README for one public GitHub repository."""
    api_url = f"{GITHUB_API_BASE_URL}/repos/{owner}/{repository}/readme"
    _validate_public_http_url(api_url, resolver)
    request = Request(
        api_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": GITHUB_RAW_CONTENT_TYPE,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        },
    )
    open_request = opener or _build_safe_opener(resolver).open

    try:
        return _fetch_bounded_text_response(
            request,
            opener=open_request,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            accepted_content_types={GITHUB_RAW_CONTENT_TYPE, "text/plain"},
        )
    except HTTPError as exc:
        raise ArticleFetchError(
            f"GitHub README API request failed: {exc}",
            error_code=f"http_{exc.code}",
            method="github_readme",
            extractor="plain_text",
        ) from exc
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"GitHub README retrieval failed: {exc}",
            error_code=exc.error_code,
            method="github_readme",
            extractor="plain_text",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"GitHub README API request failed: {exc}",
            error_code="request_failed",
            method="github_readme",
            extractor="plain_text",
        ) from exc


def fetch_github_blob(
    owner: str,
    repository: str,
    ref: str,
    path: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    html_max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    pdf_max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    extracted_max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    pdf_max_pages: int = DEFAULT_MAX_PDF_PAGES,
    pdf_parse_timeout_seconds: int = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    pdf_address_space_bytes: int = DEFAULT_PDF_ADDRESS_SPACE_BYTES,
) -> ArticleFetchResult:
    """Fetch the exact file behind a standard public GitHub blob URL."""
    raw_url = _github_raw_url(owner, repository, ref, path)
    _validate_public_http_url(raw_url, resolver)
    request = Request(
        raw_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "application/octet-stream",
        },
    )
    open_request = opener or _build_safe_opener(resolver).open
    expects_pdf = path.lower().endswith(".pdf")

    try:
        with open_request(request, timeout=timeout_seconds) as response:
            _validate_public_http_url(response.geturl(), resolver)
            content_type = response.headers.get_content_type().lower()
            raw_limit = pdf_max_bytes if expects_pdf else html_max_bytes
            payload = _read_bounded(response, raw_limit)
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        error_code = "github_file_not_found" if exc.code == 404 else f"http_{exc.code}"
        raise ArticleFetchError(
            f"GitHub raw file request failed: {exc}",
            error_code=error_code,
            method="github_raw",
        ) from exc
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"GitHub raw file retrieval failed: {exc}",
            error_code=exc.error_code,
            method="github_raw",
            extractor=exc.extractor,
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"GitHub raw file request failed: {exc}",
            error_code="request_failed",
            method="github_raw",
        ) from exc

    try:
        result = _extract_response_payload(
            payload,
            content_type=content_type,
            charset=charset,
            method="github_raw",
            extracted_max_bytes=extracted_max_bytes,
            pdf_max_pages=pdf_max_pages,
            pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
            pdf_address_space_bytes=pdf_address_space_bytes,
            expects_pdf=expects_pdf,
            allow_octet_stream_pdf=True,
        )
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"GitHub raw file extraction failed: {exc}",
            error_code=exc.error_code,
            method="github_raw",
            extractor=exc.extractor,
            fallback_attempted=exc.fallback_attempted,
            fallback_reason=exc.fallback_reason,
        ) from exc

    LOGGER.info(
        "component=article_fetch method=github_raw extractor=%s status=success",
        result.extractor,
    )
    return result


def fetch_jina_reader_text(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
) -> str:
    """Fetch and validate one bounded Jina Reader JSON response."""
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
        return text
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


def _fetch_direct_response(
    request: Request,
    *,
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
    expects_pdf = urlparse(request.full_url).path.lower().endswith(".pdf")
    with opener(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
        _validate_public_http_url(final_url, resolver)
        if _headers_indicate_challenge(response.headers):
            raise ArticleFetchError(
                "article response was a browser verification challenge page",
                error_code="challenge_page",
            )
        content_type = response.headers.get_content_type().lower()
        if content_type == "text/html":
            raw_limit = html_max_bytes
        elif content_type == "application/pdf":
            raw_limit = pdf_max_bytes
        elif content_type in PDF_CONTENT_TYPES and expects_pdf:
            raw_limit = pdf_max_bytes
        elif content_type == "text/plain":
            raw_limit = extracted_max_bytes
        else:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}",
                error_code="unsupported_content_type",
            )
        payload = _read_bounded(response, raw_limit)
        charset = response.headers.get_content_charset() or "utf-8"

    if content_type == "text/html":
        markup = payload.decode(charset, errors="replace")
        if _is_challenge_page(final_url, raw_html=markup):
            raise ArticleFetchError(
                "article response was a browser verification challenge page",
                error_code="challenge_page",
                extractor="trafilatura",
            )

    result = _extract_response_payload(
        payload,
        content_type=content_type,
        charset=charset,
        method="direct",
        extracted_max_bytes=extracted_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
        expects_pdf=expects_pdf,
        allow_octet_stream_pdf=True,
    )
    if _is_challenge_page(final_url, text=result.text):
        raise ArticleFetchError(
            "extracted article text was a browser verification challenge page",
            error_code="challenge_page",
            extractor=result.extractor,
        )
    return result


def _extract_response_payload(
    payload: bytes,
    *,
    content_type: str,
    charset: str,
    method: str,
    extracted_max_bytes: int,
    pdf_max_pages: int,
    pdf_parse_timeout_seconds: int,
    pdf_address_space_bytes: int,
    expects_pdf: bool = False,
    allow_octet_stream_pdf: bool = False,
) -> ArticleFetchResult:
    has_pdf_magic = payload.startswith(b"%PDF-")
    is_pdf_type = content_type == "application/pdf" or (
        allow_octet_stream_pdf and content_type in PDF_CONTENT_TYPES and expects_pdf
    )

    if expects_pdf and content_type not in PDF_CONTENT_TYPES:
        raise ArticleFetchError(
            f"PDF content type does not match: {content_type}",
            error_code="pdf_content_type_mismatch",
            extractor="pypdf",
        )
    if is_pdf_type:
        if not has_pdf_magic:
            raise ArticleFetchError(
                "PDF response is missing the %PDF- file signature",
                error_code="pdf_magic_mismatch",
                extractor="pypdf",
            )
        text, extractor, fallback_reason = _extract_pdf_payload(
            payload,
            max_pages=pdf_max_pages,
            max_text_bytes=extracted_max_bytes,
            pypdf_timeout_seconds=pdf_parse_timeout_seconds,
            address_space_bytes=pdf_address_space_bytes,
        )
        return ArticleFetchResult(
            text=text,
            method=method,
            extractor=extractor,
            fallback_reason=fallback_reason,
        )

    if has_pdf_magic:
        raise ArticleFetchError(
            f"PDF file returned non-PDF content type: {content_type}",
            error_code="pdf_content_type_mismatch",
            extractor="pypdf",
        )

    if content_type == "text/html":
        try:
            text = extract_html(payload.decode(charset, errors="replace"))
        except Exception as exc:
            raise ArticleFetchError(
                f"HTML extraction failed: {exc}",
                error_code="html_extraction_failed",
                extractor="trafilatura",
            ) from exc
        extractor = "trafilatura"
    elif content_type.startswith("text/") or content_type in {
        "application/json",
        GITHUB_RAW_CONTENT_TYPE,
    }:
        text = _normalize_document_text(
            payload.decode(charset, errors="replace")
        )
        extractor = "plain_text"
    else:
        raise ArticleFetchError(
            f"unsupported article content type: {content_type}",
            error_code="unsupported_content_type",
        )

    if not text:
        raise ArticleFetchError(
            "article response contained no extractable text",
            error_code="empty_content",
            extractor=extractor,
        )
    _enforce_extracted_limit(text, extracted_max_bytes, extractor=extractor)
    return ArticleFetchResult(text=text, method=method, extractor=extractor)


def _extract_pdf_payload(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    pypdf_timeout_seconds: int,
    address_space_bytes: int,
) -> tuple[str, str, str]:
    credential_state = adobe_credentials_status(os.environ)
    fallback_reason = ""
    if credential_state == "configured":
        try:
            text = _extract_pdf_with_adobe_in_subprocess(
                payload,
                max_pages=max_pages,
                max_text_bytes=max_text_bytes,
                timeout_seconds=DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS,
                address_space_bytes=address_space_bytes,
            )
        except ArticleFetchError as exc:
            fallback_reason = exc.error_code
            LOGGER.warning(
                "component=pdf_extract extractor=adobe_pdf_to_markdown "
                "status=failed code=%s fallback=pypdf",
                exc.error_code,
            )
        else:
            LOGGER.info(
                "component=pdf_extract extractor=adobe_pdf_to_markdown "
                "status=success"
            )
            return text, "adobe_pdf_to_markdown", ""
    elif credential_state == "incomplete":
        fallback_reason = "adobe_pdf_credentials_incomplete"
        LOGGER.warning(
            "component=pdf_extract extractor=adobe_pdf_to_markdown "
            "status=disabled code=%s fallback=pypdf",
            fallback_reason,
        )

    try:
        text = _extract_pdf_in_subprocess(
            payload,
            max_pages=max_pages,
            max_text_bytes=max_text_bytes,
            timeout_seconds=pypdf_timeout_seconds,
            address_space_bytes=address_space_bytes,
        )
    except ArticleFetchError as exc:
        if not fallback_reason:
            raise
        raise ArticleFetchError(
            str(exc),
            error_code=exc.error_code,
            extractor=exc.extractor,
            fallback_attempted=True,
            fallback_reason=fallback_reason,
        ) from exc
    return text, "pypdf", fallback_reason


def _extract_pdf_with_adobe_in_subprocess(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    timeout_seconds: int,
    address_space_bytes: int,
) -> str:
    command = [
        sys.executable,
        "-m",
        "daily_brief.adobe_pdf_extractor",
        str(max_pages),
        str(max_text_bytes),
        str(ADOBE_CONNECT_TIMEOUT_MS),
        str(ADOBE_READ_TIMEOUT_MS),
        str(address_space_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArticleFetchError(
            "Adobe PDF to Markdown timed out",
            error_code="adobe_pdf_timeout",
            extractor="adobe_pdf_to_markdown",
        ) from exc

    if completed.returncode != 0:
        raise ArticleFetchError(
            "Adobe PDF worker subprocess failed",
            error_code="adobe_pdf_worker_failed",
            extractor="adobe_pdf_to_markdown",
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArticleFetchError(
            "Adobe PDF worker subprocess returned an invalid result",
            error_code="adobe_pdf_worker_failed",
            extractor="adobe_pdf_to_markdown",
        ) from exc

    if result.get("status") != "success":
        raise ArticleFetchError(
            str(result.get("message") or "Adobe PDF extraction failed"),
            error_code=str(
                result.get("error_code") or "adobe_pdf_conversion_failed"
            ),
            extractor="adobe_pdf_to_markdown",
        )
    text = str(result.get("text") or "").strip()
    if not text:
        raise ArticleFetchError(
            "Adobe PDF to Markdown returned empty content",
            error_code="adobe_pdf_empty_content",
            extractor="adobe_pdf_to_markdown",
        )
    _enforce_extracted_limit(
        text,
        max_text_bytes,
        extractor="adobe_pdf_to_markdown",
    )
    return text


def _extract_pdf_in_subprocess(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    timeout_seconds: int,
    address_space_bytes: int,
) -> str:
    command = [
        sys.executable,
        "-m",
        "daily_brief.pdf_extractor",
        str(max_pages),
        str(max_text_bytes),
        str(address_space_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArticleFetchError(
            "PDF parsing timed out",
            error_code="pdf_parse_timeout",
            extractor="pypdf",
        ) from exc

    if completed.returncode != 0:
        diagnostic = _normalize_single_line(
            completed.stderr.decode("utf-8", errors="replace")
        )[:500]
        raise ArticleFetchError(
            f"PDF parser subprocess failed: {diagnostic or 'no diagnostic'}",
            error_code="pdf_parse_failed",
            extractor="pypdf",
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArticleFetchError(
            "PDF parser subprocess returned an invalid result",
            error_code="pdf_parse_failed",
            extractor="pypdf",
        ) from exc

    if result.get("status") != "success":
        raise ArticleFetchError(
            str(result.get("message") or "PDF extraction failed"),
            error_code=str(result.get("error_code") or "pdf_parse_failed"),
            extractor="pypdf",
        )
    text = str(result.get("text") or "").strip()
    if not text:
        raise ArticleFetchError(
            "PDF contains no extractable text",
            error_code="pdf_no_extractable_text",
            extractor="pypdf",
        )
    _enforce_extracted_limit(text, max_text_bytes, extractor="pypdf")
    return text


def _fetch_bounded_text_response(
    request: Request,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    accepted_content_types: set[str],
) -> str:
    with opener(request, timeout=timeout_seconds) as response:
        _validate_public_http_url(response.geturl(), resolver)
        content_type = response.headers.get_content_type().lower()
        if content_type not in accepted_content_types:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}",
                error_code="unsupported_content_type",
            )
        payload = _read_bounded(response, max_bytes)
        charset = response.headers.get_content_charset() or "utf-8"

    text = _normalize_document_text(payload.decode(charset, errors="replace"))
    if not text:
        raise ArticleFetchError(
            "article response contained no extractable text",
            error_code="empty_content",
        )
    _enforce_extracted_limit(text, max_bytes, extractor="plain_text")
    return text


def _read_bounded(response, max_bytes: int) -> bytes:
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ArticleFetchError(
            "article response is too large",
            error_code="response_too_large",
        )
    return payload


def _enforce_extracted_limit(text: str, max_bytes: int, *, extractor: str) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise ArticleFetchError(
            "extracted article text is too large",
            error_code="extracted_content_too_large",
            extractor=extractor,
        )


def _normalize_single_line(value: str) -> str:
    return " ".join(value.split())


def _normalize_extracted_blocks(value: str) -> str:
    """Normalize extractor text while retaining one line per content block."""
    lines = [" ".join(line.split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _normalize_document_text(value: str) -> str:
    """Normalize line endings without damaging Markdown or preformatted text."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _github_repository(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if not _is_standard_github_url(parsed):
        return None
    path_parts = parsed.path.strip("/").split("/")
    if len(path_parts) != 2:
        return None
    owner, repository = (unquote(part) for part in path_parts)
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        return None
    if not all(
        GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part) for part in (owner, repository)
    ):
        return None
    return owner, repository


def _github_blob(url: str) -> tuple[str, str, str, str] | None:
    parsed = urlparse(url)
    if not _is_standard_github_url(parsed):
        return None
    encoded_parts = parsed.path.strip("/").split("/")
    if len(encoded_parts) < 3 or unquote(encoded_parts[2]) != "blob":
        return None
    if len(encoded_parts) < 5:
        raise ArticleFetchError(
            "unsupported GitHub blob URL",
            error_code="unsupported_github_path",
            method="github_raw",
        )

    owner, repository, _, ref, *path_parts = (unquote(part) for part in encoded_parts)
    if not all(
        GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part) for part in (owner, repository)
    ):
        raise ArticleFetchError(
            "unsupported GitHub blob repository path",
            error_code="unsupported_github_path",
            method="github_raw",
        )
    if not ref or ref in {".", ".."} or "/" in ref:
        raise ArticleFetchError(
            "GitHub blob refs containing slashes are unsupported",
            error_code="unsupported_github_path",
            method="github_raw",
        )
    if any(not part or part in {".", ".."} or "/" in part for part in path_parts):
        raise ArticleFetchError(
            "unsupported GitHub blob file path",
            error_code="unsupported_github_path",
            method="github_raw",
        )
    return owner, repository, ref, "/".join(path_parts)


def _github_raw_url(owner: str, repository: str, ref: str, path: str) -> str:
    quoted_path = "/".join(quote(part, safe="") for part in path.split("/"))
    return "/".join(
        (
            GITHUB_RAW_BASE_URL,
            quote(owner, safe=""),
            quote(repository, safe=""),
            quote(ref, safe=""),
            quoted_path,
        )
    )


def _is_standard_github_url(parsed) -> bool:
    return bool(
        parsed.hostname
        and parsed.hostname.lower() == "github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.port is None
    )


def _is_cloudflare_challenge(error: HTTPError) -> bool:
    return _headers_indicate_challenge(error.headers)


def _headers_indicate_challenge(headers) -> bool:
    return bool(
        headers and headers.get("cf-mitigated", "").strip().lower() == "challenge"
    )


def _is_challenge_page(
    url: str,
    *,
    raw_html: str = "",
    text: str = "",
) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)
    if path == "/challenge" and "redirect" in query:
        return True
    if path.startswith("/cdn-cgi/challenge-platform"):
        return True

    normalized_text = " ".join(text.lower().split())
    if any(
        all(marker in normalized_text for marker in markers)
        for markers in CHALLENGE_TEXT_MARKER_GROUPS
    ):
        return True

    normalized_html = raw_html.lower()
    return any(
        all(marker in normalized_html for marker in markers)
        for markers in CHALLENGE_HTML_MARKER_GROUPS
    )


def _is_tls_issuer_unavailable(error: URLError) -> bool:
    reason = error.reason
    return (
        isinstance(reason, ssl.SSLCertVerificationError)
        and reason.verify_code == 20
    )


def _build_safe_opener(resolver):
    return build_opener(
        ProxyHandler({}),
        _SafeRedirectHandler(resolver),
        _PinnedHTTPHandler(resolver),
        _PinnedHTTPSHandler(resolver),
    )


def _create_public_connection(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
    *,
    resolver,
):
    host, port = address
    addresses = _resolve_public_addresses(host, port, resolver)
    errors = []
    for address_family, socket_type, protocol, _, socket_address in addresses:
        connection = None
        try:
            connection = socket.socket(address_family, socket_type, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            errors.append(exc)
            if connection is not None:
                connection.close()
    if errors:
        raise errors[-1]
    raise OSError("hostname did not resolve to a usable public address")


def _resolve_public_addresses(host: str, port: int, resolver):
    try:
        addresses = resolver(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("hostname did not resolve")
        for address_info in addresses:
            address = address_info[4][0].split("%", 1)[0]
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("destination is not public")
    except (OSError, ValueError) as exc:
        raise ArticleFetchError(
            "article URL is not a safe public HTTP destination",
            error_code="unsafe_url",
        ) from exc
    return addresses


def _validate_public_http_url(url: str, resolver) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ArticleFetchError(
            "article URL is not a safe public HTTP destination",
            error_code="unsafe_url",
        ) from exc
    _resolve_public_addresses(parsed.hostname, port, resolver)
