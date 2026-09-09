from __future__ import annotations

import socket
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..youtube_captions import (
    YoutubeCaptionError,
    fetch_youtube_caption,
    youtube_video_id,
)
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
    _github_repository,
    fetch_github_blob,
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
            adobe_pdf_enabled=policy.adobe_pdf_enabled,
            adobe_pdf_timeout_seconds=policy.adobe_pdf_timeout_seconds,
        )

    direct_request = Request(
        url,
        headers={
            "User-Agent": "daily-brief/0.1",
            "Accept": "text/html,text/plain,application/pdf;q=0.9",
        },
    )
    open_request = opener or _build_safe_opener(resolver).open
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
    )

    for attempt in range(1, policy.direct_max_attempts + 1):
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
                adobe_pdf_enabled=policy.adobe_pdf_enabled,
                adobe_pdf_timeout_seconds=policy.adobe_pdf_timeout_seconds,
            )
        except HTTPError as exc:
            fallback_reason = _http_fallback_reason(exc)
            if fallback_reason:
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason=fallback_reason,
                    direct_attempts=attempt,
                    cause=exc,
                )
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code=f"http_{exc.code}",
                method="direct",
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
                method="direct",
                extractor=exc.extractor,
                fallback_attempted=exc.fallback_attempted,
                fallback_reason=exc.fallback_reason,
                attempts=attempt,
            ) from exc
        except (URLError, TimeoutError) as exc:
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
                        "component=article_fetch method=direct status=network_timeout "
                        "attempt=%d/%d retry_in=%ss",
                        attempt,
                        policy.direct_max_attempts,
                        DIRECT_RETRY_DELAY_SECONDS,
                    )
                    sleeper(DIRECT_RETRY_DELAY_SECONDS)
                    continue
                return _recover_direct_failure(
                    fallback_context,
                    fallback_reason="network_timeout",
                    direct_attempts=attempt,
                    cause=exc,
                    direct_failure=f"network timeout after {attempt} attempts: {exc}",
                )
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code="request_failed",
                method="direct",
                attempts=attempt,
            ) from exc
        except Exception as exc:
            raise ArticleFetchError(
                f"direct article request failed: {exc}",
                error_code="request_failed",
                method="direct",
                attempts=attempt,
            ) from exc

        LOGGER.info(
            "component=article_fetch method=direct extractor=%s status=success "
            "attempts=%d",
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
        )

    raise AssertionError("direct article retry loop ended unexpectedly")
