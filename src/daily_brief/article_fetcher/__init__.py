"""Bounded public article retrieval with a stable package-level API."""

from .contracts import (
    DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS,
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_EXTRACTED_BYTES,
    DEFAULT_MAX_HTML_BYTES,
    DEFAULT_MAX_PDF_BYTES,
    DEFAULT_MAX_PDF_PAGES,
    DEFAULT_PDF_ADDRESS_SPACE_BYTES,
    DEFAULT_PDF_PARSE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DIRECT_MAX_ATTEMPTS,
    DIRECT_RETRY_DELAY_SECONDS,
    DEFAULT_FETCH_POLICY,
    ArticleFetchError,
    ArticleFetchPolicy,
    ArticleFetchResult,
)
from .extract import extract_html
from .fetch import fetch_article, fetch_article_text
from .github import fetch_github_blob, fetch_github_readme_text
from .jina import fetch_jina_reader_text
from .source_evidence import SourceEvidence, SourceRelation


__all__ = [
    "DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_EXTRACTED_BYTES",
    "DEFAULT_MAX_HTML_BYTES",
    "DEFAULT_MAX_PDF_BYTES",
    "DEFAULT_MAX_PDF_PAGES",
    "DEFAULT_PDF_ADDRESS_SPACE_BYTES",
    "DEFAULT_PDF_PARSE_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DIRECT_MAX_ATTEMPTS",
    "DIRECT_RETRY_DELAY_SECONDS",
    "DEFAULT_FETCH_POLICY",
    "ArticleFetchError",
    "ArticleFetchPolicy",
    "ArticleFetchResult",
    "SourceEvidence",
    "SourceRelation",
    "extract_html",
    "fetch_article",
    "fetch_article_text",
    "fetch_github_blob",
    "fetch_github_readme_text",
    "fetch_jina_reader_text",
]
