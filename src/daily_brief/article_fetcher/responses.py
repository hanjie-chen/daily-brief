from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlparse
from urllib.request import Request

from ..adobe_pdf_extractor import (
    DEFAULT_CONNECT_TIMEOUT_MS as ADOBE_CONNECT_TIMEOUT_MS,
    DEFAULT_READ_TIMEOUT_MS as ADOBE_READ_TIMEOUT_MS,
    credentials_status as adobe_credentials_status,
)
from .challenges import _challenge_from_headers, _is_challenge_page
from .contracts import ArticleFetchError, ArticleFetchResult, LOGGER
from .extract import extract_html
from .http_safety import (
    _enforce_extracted_limit,
    _read_bounded,
    _validate_public_http_url,
)


DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS = 300
GITHUB_RAW_CONTENT_TYPE = "application/vnd.github.raw+json"
PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/octet-stream",
    "binary/octet-stream",
}


def _reject_encoded_wayback_response(headers) -> None:
    content_encoding = headers.get("Content-Encoding", "").strip().lower()
    if content_encoding not in {"", "identity"}:
        raise ArticleFetchError(
            "Wayback returned an unsupported content encoding",
            error_code="wayback_unsupported_content_encoding",
            method="wayback",
        )


def _fetch_direct_response(
    request: Request,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    html_max_bytes: int,
    pdf_max_bytes: int,
    extracted_max_bytes: int,
    pdf_max_pages: int,
    pdf_parse_timeout_seconds: int,
    pdf_address_space_bytes: int,
    adobe_pdf_enabled: bool = True,
    require_identity_encoding: bool = False,
) -> ArticleFetchResult:
    expects_pdf = urlparse(request.full_url).path.lower().endswith(".pdf")
    with opener(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
        _validate_public_http_url(final_url, resolver)
        if require_identity_encoding:
            _reject_encoded_wayback_response(response.headers)
        header_challenge = _challenge_from_headers(response.headers)
        if header_challenge:
            raise ArticleFetchError(
                "article response was a browser verification challenge page",
                error_code=header_challenge,
            )
        content_type = response.headers.get_content_type().lower()
        if content_type == "text/html":
            raw_limit = html_max_bytes
        elif content_type == "application/pdf":
            raw_limit = pdf_max_bytes
        elif content_type in PDF_CONTENT_TYPES and expects_pdf:
            raw_limit = pdf_max_bytes
        elif content_type == "text/plain":
            raw_limit = extracted_max_bytes
        else:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}",
                error_code="unsupported_content_type",
            )
        payload = _read_bounded(response, raw_limit)
        charset = response.headers.get_content_charset() or "utf-8"

    if content_type == "text/html":
        markup = payload.decode(charset, errors="replace")
        if _is_challenge_page(final_url, raw_html=markup):
            raise ArticleFetchError(
                "article response was a browser verification challenge page",
                error_code="challenge_page",
                extractor="trafilatura",
            )

    result = _extract_response_payload(
        payload,
        content_type=content_type,
        charset=charset,
        method="direct",
        extracted_max_bytes=extracted_max_bytes,
        pdf_max_pages=pdf_max_pages,
        pdf_parse_timeout_seconds=pdf_parse_timeout_seconds,
        pdf_address_space_bytes=pdf_address_space_bytes,
        adobe_pdf_enabled=adobe_pdf_enabled,
        expects_pdf=expects_pdf,
        allow_octet_stream_pdf=True,
    )
    if _is_challenge_page(final_url, text=result.text):
        raise ArticleFetchError(
            "extracted article text was a browser verification challenge page",
            error_code="challenge_page",
            extractor=result.extractor,
        )
    return ArticleFetchResult(
        text=result.text,
        method=result.method,
        fallback_reason=result.fallback_reason,
        extractor=result.extractor,
        attempts=result.attempts,
        retrieved_url=final_url,
    )


def _extract_response_payload(
    payload: bytes,
    *,
    content_type: str,
    charset: str,
    method: str,
    extracted_max_bytes: int,
    pdf_max_pages: int,
    pdf_parse_timeout_seconds: int,
    pdf_address_space_bytes: int,
    expects_pdf: bool = False,
    allow_octet_stream_pdf: bool = False,
    adobe_pdf_enabled: bool = True,
) -> ArticleFetchResult:
    has_pdf_magic = payload.startswith(b"%PDF-")
    is_pdf_type = content_type == "application/pdf" or (
        allow_octet_stream_pdf and content_type in PDF_CONTENT_TYPES and expects_pdf
    )

    if expects_pdf and content_type not in PDF_CONTENT_TYPES:
        raise ArticleFetchError(
            f"PDF content type does not match: {content_type}",
            error_code="pdf_content_type_mismatch",
            extractor="pypdf",
        )
    if is_pdf_type:
        if not has_pdf_magic:
            raise ArticleFetchError(
                "PDF response is missing the %PDF- file signature",
                error_code="pdf_magic_mismatch",
                extractor="pypdf",
            )
        text, extractor, fallback_reason = _extract_pdf_payload(
            payload,
            max_pages=pdf_max_pages,
            max_text_bytes=extracted_max_bytes,
            pypdf_timeout_seconds=pdf_parse_timeout_seconds,
            address_space_bytes=pdf_address_space_bytes,
            adobe_pdf_enabled=adobe_pdf_enabled,
        )
        return ArticleFetchResult(
            text=text,
            method=method,
            extractor=extractor,
            fallback_reason=fallback_reason,
        )

    if has_pdf_magic:
        raise ArticleFetchError(
            f"PDF file returned non-PDF content type: {content_type}",
            error_code="pdf_content_type_mismatch",
            extractor="pypdf",
        )

    if content_type == "text/html":
        try:
            text = extract_html(payload.decode(charset, errors="replace"))
        except Exception as exc:
            raise ArticleFetchError(
                f"HTML extraction failed: {exc}",
                error_code="html_extraction_failed",
                extractor="trafilatura",
            ) from exc
        extractor = "trafilatura"
    elif content_type.startswith("text/") or content_type in {
        "application/json",
        GITHUB_RAW_CONTENT_TYPE,
    }:
        text = _normalize_document_text(
            payload.decode(charset, errors="replace")
        )
        extractor = "plain_text"
    else:
        raise ArticleFetchError(
            f"unsupported article content type: {content_type}",
            error_code="unsupported_content_type",
        )

    if not text:
        raise ArticleFetchError(
            "article response contained no extractable text",
            error_code="empty_content",
            extractor=extractor,
        )
    _enforce_extracted_limit(text, extracted_max_bytes, extractor=extractor)
    return ArticleFetchResult(text=text, method=method, extractor=extractor)


def _extract_pdf_payload(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    pypdf_timeout_seconds: int,
    address_space_bytes: int,
    adobe_pdf_enabled: bool = True,
) -> tuple[str, str, str]:
    credential_state = (
        adobe_credentials_status(os.environ) if adobe_pdf_enabled else "disabled"
    )
    fallback_reason = ""
    if credential_state == "configured":
        try:
            text = _extract_pdf_with_adobe_in_subprocess(
                payload,
                max_pages=max_pages,
                max_text_bytes=max_text_bytes,
                timeout_seconds=DEFAULT_ADOBE_PDF_TIMEOUT_SECONDS,
                address_space_bytes=address_space_bytes,
            )
        except ArticleFetchError as exc:
            fallback_reason = exc.error_code
            LOGGER.warning(
                "component=pdf_extract extractor=adobe_pdf_to_markdown "
                "status=failed code=%s fallback=pypdf",
                exc.error_code,
            )
        else:
            LOGGER.info(
                "component=pdf_extract extractor=adobe_pdf_to_markdown "
                "status=success"
            )
            return text, "adobe_pdf_to_markdown", ""
    elif credential_state == "incomplete":
        fallback_reason = "adobe_pdf_credentials_incomplete"
        LOGGER.warning(
            "component=pdf_extract extractor=adobe_pdf_to_markdown "
            "status=disabled code=%s fallback=pypdf",
            fallback_reason,
        )

    try:
        text = _extract_pdf_in_subprocess(
            payload,
            max_pages=max_pages,
            max_text_bytes=max_text_bytes,
            timeout_seconds=pypdf_timeout_seconds,
            address_space_bytes=address_space_bytes,
        )
    except ArticleFetchError as exc:
        if not fallback_reason:
            raise
        raise ArticleFetchError(
            str(exc),
            error_code=exc.error_code,
            extractor=exc.extractor,
            fallback_attempted=True,
            fallback_reason=fallback_reason,
        ) from exc
    return text, "pypdf", fallback_reason


def _extract_pdf_with_adobe_in_subprocess(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    timeout_seconds: int,
    address_space_bytes: int,
) -> str:
    command = [
        sys.executable,
        "-m",
        "daily_brief.adobe_pdf_extractor",
        str(max_pages),
        str(max_text_bytes),
        str(ADOBE_CONNECT_TIMEOUT_MS),
        str(ADOBE_READ_TIMEOUT_MS),
        str(address_space_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArticleFetchError(
            "Adobe PDF to Markdown timed out",
            error_code="adobe_pdf_timeout",
            extractor="adobe_pdf_to_markdown",
        ) from exc

    if completed.returncode != 0:
        raise ArticleFetchError(
            "Adobe PDF worker subprocess failed",
            error_code="adobe_pdf_worker_failed",
            extractor="adobe_pdf_to_markdown",
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArticleFetchError(
            "Adobe PDF worker subprocess returned an invalid result",
            error_code="adobe_pdf_worker_failed",
            extractor="adobe_pdf_to_markdown",
        ) from exc

    if result.get("status") != "success":
        raise ArticleFetchError(
            str(result.get("message") or "Adobe PDF extraction failed"),
            error_code=str(
                result.get("error_code") or "adobe_pdf_conversion_failed"
            ),
            extractor="adobe_pdf_to_markdown",
        )
    text = str(result.get("text") or "").strip()
    if not text:
        raise ArticleFetchError(
            "Adobe PDF to Markdown returned empty content",
            error_code="adobe_pdf_empty_content",
            extractor="adobe_pdf_to_markdown",
        )
    _enforce_extracted_limit(
        text,
        max_text_bytes,
        extractor="adobe_pdf_to_markdown",
    )
    return text


def _extract_pdf_in_subprocess(
    payload: bytes,
    *,
    max_pages: int,
    max_text_bytes: int,
    timeout_seconds: int,
    address_space_bytes: int,
) -> str:
    command = [
        sys.executable,
        "-m",
        "daily_brief.pdf_extractor",
        str(max_pages),
        str(max_text_bytes),
        str(address_space_bytes),
    ]
    try:
        completed = subprocess.run(
            command,
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ArticleFetchError(
            "PDF parsing timed out",
            error_code="pdf_parse_timeout",
            extractor="pypdf",
        ) from exc

    if completed.returncode != 0:
        diagnostic = _normalize_single_line(
            completed.stderr.decode("utf-8", errors="replace")
        )[:500]
        raise ArticleFetchError(
            f"PDF parser subprocess failed: {diagnostic or 'no diagnostic'}",
            error_code="pdf_parse_failed",
            extractor="pypdf",
        )
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArticleFetchError(
            "PDF parser subprocess returned an invalid result",
            error_code="pdf_parse_failed",
            extractor="pypdf",
        ) from exc

    if result.get("status") != "success":
        raise ArticleFetchError(
            str(result.get("message") or "PDF extraction failed"),
            error_code=str(result.get("error_code") or "pdf_parse_failed"),
            extractor="pypdf",
        )
    text = str(result.get("text") or "").strip()
    if not text:
        raise ArticleFetchError(
            "PDF contains no extractable text",
            error_code="pdf_no_extractable_text",
            extractor="pypdf",
        )
    _enforce_extracted_limit(text, max_text_bytes, extractor="pypdf")
    return text


def _fetch_bounded_text_response(
    request: Request,
    *,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    accepted_content_types: set[str],
) -> str:
    with opener(request, timeout=timeout_seconds) as response:
        _validate_public_http_url(response.geturl(), resolver)
        content_type = response.headers.get_content_type().lower()
        if content_type not in accepted_content_types:
            raise ArticleFetchError(
                f"unsupported article content type: {content_type}",
                error_code="unsupported_content_type",
            )
        payload = _read_bounded(response, max_bytes)
        charset = response.headers.get_content_charset() or "utf-8"

    text = _normalize_document_text(payload.decode(charset, errors="replace"))
    if not text:
        raise ArticleFetchError(
            "article response contained no extractable text",
            error_code="empty_content",
        )
    _enforce_extracted_limit(text, max_bytes, extractor="plain_text")
    return text



def _normalize_single_line(value: str) -> str:
    return " ".join(value.split())


def _normalize_document_text(value: str) -> str:
    """Normalize line endings without damaging Markdown or preformatted text."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()
