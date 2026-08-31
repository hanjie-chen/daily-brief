from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from http.client import HTTPResponse
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .models import Candidate

TAVILY_SEARCH_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = 10
TAVILY_MAX_RESPONSE_BYTES = 256 * 1024
MAX_ALTERNATE_REPORTING_CANDIDATES = 5
MAX_RESULT_TITLE_CHARS = 500
MAX_RESULT_URL_CHARS = 2048
MIN_ALTERNATE_REPORTING_BODY_CHARS = 400
REUTERS_HOSTS = {"reuters.com", "www.reuters.com"}
YAHOO_HOSTS = {"finance.yahoo.com", "ca.finance.yahoo.com"}
ALTERNATE_REPORTING_HOST_ALLOWLIST = REUTERS_HOSTS | YAHOO_HOSTS

_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_NUMBER_SIGNAL = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s+(?:million|billion|trillion|percent))?\b",
    re.IGNORECASE,
)
_NYTIMES_DATE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)")
_REUTERS_DATE = re.compile(r"-(20\d{2})-(\d{2})-(\d{2})(?:/|$)")
_REUTERS_MARKER = re.compile(
    r"(?:\(\s*reuters\s*\)|\bby\s+[^\n]{0,80}\breuters\b|\breuters\s*[-\u2013\u2014])",
    re.IGNORECASE,
)
_REPORTING_FOOTER = re.compile(
    r"(?:\(|^|\n)\s*reporting\s+by\s+[^\n()]{2,300}"
    r"(?:\n|;\s*)?(?:editing\s+by\s+[^\n()]{2,200})?\s*\)?\s*$",
    re.IGNORECASE,
)
_TEASER_SIGNALS = (
    "read the full article",
    "get unlimited access",
    "subscribe to continue",
    "sign in to continue reading",
    "purchase a subscription",
)
_STOP_WORDS = {
    "about",
    "after",
    "against",
    "amid",
    "article",
    "before",
    "from",
    "html",
    "into",
    "news",
    "over",
    "report",
    "reports",
    "reuters",
    "says",
    "that",
    "their",
    "this",
    "under",
    "with",
    "without",
    "rules",
    "story",
    "technology",
}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_TEXTUAL_DATE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True)
class AlternateReportingCandidate:
    title: str
    url: str


@dataclass(frozen=True)
class AlternateReportingValidation:
    accepted: bool
    reason: str
    reporting_date: date | None = None
    matched_anchors: tuple[str, ...] = ()


class AlternateReportingFinderError(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class AlternateReportingFinder(Protocol):
    provider: str

    def find(self, candidate: Candidate) -> list[AlternateReportingCandidate]: ...


class TavilyAlternateReportingFinder:
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
        cls, env: Mapping[str, str] | None = None, **kwargs
    ) -> TavilyAlternateReportingFinder:
        environment = os.environ if env is None else env
        return cls(api_key=environment.get("TAVILY_API_KEY", ""), **kwargs)

    def find(self, candidate: Candidate) -> list[AlternateReportingCandidate]:
        if not self.api_key:
            raise AlternateReportingFinderError(
                "TAVILY_API_KEY is not configured",
                error_code="not_configured",
            )
        body = json.dumps(
            {
                "query": build_tavily_query(candidate),
                "search_depth": "basic",
                "topic": "general",
                "max_results": MAX_ALTERNATE_REPORTING_CANDIDATES,
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
                "include_domains": sorted(ALTERNATE_REPORTING_HOST_ALLOWLIST),
                "auto_parameters": False,
                "exact_match": False,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
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
            raise AlternateReportingFinderError(
                f"Tavily Search returned HTTP {exc.code}",
                error_code="provider_http_error",
            ) from exc
        except (TimeoutError, URLError) as exc:
            raise AlternateReportingFinderError(
                "Tavily Search request failed",
                error_code="provider_request_failed",
            ) from exc

        if len(response_body) > TAVILY_MAX_RESPONSE_BYTES:
            raise AlternateReportingFinderError(
                "Tavily Search response exceeded the size limit",
                error_code="response_too_large",
            )
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlternateReportingFinderError(
                "Tavily Search returned invalid JSON",
                error_code="malformed_response",
            ) from exc
        if not isinstance(payload, dict) or not isinstance(
            payload.get("results"), list
        ):
            raise AlternateReportingFinderError(
                "Tavily Search returned an invalid result envelope",
                error_code="malformed_response",
            )

        candidates = []
        for item in payload["results"][:MAX_ALTERNATE_REPORTING_CANDIDATES]:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            url = item.get("url")
            if not isinstance(title, str) or not isinstance(url, str):
                continue
            title = title.strip()
            url = url.strip()
            if (
                not title
                or not url
                or len(title) > MAX_RESULT_TITLE_CHARS
                or len(url) > MAX_RESULT_URL_CHARS
            ):
                continue
            candidates.append(AlternateReportingCandidate(title=title, url=url))
        return candidates


def normalize_allowed_candidate_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname not in ALTERNATE_REPORTING_HOST_ALLOWLIST
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or (port is not None and port not in {80, 443})
    ):
        return None
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def is_yahoo_url(url: str) -> bool:
    normalized = normalize_allowed_candidate_url(url)
    return bool(normalized and (urlsplit(normalized).hostname or "") in YAHOO_HOSTS)


def validate_alternate_reporting(
    source: Candidate,
    alternate: AlternateReportingCandidate,
    body: str,
) -> AlternateReportingValidation:
    text = body.strip()
    if len(text) < MIN_ALTERNATE_REPORTING_BODY_CHARS:
        return AlternateReportingValidation(False, "body_too_short")
    normalized_text = " ".join(text.lower().split())
    if any(signal in normalized_text for signal in _TEASER_SIGNALS):
        return AlternateReportingValidation(False, "teaser_content")
    if _REUTERS_MARKER.search(text[:700]) is None:
        return AlternateReportingValidation(False, "missing_reuters_marker")
    if _REPORTING_FOOTER.search(text[-600:]) is None:
        return AlternateReportingValidation(False, "missing_reporting_footer")

    source_date = _source_date(source)
    if source_date is None:
        return AlternateReportingValidation(False, "source_date_missing")
    reporting_date = _nearby_reporting_date(text[:1600], source_date)
    if reporting_date is None:
        return AlternateReportingValidation(False, "date_mismatch")

    source_anchors = _event_anchors(source)
    identity_words = {
        _canonical_token(word)
        for word in _WORD.findall(f"{alternate.title}\n{text}")
    }
    matched = tuple(anchor for anchor in source_anchors if anchor in identity_words)
    required = min(5, max(3, (len(source_anchors) + 1) // 2))
    if len(source_anchors) < 3 or len(matched) < required:
        return AlternateReportingValidation(False, "insufficient_event_signals")
    identity_numbers = set(_number_signals(f"{alternate.title}\n{text}"))
    if any(
        signal not in identity_numbers for signal in _source_number_signals(source)
    ):
        return AlternateReportingValidation(False, "insufficient_event_signals")

    return AlternateReportingValidation(
        True,
        "verified",
        reporting_date=reporting_date,
        matched_anchors=matched,
    )


def validations_conflict(
    validations: list[AlternateReportingValidation],
) -> bool:
    accepted = [validation for validation in validations if validation.accepted]
    for index, left in enumerate(accepted):
        for right in accepted[index + 1 :]:
            if (
                left.reporting_date is not None
                and right.reporting_date is not None
                and abs((left.reporting_date - right.reporting_date).days) > 2
            ):
                return True
            if len(set(left.matched_anchors) & set(right.matched_anchors)) < 2:
                return True
    return False


def build_tavily_query(candidate: Candidate) -> str:
    anchors = _slug_search_terms(candidate.story.source_url)
    if len(anchors) < 3:
        for anchor in _title_search_terms(candidate.story.title):
            if anchor not in anchors:
                anchors.append(anchor)
            if len(anchors) >= 3:
                break
    if not anchors:
        raise AlternateReportingFinderError(
            "source URL and title do not contain searchable event anchors",
            error_code="invalid_source_metadata",
        )
    return (" ".join(anchors[:3]) + " Reuters")[:400]


def _event_anchors(candidate: Candidate) -> list[str]:
    anchors = _title_anchors(candidate.story.title)
    for anchor in _slug_anchors(candidate.story.source_url):
        if anchor not in anchors:
            anchors.append(anchor)
    return anchors


def _title_anchors(title: str) -> list[str]:
    return _distinctive_anchors(_WORD.findall(title))


def _title_search_terms(title: str) -> list[str]:
    return _distinctive_search_terms(_WORD.findall(title))


def _slug_anchors(url: str) -> list[str]:
    anchors = []
    for term in _slug_search_terms(url):
        canonical = _canonical_token(term)
        if canonical and canonical not in anchors:
            anchors.append(canonical)
    return anchors


def _slug_search_terms(url: str) -> list[str]:
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return []
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"\.(?:html?|aspx?)$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
    return _distinctive_search_terms(_WORD.findall(slug))


def _distinctive_anchors(words: list[str]) -> list[str]:
    anchors = []
    for word in words:
        lowered = word.lower()
        canonical = _canonical_token(lowered)
        if (
            lowered not in _STOP_WORDS
            and len(canonical) >= 4
            and canonical not in anchors
        ):
            anchors.append(canonical)
    return anchors


def _distinctive_search_terms(words: list[str]) -> list[str]:
    terms = []
    for word in words:
        lowered = word.lower()
        if (
            lowered not in _STOP_WORDS
            and len(lowered) >= 4
            and lowered not in terms
        ):
            terms.append(lowered)
    return terms


def _canonical_token(word: str) -> str:
    lowered = word.lower()
    aliases = {
        "administration": "government",
        "pentagon": "government",
        "unlawful": "illegal",
        "illegality": "illegal",
        "blacklisted": "blacklist",
        "blacklisting": "blacklist",
        "ruling": "rule",
        "ruled": "rule",
        "rules": "rule",
    }
    return aliases.get(lowered, lowered)


def _source_date(candidate: Candidate) -> date | None:
    try:
        path = urlsplit(candidate.story.source_url).path
    except ValueError:
        path = ""
    for pattern in (_NYTIMES_DATE, _REUTERS_DATE):
        match = pattern.search(path)
        if match is not None:
            try:
                return date(*(int(value) for value in match.groups()))
            except ValueError:
                pass
    try:
        created_at = datetime.fromisoformat(
            candidate.story.created_at.replace("Z", "+00:00")
        )
    except (AttributeError, ValueError):
        return None
    return created_at.date()


def _source_number_signals(candidate: Candidate) -> tuple[str, ...]:
    try:
        slug = unquote(urlsplit(candidate.story.source_url).path).rstrip("/").rsplit(
            "/", 1
        )[-1]
    except ValueError:
        slug = ""
    slug = re.sub(r"\.(?:html?|aspx?)$", "", slug, flags=re.IGNORECASE)
    slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
    return tuple(_number_signals(f"{candidate.story.title}\n{slug.replace('-', ' ')}"))


def _number_signals(text: str) -> list[str]:
    signals = []
    for match in _NUMBER_SIGNAL.finditer(text.lower()):
        signal = " ".join(match.group(0).split())
        if re.fullmatch(r"20\d{2}", signal) or signal in signals:
            continue
        signals.append(signal)
    return signals


def _nearby_reporting_date(text: str, source_date: date) -> date | None:
    observed: list[date] = []
    for match in _ISO_DATE.finditer(text):
        try:
            observed.append(date(*(int(value) for value in match.groups())))
        except ValueError:
            continue
    for match in _TEXTUAL_DATE.finditer(text):
        month_text, day_text, year_text = match.groups()
        year = int(year_text) if year_text else source_date.year
        try:
            observed.append(date(year, _MONTHS[month_text.lower()], int(day_text)))
        except ValueError:
            continue
    nearby = [
        candidate_date
        for candidate_date in observed
        if abs((candidate_date - source_date).days) <= 2
    ]
    if not nearby:
        return None
    return min(
        nearby,
        key=lambda value: (abs((value - source_date).days), value),
    )
