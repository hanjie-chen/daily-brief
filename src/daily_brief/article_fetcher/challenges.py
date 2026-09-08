from __future__ import annotations

import ssl
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse


CHALLENGE_TEXT_MARKER_GROUPS = (
    ("verifying your browser", "complete the check below"),
    ("complete the check below to continue", "complete the verification above"),
    ("checking your browser", "enable javascript and cookies to continue"),
    ("vercel security checkpoint", "verifying your browser"),
)
CHALLENGE_HTML_MARKER_GROUPS = (
    ("challenges.cloudflare.com", "cf-turnstile"),
    ("challenges.cloudflare.com/turnstile", "complete the check below"),
)


def _is_cloudflare_challenge(error: HTTPError) -> bool:
    return bool(
        error.headers
        and error.headers.get("cf-mitigated", "").strip().lower() == "challenge"
    )


def _is_vercel_challenge(error: HTTPError) -> bool:
    return bool(
        error.code == 429
        and error.headers
        and error.headers.get("x-vercel-mitigated", "").strip().lower()
        == "challenge"
    )


def _is_datadome_challenge(error: HTTPError) -> bool:
    return bool(
        error.code in {401, 403}
        and error.headers
        and error.headers.get("x-datadome", "").strip().lower() == "protected"
    )


def _challenge_from_headers(headers) -> str:
    if not headers:
        return ""
    if headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return "challenge_page"
    if headers.get("x-vercel-mitigated", "").strip().lower() == "challenge":
        return "vercel_challenge"
    return ""


def _is_challenge_page(
    url: str,
    *,
    raw_html: str = "",
    text: str = "",
) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").lower()
    query = parse_qs(parsed.query)
    if path == "/challenge" and "redirect" in query:
        return True
    if path.startswith("/cdn-cgi/challenge-platform"):
        return True

    normalized_text = " ".join(text.lower().split())
    if any(
        all(marker in normalized_text for marker in markers)
        for markers in CHALLENGE_TEXT_MARKER_GROUPS
    ):
        return True

    normalized_html = raw_html.lower()
    return any(
        all(marker in normalized_html for marker in markers)
        for markers in CHALLENGE_HTML_MARKER_GROUPS
    )


def _is_tls_issuer_unavailable(error: BaseException) -> bool:
    reason = getattr(error, "reason", None)
    return (
        isinstance(reason, ssl.SSLCertVerificationError)
        and reason.verify_code == 20
    )


def _is_network_timeout(error: BaseException) -> bool:
    reason = getattr(error, "reason", error)
    return isinstance(reason, TimeoutError)
