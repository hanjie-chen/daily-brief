from __future__ import annotations

from dataclasses import dataclass

from ..article_fetcher import ArticleFetchError, ArticleFetchResult


@dataclass(frozen=True)
class FetchedMaterial:
    text: str
    method: str
    extractor: str
    attempts: int
    fallback_reason: str
    retrieved_url: str
    material_origin: str


def coerce_fetched_material(fetch_result, retrieved_url: str) -> FetchedMaterial:
    if isinstance(fetch_result, ArticleFetchResult):
        text = fetch_result.text.strip()
        method = fetch_result.method
        extractor = fetch_result.extractor
        fallback_reason = fetch_result.fallback_reason
        attempts = fetch_result.attempts
        effective_url = fetch_result.retrieved_url or retrieved_url
        material_origin = fetch_result.material_origin
    elif isinstance(fetch_result, str):
        text = fetch_result.strip()
        method = "direct"
        extractor = "plain_text"
        fallback_reason = ""
        attempts = 1
        effective_url = retrieved_url
        material_origin = "original"
    else:
        raise ArticleFetchError(
            "article fetcher returned an invalid result",
            error_code="invalid_fetch_result",
            method="direct",
        )
    if not text:
        raise ArticleFetchError(
            "article response contained no visible text",
            error_code="empty_content",
            method=method,
            extractor=extractor,
            fallback_attempted=bool(fallback_reason),
            fallback_reason=fallback_reason,
            attempts=attempts,
        )
    return FetchedMaterial(
        text=text,
        method=method,
        extractor=extractor,
        attempts=attempts,
        fallback_reason=fallback_reason,
        retrieved_url=effective_url,
        material_origin=material_origin,
    )
