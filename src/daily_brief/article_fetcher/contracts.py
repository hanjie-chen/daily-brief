from __future__ import annotations

import logging
from dataclasses import dataclass


DEFAULT_TIMEOUT_SECONDS = 15
CLASSIFICATION_HTTP_TIMEOUT_SECONDS = 8
DIRECT_MAX_ATTEMPTS = 2
DIRECT_RETRY_DELAY_SECONDS = 1
DEFAULT_MAX_EXTRACTED_BYTES = 256 * 1024
DEFAULT_MAX_HTML_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_PDF_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PDF_PAGES = 100
DEFAULT_PDF_PARSE_TIMEOUT_SECONDS = 60
CLASSIFICATION_PDF_PARSE_TIMEOUT_SECONDS = 10
DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS = 300
# Classification examines candidates serially, so keep remote PDF conversion
# bounded well below the fuller summary-retrieval allowance.
CLASSIFICATION_ADOBE_PDF_TIMEOUT_SECONDS = 30
DEFAULT_PDF_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024

# Kept as a compatibility name for direct helper callers and tests.
DEFAULT_MAX_BYTES = DEFAULT_MAX_EXTRACTED_BYTES

# Keep the established logger name even though the implementation now spans a package.
LOGGER = logging.getLogger("daily_brief.article_fetcher")


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
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.method = method
        self.extractor = extractor
        self.fallback_attempted = fallback_attempted
        self.fallback_reason = fallback_reason
        self.attempts = attempts


@dataclass(frozen=True)
class ArticleFetchPolicy:
    direct_max_attempts: int = DIRECT_MAX_ATTEMPTS
    jina_enabled: bool = True
    wayback_enabled: bool = True
    youtube_enabled: bool = True
    adobe_pdf_enabled: bool = True
    adobe_pdf_timeout_seconds: int = DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS


SUMMARY_FETCH_POLICY = ArticleFetchPolicy()
CLASSIFICATION_FETCH_POLICY = ArticleFetchPolicy(
    direct_max_attempts=1,
    jina_enabled=False,
    wayback_enabled=False,
    youtube_enabled=False,
    adobe_pdf_enabled=True,
    adobe_pdf_timeout_seconds=CLASSIFICATION_ADOBE_PDF_TIMEOUT_SECONDS,
)


@dataclass(frozen=True)
class ArticleFetchResult:
    text: str
    method: str
    fallback_reason: str = ""
    extractor: str = ""
    attempts: int = 1
    retrieved_url: str = ""
    material_origin: str = "original"
