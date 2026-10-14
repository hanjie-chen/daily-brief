from __future__ import annotations

import json
import os
import re
import socket
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request

from .contracts import (
    ArticleFetchError,
    ArticleFetchResult,
    DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS,
    DEFAULT_MAX_EXTRACTED_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_PDF_ADDRESS_SPACE_BYTES,
    DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    LOGGER,
)
from .http_safety import (
    _build_safe_opener,
    _enforce_extracted_limit,
    _read_bounded,
    _validate_public_http_url,
)
from .responses import (
    GITHUB_RAW_CONTENT_TYPE,
    _extract_response_payload,
    _fetch_bounded_text_response,
)
from .source_evidence import SourceEvidence


GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_RAW_BASE_URL = "https://raw.githubusercontent.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_REPOSITORY_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _github_api_request(url: str, *, accept: str) -> Request:
    """Attach optional credentials only to the initial GitHub API request."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https" or parsed.netloc != "api.github.com"
        or not parsed.path.startswith("/repos/")
    ):
        raise ArticleFetchError("invalid GitHub API endpoint", error_code="unsafe_url")
    request = Request(url, headers={
        "User-Agent": "daily-brief/0.1",
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    })
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        if not re.fullmatch(r"[!-~]+", token):
            raise ArticleFetchError(
                "GITHUB_TOKEN must contain only visible ASCII characters without spaces",
                error_code="invalid_configuration",
            )
        # urllib drops unredirected headers on redirects, including to raw file
        # hosts. Never send this token to Reader, archives, or article sites.
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    return request


def fetch_github_issue(
    owner: str,
    repository: str,
    number: str,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    extracted_max_bytes: int,
) -> ArticleFetchResult:
    """Read one issue's Markdown body; transport recovery belongs to fetch.py."""
    api_url = f"{GITHUB_API_BASE_URL}/repos/{owner}/{repository}/issues/{number}"
    _validate_public_http_url(api_url, resolver)
    request = _github_api_request(api_url, accept="application/vnd.github+json")
    request._redirect_validator = _validate_github_issue_api_url
    with opener(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
        _validate_public_http_url(final_url, resolver)
        final_identity = _validate_github_issue_api_url(final_url)
        payload = _read_bounded(response, max_bytes)
        if response.headers.get_content_type().lower() not in {
            "application/json", "application/vnd.github+json",
        }:
            raise ArticleFetchError(
                "GitHub issue API returned a non-JSON response",
                error_code="invalid_response", extractor="github_api",
            )
    try:
        issue = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ArticleFetchError(
            "GitHub issue API returned invalid JSON",
            error_code="invalid_response", extractor="github_api",
        ) from exc
    page_url = issue.get("html_url") if isinstance(issue, dict) else None
    identity = _github_issue(page_url) if isinstance(page_url, str) else None
    api_identity = _github_issue_api_identity(issue.get("url")) if isinstance(issue, dict) else None
    if (
        not isinstance(issue, dict)
        or type(issue.get("number")) is not int
        or str(issue["number"]) != final_identity[-1]
        or identity is None
        or api_identity != ("repos", identity[0].casefold(), identity[1].casefold(), identity[2])
        or identity[2] != str(issue["number"])
        or (final_identity[0] == "repos" and api_identity != final_identity)
        or "pull_request" in issue
        or not isinstance(issue.get("title"), str)
        or not issue["title"].strip()
        or (issue.get("body") is not None and not isinstance(issue["body"], str))
        or "body" not in issue
    ):
        raise ArticleFetchError(
            "GitHub issue API response does not identify the requested issue",
            error_code="invalid_response", extractor="github_api",
        )
    body = (issue["body"] or "").strip()
    if not body:
        raise ArticleFetchError(
            "GitHub issue API returned an empty body",
            error_code="empty_content", extractor="github_api",
        )
    title = " ".join(issue["title"].split())[:512]
    text = (
        "Page metadata (publisher-provided context, not article body):\n"
        f"title: {title}\n\nExtracted body:\n{body}"
    )
    _enforce_extracted_limit(text, extracted_max_bytes, extractor="github_api")
    created_at = issue.get("created_at")
    published_at = created_at.strip() if isinstance(created_at, str) and len(created_at) <= 64 else ""
    return ArticleFetchResult(
        text=text, method="github_issue", extractor="github_api",
        retrieved_url=page_url,
        source_evidence=SourceEvidence(title=title, published_at=published_at),
    )


def _github_issue_api_identity(url: object) -> tuple[str, ...] | None:
    """Recognize named and numeric-repository issue endpoints on GitHub only."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or parsed.netloc.casefold() != "api.github.com"
        or parsed.params or parsed.query or parsed.fragment
    ):
        return None
    parts = parsed.path.removesuffix("/").split("/")
    if not re.fullmatch(r"[1-9][0-9]*", parts[-1]):
        return None
    if len(parts) == 6 and parts[:2] == ["", "repos"] and parts[4] == "issues":
        owner, repository = parts[2:4]
        if all(GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part) and part not in {".", ".."}
               for part in (owner, repository)):
            return "repos", owner.casefold(), repository.casefold(), parts[-1]
    if (
        len(parts) == 5 and parts[:2] == ["", "repositories"] and parts[3] == "issues"
        and re.fullmatch(r"[1-9][0-9]*", parts[2])
    ):
        return "repositories", parts[2], parts[-1]
    return None


def _validate_github_issue_api_url(url: str) -> tuple[str, ...]:
    identity = _github_issue_api_identity(url)
    if identity is None:
        raise ArticleFetchError(
            "GitHub issue API redirected to an unsupported endpoint",
            error_code="invalid_response", extractor="github_api",
        )
    return identity


def _github_issue(url: str) -> tuple[str, str, str] | None:
    """Recognize issue pages only, never lists, PRs, comments, or lookalike hosts."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not _is_standard_github_url(parsed):
            return None
    except ValueError:
        return None
    parts = parsed.path.removesuffix("/").split("/")
    if len(parts) != 5 or parts[0] or parts[3] != "issues":
        return None
    _, owner, repository, _, number = parts
    if (
        not all(GITHUB_REPOSITORY_PART_PATTERN.fullmatch(part) and part not in {".", ".."}
                for part in (owner, repository))
        or not re.fullmatch(r"[1-9][0-9]*", number)
    ):
        return None
    return owner, repository, number


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
    request = _github_api_request(api_url, accept=GITHUB_RAW_CONTENT_TYPE)
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
    adobe_pdf_enabled: bool = True,
    adobe_pdf_timeout_seconds: int = DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS,
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
            adobe_pdf_enabled=adobe_pdf_enabled,
            adobe_pdf_timeout_seconds=adobe_pdf_timeout_seconds,
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
