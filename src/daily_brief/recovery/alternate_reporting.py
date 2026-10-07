from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from urllib.parse import unquote, urlsplit

from ..article_fetcher.source_evidence import SourceEvidence
from ..models import Candidate
from .same_article import normalize_candidate_url
from .tavily import TavilyFinder

MAX_ALTERNATE_REPORTING_CANDIDATES = 5
MIN_ALTERNATE_REPORTING_BODY_CHARS = 400
# These are discussion/social endpoints, not replacement news articles. Public
# address validation is still enforced by the article client on every request.
EXCLUDED_REPORTING_HOSTS = {
    "news.ycombinator.com", "reddit.com", "x.com", "twitter.com", "facebook.com",
    "instagram.com", "linkedin.com", "youtube.com", "youtu.be", "github.com",
}

_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_NUMBER_SIGNAL = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s+(?:million|billion|trillion|percent)\b|"
    r"(?<!\w)\d+(?:\.\d+)?%(?!\w))",
    re.IGNORECASE,
)
_NYTIMES_DATE = re.compile(r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)")
_REUTERS_DATE = re.compile(r"-(20\d{2})-(\d{2})-(\d{2})(?:/|$)")
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
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:,?\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})(?:\b|(?=T\d{2}:))")


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


class TavilyAlternateReportingFinder(TavilyFinder):
    def find(self, candidate: Candidate) -> list[AlternateReportingCandidate]:
        self._require_api_key(AlternateReportingFinderError)
        results = self._search(
            AlternateReportingFinderError,
            query=build_tavily_query(candidate),
            max_results=MAX_ALTERNATE_REPORTING_CANDIDATES,
            exclude_domains=sorted(EXCLUDED_REPORTING_HOSTS),
            exact_match=False,
        )
        return [AlternateReportingCandidate(title=title, url=url) for title, url in results]


def normalize_allowed_candidate_url(url: str) -> str | None:
    normalized = normalize_candidate_url(url)
    if normalized is None:
        return None
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    if parsed.port not in {None, 80, 443} or any(
        hostname == host or hostname.endswith("." + host)
        for host in EXCLUDED_REPORTING_HOSTS
    ):
        return None
    return normalized


def validate_alternate_reporting(
    source: Candidate,
    alternate: AlternateReportingCandidate,
    body: str,
    *,
    source_evidence: SourceEvidence | None = None,
) -> AlternateReportingValidation:
    del alternate  # Search titles/snippets only discover URLs, never verify them.
    text = body.split("\n\nExtracted body:\n", 1)[-1].strip()
    if len(text) < MIN_ALTERNATE_REPORTING_BODY_CHARS:
        return AlternateReportingValidation(False, "body_too_short")
    normalized_text = " ".join(text.lower().split())
    if any(signal in normalized_text for signal in _TEASER_SIGNALS):
        return AlternateReportingValidation(False, "teaser_content")
    # Require prose, rather than a headline, keyword list, or search snippet.
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    if sum(len(sentence.strip()) >= 60 for sentence in sentences) < 2:
        return AlternateReportingValidation(False, "insufficient_substantive_material")

    source_date = _source_date(source)
    if source_date is None:
        return AlternateReportingValidation(False, "source_date_missing")
    published_at = getattr(source_evidence, "published_at", "")
    # Explicit publication metadata takes precedence: a recent date mentioned
    # in an old article must not make it look like a fresh report.
    reporting_date = _nearby_reporting_date(
        published_at if published_at else text[:1600], source_date
    )
    if reporting_date is None:
        return AlternateReportingValidation(False, "date_mismatch")

    source_anchors = _event_anchors(source)
    identity_words = {
        _canonical_token(word)
        for word in _WORD.findall(text[:4000])
    }
    matched = tuple(anchor for anchor in source_anchors if anchor in identity_words)
    required = min(5, max(3, (len(source_anchors) + 1) // 2))
    if len(source_anchors) < 3 or len(matched) < required:
        return AlternateReportingValidation(False, "insufficient_event_signals")
    entities = _source_entities(source)
    if any(entity not in identity_words for entity in entities):
        return AlternateReportingValidation(False, "entity_mismatch")
    identity_numbers = set(_number_signals(text[:4000]))
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
    # Preserve the event in the HN title; numeric publisher article IDs from
    # slugs and a hard-coded news agency make the search less useful.
    title = " ".join(candidate.story.title.split())
    if title:
        return title[:400]
    anchors = _slug_search_terms(candidate.story.source_url)
    if not anchors:
        raise AlternateReportingFinderError(
            "source URL and title do not contain searchable event anchors",
            error_code="invalid_source_metadata",
        )
    return " ".join(anchors[:8])[:400]


def _source_entities(candidate: Candidate) -> tuple[str, ...]:
    # Headline/slug agreement identifies names without treating sentence-initial
    # verbs as entities. Multiword names also survive when absent from the slug.
    title = candidate.story.title
    slug_words = set(_slug_anchors(candidate.story.source_url))
    words = re.findall(r"[a-zA-Z]+", title)
    # Capitalization cannot identify names in a Title Case headline. In that
    # case rely on the event-word checks instead of requiring every headline
    # word as if it were part of a person's name.
    significant = [word for word in words if word.lower() not in _STOP_WORDS and len(word) >= 3]
    if len(significant) >= 5 and sum(word[0].isupper() for word in significant) / len(significant) > 0.7:
        return ()
    names = []
    for match in re.finditer(r"\b[A-Z][a-zA-Z]+(?:[ -]+[A-Z][a-zA-Z]+)*\b", title):
        words = _WORD.findall(match.group())
        for word in words:
            token = _canonical_token(word)
            if (token not in _STOP_WORDS and len(token) >= 3
                    and (token in slug_words or len(words) > 1)):
                names.append(token)
    return tuple(dict.fromkeys(names))


def _event_anchors(candidate: Candidate) -> list[str]:
    anchors = _title_anchors(candidate.story.title)
    for anchor in _slug_anchors(candidate.story.source_url):
        if anchor not in anchors:
            anchors.append(anchor)
    return anchors


def _title_anchors(title: str) -> list[str]:
    return _distinctive_anchors(_WORD.findall(title))



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
            and not lowered.isdigit()
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
            and not lowered.isdigit()
            and len(lowered) >= 4
            and lowered not in terms
        ):
            terms.append(lowered)
    return terms


def _canonical_token(word: str) -> str:
    lowered = word.lower()
    aliases = {
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
