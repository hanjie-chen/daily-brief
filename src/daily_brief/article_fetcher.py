from __future__ import annotations

import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_BYTES = 256 * 1024
JINA_READER_BASE_URL = "https://r.jina.ai/"
JINA_CACHE_TOLERANCE_SECONDS = 5 * 60
IGNORED_ELEMENTS = {"script", "style", "noscript", "svg", "template"}
LOGGER = logging.getLogger(__name__)


class ArticleFetchError(RuntimeError):
    pass


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
    _validate_public_http_url(url, resolver)
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
            raise ArticleFetchError(f"direct article request failed: {exc}") from exc
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
                f"jina={jina_exc}"
            ) from jina_exc
        LOGGER.info("component=article_fetch method=jina status=success")
        return text
    except ArticleFetchError as exc:
        raise ArticleFetchError(f"direct article retrieval failed: {exc}") from exc
    except Exception as exc:
        raise ArticleFetchError(f"direct article request failed: {exc}") from exc

    LOGGER.info("component=article_fetch method=direct status=success")
    return text


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
    except ArticleFetchError:
        raise
    except Exception as exc:
        raise ArticleFetchError(f"Jina Reader request failed: {exc}") from exc


def _fetch_text_response(
    request: Request,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
) -> str:
    with opener(request, timeout=timeout_seconds) as response:
        _validate_public_http_url(response.geturl(), resolver)
        content_type = response.headers.get_content_type().lower()
        if content_type not in {"text/html", "text/plain"}:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}"
            )
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ArticleFetchError("article response is too large")
        charset = response.headers.get_content_charset() or "utf-8"

    decoded = payload.decode(charset, errors="replace")
    if content_type == "text/html":
        return extract_html(decoded)
    return " ".join(decoded.split())


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
        raise ArticleFetchError("article URL is not a safe public HTTP destination") from exc
