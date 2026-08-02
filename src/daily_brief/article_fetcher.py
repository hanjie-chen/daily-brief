from __future__ import annotations

import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_BYTES = 256 * 1024
JINA_READER_BASE_URL = "https://r.jina.ai/"
JINA_CACHE_TOLERANCE_SECONDS = 5 * 60
GITHUB_API_BASE_URL = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_RAW_CONTENT_TYPE = "application/vnd.github.raw+json"
GITHUB_REPOSITORY_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
IGNORED_ELEMENTS = {"script", "style", "noscript", "svg", "template"}
LOGGER = logging.getLogger(__name__)


class ArticleFetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "fetch_failed",
        method: str = "",
        fallback_attempted: bool = False,
        fallback_reason: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.method = method
        self.fallback_attempted = fallback_attempted
        self.fallback_reason = fallback_reason


@dataclass(frozen=True)
class ArticleFetchResult:
    text: str
    method: str
    fallback_reason: str = ""


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in IGNORED_ELEMENTS:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in IGNORED_ELEMENTS and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl, self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def extract_html(markup: str) -> str:
    parser = _VisibleTextParser()
    parser.feed(markup)
    parser.close()
    text = " ".join(" ".join(parser.parts).split())
    return re.sub(r"\s+([,.;:!?，。；：！？])", r"\1", text)


def fetch_article_text(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> str:
    """Fetch article text while preserving the original string-returning API."""
    return fetch_article(
        url,
        opener=opener,
        resolver=resolver,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
    ).text


def fetch_article(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ArticleFetchResult:
    """Fetch an article and report the successful retrieval method."""
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
            max_bytes=max_bytes,
        )
        LOGGER.info("component=article_fetch method=github_readme status=success")
        return ArticleFetchResult(text=text, method="github_readme")

    direct_request = Request(
        url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/html,text/plain;q=0.9",
        },
    )
    if opener is None:
        opener = build_opener(_SafeRedirectHandler(resolver)).open

    try:
        text = _fetch_text_response(
            direct_request,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
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
                opener=opener,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
            )
        except ArticleFetchError as jina_exc:
            raise ArticleFetchError(
                "article retrieval failed: direct=cloudflare challenge; "
                f"jina={jina_exc}",
                error_code=jina_exc.error_code,
                method="jina",
                fallback_attempted=True,
                fallback_reason="cloudflare_challenge",
            ) from jina_exc
        LOGGER.info("component=article_fetch method=jina status=success")
        return ArticleFetchResult(
            text=text,
            method="jina",
            fallback_reason="cloudflare_challenge",
        )
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"direct article retrieval failed: {exc}",
            error_code=exc.error_code,
            method="direct",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"direct article request failed: {exc}",
            error_code="request_failed",
            method="direct",
        ) from exc

    LOGGER.info("component=article_fetch method=direct status=success")
    return ArticleFetchResult(text=text, method="direct")


def fetch_github_readme_text(
    owner: str,
    repository: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
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
    if opener is None:
        opener = build_opener(_SafeRedirectHandler(resolver)).open

    try:
        return _fetch_text_response(
            request,
            opener=opener,
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
        ) from exc
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            f"GitHub README retrieval failed: {exc}",
            error_code=exc.error_code,
            method="github_readme",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"GitHub README API request failed: {exc}",
            error_code="request_failed",
            method="github_readme",
        ) from exc


def fetch_jina_reader_text(
    url: str,
    *,
    opener=None,
    resolver=socket.getaddrinfo,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
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
    if opener is None:
        opener = build_opener(_SafeRedirectHandler(resolver)).open

    try:
        return _fetch_text_response(
            request,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except HTTPError as exc:
        raise ArticleFetchError(
            f"Jina Reader request failed: {exc}",
            error_code=f"http_{exc.code}",
            method="jina",
        ) from exc
    except ArticleFetchError as exc:
        raise ArticleFetchError(
            str(exc),
            error_code=exc.error_code,
            method="jina",
        ) from exc
    except Exception as exc:
        raise ArticleFetchError(
            f"Jina Reader request failed: {exc}",
            error_code="request_failed",
            method="jina",
        ) from exc


def _fetch_text_response(
    request: Request,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    accepted_content_types: set[str] | None = None,
) -> str:
    allowed_content_types = accepted_content_types or {"text/html", "text/plain"}
    with opener(request, timeout=timeout_seconds) as response:
        _validate_public_http_url(response.geturl(), resolver)
        content_type = response.headers.get_content_type().lower()
        if content_type not in allowed_content_types:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}",
                error_code="unsupported_content_type",
            )
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ArticleFetchError(
                "article response is too large",
                error_code="response_too_large",
            )
        charset = response.headers.get_content_charset() or "utf-8"

    decoded = payload.decode(charset, errors="replace")
    if content_type == "text/html":
        text = extract_html(decoded)
    else:
        text = " ".join(decoded.split())
    if not text:
        raise ArticleFetchError(
            "article response contained no visible text",
            error_code="empty_content",
        )
    return text


def _github_repository(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if (
        parsed.hostname is None
        or parsed.hostname.lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
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
