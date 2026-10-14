from __future__ import annotations

import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from .budget import budgeted_opener, budgeted_sleep, remaining_seconds, with_retrieval_budget
from .challenges import _is_network_timeout, _is_tls_issuer_unavailable
from .contracts import (
    ArticleFetchError,
    ArticleFetchPolicy,
    ArticleFetchResult,
    DEFAULT_MAX_EXTRACTED_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_PDF_ADDRESS_SPACE_BYTES,
    DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DIRECT_RETRY_DELAY_SECONDS,
    LOGGER,
    SUMMARY_FETCH_POLICY,
)
from .github import (
    _github_blob,
    _github_issue,
    _github_repository,
    fetch_github_blob,
    fetch_github_issue,
    fetch_github_readme_text,
)
from .http_safety import _build_safe_opener, _validate_public_http_url
from .recovery import (
    _JinaFallbackContext,
    _article_error_fallback_reason,
    _http_fallback_reason,
    _recover_direct_failure,
)
from .responses import _fetch_direct_response
from .youtube_captions import (
    YoutubeCaptionError,
    fetch_youtube_caption,
    youtube_video_id,
)


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
    wayback_not_before: datetime | None = None,
    wayback_not_after: datetime | None = None,
    sleeper=time.sleep,
    policy: ArticleFetchPolicy = SUMMARY_FETCH_POLICY,
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
        wayback_not_before=wayback_not_before,
        wayback_not_after=wayback_not_after,
        sleeper=sleeper,
        policy=policy,
    ).text


@with_retrieval_budget
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
    wayback_not_before: datetime | None = None,
    wayback_not_after: datetime | None = None,
    sleeper=time.sleep,
    policy: ArticleFetchPolicy = SUMMARY_FETCH_POLICY,
) -> ArticleFetchResult:
    """Fetch an article and report transport and extraction provenance."""
    if max_bytes is not None:
        html_max_bytes = max_bytes
        pdf_max_bytes = max_bytes
        extracted_max_bytes = max_bytes

    _validate_public_http_url(url, resolver)
    opener = budgeted_opener(opener or _build_safe_opener(resolver).open)
    if youtube_video_id(url) is not None:
        if not policy.youtube_enabled:
            raise ArticleFetchError(
                "YouTube is skipped during classification retrieval",
                error_code="youtube_skipped_in_classification",
                method="youtube_caption",
                extractor="yt_dlp",
                attempts=0,
            )
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
            source_evidence=caption.source_evidence,
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
        result = fetch_github_blob(
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
            adobe_pdf_enabled=policy.adobe_pdf_enabled,
            adobe_pdf_timeout_seconds=policy.adobe_pdf_timeout_seconds,
        )
        return result

    github_issue = _github_issue(url)
    direct_method = "github_issue" if github_issue else "direct"
    direct_request = Request(
        url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/html,text/plain,application/pdf;q=0.9",
        },
    )
    open_request = opener
    fallback_context = _JinaFallbackContext(
        url=url,
        opener=open_request,
        resolver=resolver,
        timeout_seconds=timeout_seconds,
        extracted_max_bytes=extracted_max_bytes,
        html_max_bytes=html_max_bytes,
        pdf_max_bytes=pdf_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
        wayback_not_before=wayback_not_before,
        wayback_not_after=wayback_not_after,
        policy=policy,
        method=direct_method,
    )

    fetch_direct = partial(
        _fetch_direct_response,
        opener=open_request,
        resolver=resolver,
        timeout_seconds=timeout_seconds,
        html_max_bytes=html_max_bytes,
        pdf_max_bytes=pdf_max_bytes,
        extracted_max_bytes=extracted_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
        adobe_pdf_enabled=policy.adobe_pdf_enabled,
        adobe_pdf_timeout_seconds=policy.adobe_pdf_timeout_seconds,
    )
    for attempt in range(1, policy.direct_max_attempts + 1):
        try:
            if github_issue:
                result = fetch_github_issue(
                    *github_issue, opener=open_request, resolver=resolver,
                    timeout_seconds=timeout_seconds, max_bytes=html_max_bytes,
                    extracted_max_bytes=extracted_max_bytes,
                )
            else:
                result = fetch_direct(direct_request)
        except HTTPError as exc:
            # API quota exhaustion does not imply the issue page is limited.
            # Stop API requests; recovery reads the original webpage instead.
            if github_issue and exc.code in {403, 429}:
                limited = exc.code == 429 or bool(exc.headers) and (
                    exc.headers.get("X-RateLimit-Remaining") == "0"
                    or bool(exc.headers.get("Retry-After"))
                )
                exc.close()
                if limited:
                    return _recover_direct_failure(
                        fallback_context,
                        fallback_reason="github_api_rate_limited",
                        direct_attempts=attempt,
                        cause=exc,
                        extractor="github_api",
                    )
                raise ArticleFetchError(
                    "GitHub issue API request was forbidden",
                    error_code="http_403",
                    method=direct_method, attempts=attempt,
                ) from exc
            if (
                exc.code == 404
                and exc.url == direct_request.full_url
                and policy.url_correction_enabled
                and urlsplit(url).path.endswith("\\")
            ):
                return _retry_without_trailing_backslash(
                    direct_request,
                    fetch_direct=fetch_direct,
                    resolver=resolver,
                    direct_attempts=attempt,
                )
            fallback_reason = _http_fallback_reason(exc)
            if exc.code == 429 and exc.headers and exc.headers.get("Retry-After"):
                fallback_reason = ""
            # Recognized verification responses retain their diagnostic reason;
            # ordinary rate limits must wait, never immediately switch egress.
            is_challenge = bool(fallback_reason) and not fallback_reason.startswith("http_")
            transient = not is_challenge and (
                exc.code == 408 or 500 <= exc.code <= 599 or exc.code == 429
            )
            if transient:
                delay = _http_retry_delay(exc)
                can_retry = delay is not None and attempt < policy.direct_max_attempts
                if can_retry:
                    remaining = remaining_seconds()
                    can_retry = remaining is None or delay < remaining
                if can_retry:
                    exc.close()
                    budgeted_sleep(delay, sleeper)
                    LOGGER.warning(
                        "component=article_fetch method=%s status=http_%d "
                        "attempt=%d/%d retry_in=%ss", direct_method, exc.code, attempt,
                        policy.direct_max_attempts, delay,
                    )
                    continue
                # A long wait skips this direct retry. Recoverable errors can
                # still use Reader; ordinary 429 has no fallback and stops.
            if fallback_reason:
                exc.close()
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason=fallback_reason,
                    direct_attempts=attempt,
                    cause=exc,
                )
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code=f"http_{exc.code}",
                method=direct_method,
                attempts=attempt,
            ) from exc
        except ArticleFetchError as exc:
            fallback_reason = _article_error_fallback_reason(exc)
            if fallback_reason:
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason=fallback_reason,
                    direct_attempts=attempt,
                    cause=exc,
                    extractor=exc.extractor,
                )
            raise ArticleFetchError(
                f"direct article retrieval failed: {exc}",
                error_code=exc.error_code,
                method=direct_method,
                extractor=exc.extractor,
                fallback_attempted=exc.fallback_attempted,
                fallback_reason=exc.fallback_reason,
                attempts=attempt,
            ) from exc
        except (URLError, OSError, HTTPException) as exc:
            if _is_tls_issuer_unavailable(exc):
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason="tls_issuer_unavailable",
                    direct_attempts=attempt,
                    cause=exc,
                )
            if _is_network_timeout(exc):
                if attempt < policy.direct_max_attempts:
                    LOGGER.warning(
                        "component=article_fetch method=%s status=network_timeout "
                        "attempt=%d/%d retry_in=%ss",
                        direct_method,
                        attempt,
                        policy.direct_max_attempts,
                        DIRECT_RETRY_DELAY_SECONDS,
                    )
                    budgeted_sleep(DIRECT_RETRY_DELAY_SECONDS, sleeper)
                    continue
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason="network_timeout",
                    direct_attempts=attempt,
                    cause=exc,
                    direct_failure=f"network timeout after {attempt} attempts: {exc}",
                )
            # Certificate errors other than a missing issuer keep their existing
            # terminal behavior. Other transport failures can use a new route.
            if not isinstance(getattr(exc, "reason", exc), ssl.SSLError):
                if attempt < policy.direct_max_attempts:
                    budgeted_sleep(DIRECT_RETRY_DELAY_SECONDS, sleeper)
                    continue
                return _recover_direct_failure(
                    fallback_context, fallback_reason="request_failed",
                    direct_attempts=attempt, cause=exc,
                )
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code="request_failed",
                method=direct_method,
                attempts=attempt,
            ) from exc
        except Exception as exc:
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code="request_failed",
                method=direct_method,
                attempts=attempt,
            ) from exc

        LOGGER.info(
            "component=article_fetch method=%s extractor=%s status=success "
            "attempts=%d",
            direct_method,
            result.extractor,
            attempt,
        )
        return ArticleFetchResult(
            text=result.text,
            method=result.method,
            fallback_reason=result.fallback_reason,
            extractor=result.extractor,
            attempts=attempt,
            retrieved_url=result.retrieved_url,
            source_evidence=result.source_evidence,
        )

    raise AssertionError("direct article retry loop ended unexpectedly")


def _http_retry_delay(exc: HTTPError, *, now: datetime | None = None) -> float | None:
    """Return an affordable direct retry delay, without shortening Retry-After.

    None skips the direct retry; the caller decides whether recovery is allowed.
    Invalid/missing values use the ordinary short backoff.
    """
    value = (exc.headers.get("Retry-After", "") if exc.headers else "").strip()
    if value.isascii() and value.isdigit():
        delay = float(value) if len(value) < 10 else float("inf")
    else:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            delay = max(0.0, (target - (now or datetime.now(UTC))).total_seconds())
        except (TypeError, ValueError, OverflowError):
            delay = DIRECT_RETRY_DELAY_SECONDS
    return delay if delay <= 15 else None


def _retry_without_trailing_backslash(
    request: Request,
    *,
    fetch_direct: Callable[[Request], ArticleFetchResult],
    resolver,
    direct_attempts: int,
) -> ArticleFetchResult:
    """Make one direct correction attempt; never start another recovery chain."""
    # Preserve the exact query/fragment (including empty delimiters). Only the
    # final literal path character is removed, without decoding percent escapes.
    url = request.full_url
    path_end = min((url.find(c) for c in "?#" if c in url), default=len(url))
    corrected_url = url[:path_end - 1] + url[path_end:]
    reason = "url_trailing_backslash"
    attempts = direct_attempts
    LOGGER.warning(
        "component=article_fetch method=direct status=http_404 "
        "correction=%s attempts=%d", reason, direct_attempts,
    )
    try:
        _validate_public_http_url(corrected_url, resolver)
        attempts += 1
        result = fetch_direct(Request(corrected_url, headers=dict(request.header_items())))
    except Exception as exc:
        if isinstance(exc, HTTPError):
            code = f"http_{exc.code}"
        elif isinstance(exc, ArticleFetchError):
            code = exc.error_code
        elif _is_network_timeout(exc):
            code = "network_timeout"
        else:
            code = "request_failed"
        raise ArticleFetchError(
            f"direct URL correction failed after original HTTP 404: {exc}",
            error_code=code,
            method="direct",
            extractor=getattr(exc, "extractor", ""),
            fallback_attempted=True,
            fallback_reason=reason,
            attempts=attempts,
        ) from exc
    LOGGER.info(
        "component=article_fetch method=direct extractor=%s status=success "
        "fallback_reason=%s attempts=%d", result.extractor, reason, attempts,
    )
    return replace(result, attempts=attempts, fallback_reason=reason)
