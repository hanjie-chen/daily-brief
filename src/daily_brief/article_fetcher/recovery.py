from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError

from .challenges import (
    _is_cloudflare_challenge,
    _is_datadome_challenge,
    _is_vercel_challenge,
)
from .contracts import (
    ArticleFetchError,
    ArticleFetchPolicy,
    ArticleFetchResult,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_PDF_ADDRESS_SPACE_BYTES,
    DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    LOGGER,
)
from .jina import _fetch_jina_reader
from .wayback import _fetch_wayback_capture, _find_wayback_capture


@dataclass(frozen=True)
class _DirectFailureRule:
    direct_failure: str
    policy_failure: str = ""
    wayback_after_jina: bool = False
    log_extractor: str = ""
    log_attempts: bool = False


_DIRECT_FAILURE_RULES = {
    "vercel_challenge": _DirectFailureRule(
        direct_failure="vercel challenge",
        wayback_after_jina=True,
    ),
    "datadome_challenge": _DirectFailureRule(
        direct_failure="datadome challenge",
        policy_failure="DataDome challenge",
        wayback_after_jina=True,
    ),
    "cloudflare_challenge": _DirectFailureRule(
        direct_failure="cloudflare challenge",
        policy_failure="Cloudflare challenge",
        wayback_after_jina=True,
    ),
    "challenge_page": _DirectFailureRule(
        direct_failure="browser verification challenge page",
        wayback_after_jina=True,
    ),
    "empty_content": _DirectFailureRule(
        direct_failure="trafilatura empty_content",
        log_extractor="trafilatura",
    ),
    "tls_issuer_unavailable": _DirectFailureRule(
        direct_failure="TLS issuer unavailable",
    ),
    "network_timeout": _DirectFailureRule(
        direct_failure="network timeout",
        log_attempts=True,
    ),
}


@dataclass(frozen=True)
class _JinaFallbackContext:
    url: str
    opener: object
    resolver: object
    timeout_seconds: int
    extracted_max_bytes: int
    html_max_bytes: int
    pdf_max_bytes: int
    pdf_max_pages: int
    pdf_parse_timeout_seconds: int
    pdf_address_space_bytes: int
    wayback_not_before: datetime | None
    wayback_not_after: datetime | None
    policy: ArticleFetchPolicy


def _http_fallback_reason(exc: HTTPError) -> str:
    if _is_vercel_challenge(exc):
        return "vercel_challenge"
    if _is_datadome_challenge(exc):
        return "datadome_challenge"
    if _is_cloudflare_challenge(exc):
        return "cloudflare_challenge"
    return ""


def _article_error_fallback_reason(exc: ArticleFetchError) -> str:
    if exc.error_code in {"vercel_challenge", "challenge_page"}:
        return exc.error_code
    if exc.error_code == "empty_content" and exc.extractor == "trafilatura":
        return "empty_content"
    return ""


def _recover_direct_failure(
    context: _JinaFallbackContext,
    *,
    fallback_reason: str,
    direct_attempts: int,
    cause: BaseException,
    extractor: str = "",
    direct_failure: str = "",
) -> ArticleFetchResult:
    rule = _DIRECT_FAILURE_RULES[fallback_reason]
    policy_failure = direct_failure or rule.policy_failure or rule.direct_failure
    direct_failure = direct_failure or rule.direct_failure
    if not context.policy.jina_enabled:
        raise _direct_policy_failure(
            policy_failure,
            fallback_reason,
            direct_attempts,
            extractor=extractor,
        ) from cause

    if rule.log_attempts:
        LOGGER.warning(
            "component=article_fetch method=direct status=%s "
            "attempt=%d/%d fallback=jina",
            fallback_reason,
            direct_attempts,
            context.policy.direct_max_attempts,
        )
    elif rule.log_extractor:
        LOGGER.warning(
            "component=article_fetch method=direct extractor=%s "
            "status=%s fallback=jina",
            rule.log_extractor,
            fallback_reason,
        )
    else:
        LOGGER.warning(
            "component=article_fetch method=direct status=%s fallback=jina",
            fallback_reason,
        )

    return _fetch_jina_fallback(
        context.url,
        direct_failure=direct_failure,
        fallback_reason=fallback_reason,
        opener=context.opener,
        resolver=context.resolver,
        timeout_seconds=context.timeout_seconds,
        max_bytes=context.extracted_max_bytes,
        direct_attempts=direct_attempts,
        wayback_enabled=(rule.wayback_after_jina and context.policy.wayback_enabled),
        wayback_not_before=context.wayback_not_before,
        wayback_not_after=context.wayback_not_after,
        html_max_bytes=context.html_max_bytes,
        pdf_max_bytes=context.pdf_max_bytes,
        pdf_max_pages=context.pdf_max_pages,
        pdf_parse_timeout_seconds=context.pdf_parse_timeout_seconds,
        pdf_address_space_bytes=context.pdf_address_space_bytes,
    )


def _direct_policy_failure(
    message: str,
    error_code: str,
    attempts: int,
    *,
    extractor: str = "",
) -> ArticleFetchError:
    return ArticleFetchError(
        f"direct article retrieval failed: {message}",
        error_code=error_code,
        method="direct",
        extractor=extractor,
        attempts=attempts,
    )


def _fetch_jina_fallback(
    url: str,
    *,
    direct_failure: str,
    fallback_reason: str,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    direct_attempts: int = 1,
    wayback_enabled: bool = False,
    wayback_not_before: datetime | None = None,
    wayback_not_after: datetime | None = None,
    html_max_bytes: int = DEFAULT_MAX_HTML_BYTES,
    pdf_max_bytes: int = DEFAULT_MAX_PDF_BYTES,
    pdf_max_pages: int = DEFAULT_MAX_PDF_PAGES,
    pdf_parse_timeout_seconds: int = DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    pdf_address_space_bytes: int = DEFAULT_PDF_ADDRESS_SPACE_BYTES,
) -> ArticleFetchResult:
    try:
        reader_result = _fetch_jina_reader(
            url,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
        )
    except ArticleFetchError as jina_exc:
        if wayback_enabled:
            LOGGER.warning(
                "component=article_fetch method=jina extractor=jina status=failed "
                "code=%s fallback=wayback",
                jina_exc.error_code,
            )
            return _fetch_wayback_fallback(
                url,
                direct_failure=direct_failure,
                jina_failure=jina_exc,
                fallback_reason=fallback_reason,
                opener=opener,
                resolver=resolver,
                timeout_seconds=timeout_seconds,
                html_max_bytes=html_max_bytes,
                pdf_max_bytes=pdf_max_bytes,
                extracted_max_bytes=max_bytes,
                pdf_max_pages=pdf_max_pages,
                pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
                pdf_address_space_bytes=pdf_address_space_bytes,
                prior_attempts=direct_attempts + 1,
                not_before=wayback_not_before,
                not_after=wayback_not_after,
            )
        raise ArticleFetchError(
            f"article retrieval failed: direct={direct_failure}; jina={jina_exc}",
            error_code=jina_exc.error_code,
            method="jina",
            extractor="jina",
            fallback_attempted=True,
            fallback_reason=fallback_reason,
            attempts=direct_attempts + 1,
        ) from jina_exc
    LOGGER.info(
        "component=article_fetch method=jina extractor=jina status=success "
        "fallback_reason=%s attempts=%d",
        fallback_reason,
        direct_attempts + 1,
    )
    return ArticleFetchResult(
        text=reader_result.text,
        method="jina",
        extractor="jina",
        fallback_reason=fallback_reason,
        attempts=direct_attempts + 1,
        retrieved_url=reader_result.origin_url,
    )


def _fetch_wayback_fallback(
    url: str,
    *,
    direct_failure: str,
    jina_failure: ArticleFetchError,
    fallback_reason: str,
    opener,
    resolver,
    timeout_seconds: int,
    html_max_bytes: int,
    pdf_max_bytes: int,
    extracted_max_bytes: int,
    pdf_max_pages: int,
    pdf_parse_timeout_seconds: int,
    pdf_address_space_bytes: int,
    prior_attempts: int,
    not_before: datetime | None,
    not_after: datetime | None,
) -> ArticleFetchResult:
    try:
        capture = _find_wayback_capture(
            url,
            opener=opener,
            resolver=resolver,
            timeout_seconds=timeout_seconds,
            not_before=not_before,
            not_after=not_after,
        )
    except ArticleFetchError as wayback_exc:
        raise ArticleFetchError(
            "article retrieval failed: "
            f"direct={direct_failure}; jina={jina_failure}; "
            f"wayback={wayback_exc}",
            error_code=wayback_exc.error_code,
            method="wayback",
            extractor=wayback_exc.extractor,
            fallback_attempted=True,
            fallback_reason=fallback_reason,
            attempts=prior_attempts + 1,
        ) from wayback_exc

    try:
        archived = _fetch_wayback_capture(
            capture,
            source_url=url,
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
    except ArticleFetchError as wayback_exc:
        raise ArticleFetchError(
            "article retrieval failed: "
            f"direct={direct_failure}; jina={jina_failure}; "
            f"wayback={wayback_exc}",
            error_code=wayback_exc.error_code,
            method="wayback",
            extractor=wayback_exc.extractor,
            fallback_attempted=True,
            fallback_reason=fallback_reason,
            attempts=prior_attempts + 2,
        ) from wayback_exc

    attempts = prior_attempts + 2
    LOGGER.info(
        "component=article_fetch method=wayback extractor=%s status=success "
        "fallback_reason=%s capture_timestamp=%s attempts=%d",
        archived.extractor,
        fallback_reason,
        capture.timestamp,
        attempts,
    )
    return ArticleFetchResult(
        text=archived.text,
        method="wayback",
        fallback_reason=fallback_reason,
        extractor=archived.extractor,
        attempts=attempts,
        retrieved_url=archived.retrieved_url,
        material_origin="archived_copy",
    )
