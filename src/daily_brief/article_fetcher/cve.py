"""Read published CVE records through the public CVE Services API."""

from __future__ import annotations

import json
import re
from functools import partial
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from .contracts import ArticleFetchError, ArticleFetchResult
from .http_safety import _enforce_extracted_limit, _read_bounded, _validate_public_http_url
from .source_evidence import SourceEvidence


def cve_record_id(url: str) -> str | None:
    """Recognize record pages, leaving other CVE website URLs on the HTML path."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"cve.org", "www.cve.org"}
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 443 if parsed.scheme == "https" else 80}
            or parsed.path not in {"/CVERecord", "/CVERecord/"}
        ):
            return None
        identifiers = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
    except ValueError:
        return None
    if len(identifiers) != 1 or not re.fullmatch(
        r"CVE-[0-9]{4}-[0-9]{4,}", identifiers[0], flags=re.IGNORECASE,
    ):
        return None
    return identifiers[0].upper()


def _invalid_response(message: str) -> ArticleFetchError:
    return ArticleFetchError(message, error_code="invalid_response", extractor="cve_api")


def _validate_api_url(url: str, *, expected: str) -> None:
    # No credentials are used, but every hop must still retain source identity.
    if url != expected:
        raise _invalid_response("CVE API redirected away from the requested record")


def fetch_cve_record(
    identifier: str,
    *,
    page_url: str,
    opener,
    resolver,
    timeout_seconds: int,
    max_bytes: int,
    extracted_max_bytes: int,
) -> ArticleFetchResult:
    """Fetch and validate one record; fetch.py owns retries and webpage recovery."""
    api_url = f"https://cveawg.mitre.org/api/cve/{identifier}"
    _validate_public_http_url(api_url, resolver)
    request = Request(api_url, headers={
        "User-Agent": "daily-brief/0.1", "Accept": "application/json",
    })
    request._redirect_validator = partial(_validate_api_url, expected=api_url)
    with opener(request, timeout=timeout_seconds) as response:
        final_url = response.geturl()
        _validate_public_http_url(final_url, resolver)
        _validate_api_url(final_url, expected=api_url)
        payload = _read_bounded(response, max_bytes)
        if response.headers.get_content_type().lower() != "application/json":
            raise _invalid_response("CVE API returned a non-JSON response")
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid_response("CVE API returned invalid JSON") from exc
    if not isinstance(record, dict) or record.get("dataType") != "CVE_RECORD":
        raise _invalid_response("CVE API returned an invalid record")
    metadata = record.get("cveMetadata")
    containers = record.get("containers")
    if (
        not isinstance(metadata, dict)
        or metadata.get("cveId") != identifier
        or metadata.get("state") != "PUBLISHED"
        or not isinstance(containers, dict)
        or not isinstance(containers.get("cna"), dict)
    ):
        raise _invalid_response("CVE API response does not identify the requested published record")

    cna = containers["cna"]
    descriptions = cna.get("descriptions", [])
    if not isinstance(descriptions, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("value"), str)
        for entry in descriptions
    ):
        raise _invalid_response("CVE API returned invalid descriptions")
    if not any(entry["value"].strip() for entry in descriptions):
        raise ArticleFetchError(
            "CVE record contained no description",
            error_code="empty_content", extractor="cve_api",
        )

    title = cna.get("title", "")
    if not isinstance(title, str):
        raise _invalid_response("CVE API returned an invalid title")
    title = " ".join(title.split())[:512]
    # Keep nested version ranges, metric vectors, remedies, references and ADP
    # additions intact. Flattening these into prose can change their meaning.
    body = "Official CVE record (CNA and supplementary ADP data):\n" + json.dumps(
        {"cveMetadata": metadata, "containers": containers}, ensure_ascii=False, indent=2,
    )
    text = (
        "Page metadata (publisher-provided context, not article body):\n"
        f"title: {title}\n\nExtracted body:\n{body}"
        if title else body
    )
    _enforce_extracted_limit(text, extracted_max_bytes, extractor="cve_api")
    published_at = metadata.get("datePublished", "")
    if not isinstance(published_at, str) or len(published_at) > 64:
        published_at = ""
    return ArticleFetchResult(
        text=text, method="cve_api", extractor="cve_api", retrieved_url=page_url,
        source_evidence=SourceEvidence(title=title, published_at=published_at),
    )
