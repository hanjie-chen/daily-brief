from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import subprocess
import sys
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

import trafilatura

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_EXTRACTED_BYTES = 256 * 1024
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PDF_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 100
DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 60
DEFAULT_PDF_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
# Kept as a compatibility name for direct helper callers and tests.
DEFAULT_MAX_BYTES = DEFAULT_MAX_EXTRACTED_BYTES
JINA_READER_BASE_URL = "https://r.jina.ai/"
JINA_CACHE_TOLERANCE_SECONDS = 5 * 60
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_RAW_CONTENT_TYPE = "application/vnd.github.raw+json"
GITHUB_REPOSITORY_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
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


def extract_html(markup: str) -> str:
    """Extract article body text from HTML with the production settings."""
    extracted = trafilatura.extract(
        markup,
        include_comments=False,
        favor_precision=True,
        include_tables=True,
    )
    return _normalize_text(extracted or "")


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
    open_request = opener or build_opener(_SafeRedirectHandler(resolver)).open

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
        try:
            text = fetch_jina_reader_text(
                url,
                opener=open_request,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                max_bytes=extracted_max_bytes,
            )
        except ArticleFetchError as jina_exc:
            raise ArticleFetchError(
                "article retrieval failed: direct=cloudflare challenge; "
                f"jina={jina_exc}",
                error_code=jina_exc.error_code,
                method="jina",
                extractor="jina",
                fallback_attempted=True,
                fallback_reason="cloudflare_challenge",
            ) from jina_exc
        LOGGER.info(
            "component=article_fetch method=jina extractor=jina status=success"
        )
        return ArticleFetchResult(
            text=text,
            method="jina",
            extractor="jina",
            fallback_reason="cloudflare_challenge",
        )
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"direct article retrieval failed: {exc}",
            error_code=exc.error_code,
            method="direct",
            extractor=exc.extractor,
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
    open_request = opener or build_opener(_SafeRedirectHandler(resolver)).open

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
    open_request = opener or build_opener(_SafeRedirectHandler(resolver)).open
    expects_pdf = path.lower().endswith(".pdf")

    try:
        with open_request(request, timeout=timeout_seconds) as response:
            _validate_public_http_url(response.geturl(), resolver)
            content_type = response.headers.get_content_type().lower()
            raw_limit = pdf_max_bytes if expects_pdf else html_max_bytes
            payload = _read_bounded(response, raw_limit)
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        error_code = (
            "github_file_not_found" if exc.code == 404 else f"http_{exc.code}"
        )
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
    """Fetch one public article through Jina Reader as bounded plain text."""
    _validate_public_http_url(url, resolver)
    reader_url = f"{JINA_READER_BASE_URL}{url}"
    _validate_public_http_url(reader_url, resolver)
    request = Request(
        reader_url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/plain",
            "X-Cache-Tolerance": str(JINA_CACHE_TOLERANCE_SECONDS),
        },
    )
    open_request = opener or build_opener(_SafeRedirectHandler(resolver)).open

    try:
        return _fetch_bounded_text_response(
            request,
            opener=open_request,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            accepted_content_types={"text/plain"},
        )
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
    with opener(request, timeout=timeout_seconds) as response:
        _validate_public_http_url(response.geturl(), resolver)
        content_type = response.headers.get_content_type().lower()
        if content_type == "text/html":
            raw_limit = html_max_bytes
        elif content_type == "application/pdf":
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

    return _extract_response_payload(
        payload,
        content_type=content_type,
        charset=charset,
        method="direct",
        extracted_max_bytes=extracted_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
    )


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
        text = _extract_pdf_in_subprocess(
            payload,
            max_pages=pdf_max_pages,
            max_text_bytes=extracted_max_bytes,
            timeout_seconds=pdf_parse_timeout_seconds,
            address_space_bytes=pdf_address_space_bytes,
        )
        return ArticleFetchResult(text=text, method=method, extractor="pypdf")

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
        text = _normalize_text(payload.decode(charset, errors="replace"))
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
        diagnostic = _normalize_text(
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

    text = _normalize_text(payload.decode(charset, errors="replace"))
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


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


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
        GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part)
        for part in (owner, repository)
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

    owner, repository, _, ref, *path_parts = (
        unquote(part) for part in encoded_parts
    )
    if not all(
        GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part)
        for part in (owner, repository)
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
    headers = error.headers
    return bool(
        headers
        and headers.get("cf-mitigated", "").strip().lower() == "challenge"
    )


def _validate_public_http_url(url: str, resolver) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
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
