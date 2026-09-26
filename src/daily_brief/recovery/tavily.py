"""Tavily Search request and response handling shared by the recovery finders.

Each finder supplies its own query, result limit, domain filter, and error type;
this module owns the request shape, bounds, and error codes.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from http.client import HTTPResponse
from typing import Self
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = 10
TAVILY_MAX_RESPONSE_BYTES = 256 * 1024
MAX_RESULT_TITLE_CHARS = 500
MAX_RESULT_URL_CHARS = 2048

# Each finder raises its own error class; all share this constructor.
FinderError = Callable[..., Exception]


class TavilyFinder:
    """Shared construction for finders backed by Tavily Search."""

    provider = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        opener: Callable[..., HTTPResponse] = urlopen,
        timeout_seconds: int = TAVILY_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key.strip()
        self.opener = opener
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, **kwargs: object
    ) -> Self:
        environment = os.environ if env is None else env
        return cls(api_key=environment.get("TAVILY_API_KEY", ""), **kwargs)

    def _require_api_key(self, error_type: FinderError) -> None:
        if not self.api_key:
            raise error_type(
                "TAVILY_API_KEY is not configured", error_code="not_configured"
            )

    def _search(
        self,
        error_type: FinderError,
        *,
        query: str,
        max_results: int,
        exact_match: bool,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Return up to ``max_results`` stripped (title, url) pairs within bounds."""
        params: dict[str, object] = {
            "query": query,
            "search_depth": "basic",
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        if include_domains is not None:
            params["include_domains"] = include_domains
        if exclude_domains is not None:
            params["exclude_domains"] = exclude_domains
        params["auto_parameters"] = False
        params["exact_match"] = exact_match
        body = json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = Request(
            TAVILY_SEARCH_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "daily-brief/0.1",
            },
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                response_body = response.read(TAVILY_MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise error_type(
                f"Tavily Search returned HTTP {exc.code}",
                error_code="provider_http_error",
            ) from exc
        except (TimeoutError, URLError) as exc:
            raise error_type(
                "Tavily Search request failed",
                error_code="provider_request_failed",
            ) from exc
        if len(response_body) > TAVILY_MAX_RESPONSE_BYTES:
            raise error_type(
                "Tavily Search response exceeded the size limit",
                error_code="response_too_large",
            )
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error_type(
                "Tavily Search returned invalid JSON",
                error_code="malformed_response",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise error_type(
                "Tavily Search returned an invalid result envelope",
                error_code="malformed_response",
            )

        results: list[tuple[str, str]] = []
        for item in payload["results"][:max_results]:
            if not isinstance(item, dict):
                continue
            title, url = item.get("title"), item.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            title, url = title.strip(), url.strip()
            if (
                not title
                or not url
                or len(title) > MAX_RESULT_TITLE_CHARS
                or len(url) > MAX_RESULT_URL_CHARS
            ):
                continue
            results.append((title, url))
        return results
