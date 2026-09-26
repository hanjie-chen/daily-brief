from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol
from urllib.parse import unquote, urlsplit, urlunsplit

from ..models import Candidate
from .tavily import TavilyFinder

MAX_SYNDICATED_CANDIDATES = 3
MIN_SYNDICATED_BODY_CHARS = 800
REUTERS_HOSTS = {"reuters.com", "www.reuters.com"}
SYNDICATED_HOST_ALLOWLIST = {
    "ca.finance.yahoo.com",
    "finance.yahoo.com",
}

_DATE_IN_URL = re.compile(r"(20\d{2})-(\d{2})-(\d{2})(?:/|$)")
_REUTERS_MARKER = re.compile(
    r"(?:\(\s*reuters\s*\)|\bby\s+[^\n]{0,80}\breuters\b|\breuters\s*[-–—])",
    re.IGNORECASE,
)
_NUMBER_PHRASE = re.compile(
    r"\b\d+(?:\.\d+)?\s+(?:million|billion|trillion)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_TITLE_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
_TEASER_SIGNALS = (
    "read the full article",
    "get unlimited access",
    "subscribe to continue",
    "sign in to continue reading",
)
_SLUG_STOP_WORDS = {
    "about",
    "after",
    "amid",
    "article",
    "back",
    "before",
    "business",
    "from",
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
}
_ENTITY_STOP_WORDS = {
    "About",
    "After",
    "Amid",
    "Before",
    "From",
    "Into",
    "Over",
    "Reuters",
    "That",
    "This",
    "Under",
    "With",
}


@dataclass(frozen=True)
class SyndicatedCandidate:
    title: str
    url: str


@dataclass(frozen=True)
class SyndicatedValidation:
    accepted: bool
    reason: str


class SyndicatedFinderError(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SyndicatedCopyFinder(Protocol):
    provider: str

    def find(self, candidate: Candidate) -> list[SyndicatedCandidate]: ...


class TavilySyndicatedCopyFinder(TavilyFinder):
    def find(self, candidate: Candidate) -> list[SyndicatedCandidate]:
        self._require_api_key(SyndicatedFinderError)
        results = self._search(
            SyndicatedFinderError,
            query=build_tavily_query(candidate),
            max_results=MAX_SYNDICATED_CANDIDATES,
            include_domains=sorted(SYNDICATED_HOST_ALLOWLIST),
            exact_match=True,
        )
        return [SyndicatedCandidate(title=title, url=url) for title, url in results]


def is_reuters_url(url: str) -> bool:
    try:
        hostname = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return hostname in REUTERS_HOSTS


def normalize_allowed_candidate_url(url: str) -> str | None:
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or hostname not in SYNDICATED_HOST_ALLOWLIST
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
    ):
        return None
    if port is not None and port not in {80, 443}:
        return None
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def validate_syndicated_copy(
    source: Candidate,
    syndicated: SyndicatedCandidate,
    body: str,
) -> SyndicatedValidation:
    text = body.strip()
    if len(text) < MIN_SYNDICATED_BODY_CHARS:
        return SyndicatedValidation(False, "body_too_short")

    normalized_text = " ".join(text.lower().split())
    if any(signal in normalized_text for signal in _TEASER_SIGNALS):
        return SyndicatedValidation(False, "teaser_content")

    early_text = text[:700]
    if _REUTERS_MARKER.search(early_text) is None:
        return SyndicatedValidation(False, "missing_reuters_marker")

    source_date = _source_date(source.story.source_url)
    if source_date is None:
        return SyndicatedValidation(False, "source_date_missing")
    if not _contains_nearby_date(text[:1200], source_date):
        return SyndicatedValidation(False, "date_mismatch")

    identity_text = f"{syndicated.title}\n{text}".lower()
    identity_words = set(_WORD.findall(identity_text))
    slug_words = _slug_words(source.story.source_url)
    anchors = _distinctive_slug_anchors(slug_words)
    matched_anchors = sum(anchor in identity_words for anchor in anchors)
    required_anchors = min(5, max(3, (len(anchors) + 1) // 2))
    if len(anchors) < 3 or matched_anchors < required_anchors:
        return SyndicatedValidation(False, "insufficient_story_signals")

    number_phrases = set(_NUMBER_PHRASE.findall(" ".join(slug_words)))
    if any(phrase.lower() not in identity_text for phrase in number_phrases):
        return SyndicatedValidation(False, "insufficient_story_signals")

    entities = _title_entities(source.story.title, set(slug_words))
    required_entities = min(2, len(entities))
    if (
        required_entities
        and sum(entity in identity_words for entity in entities) < required_entities
    ):
        return SyndicatedValidation(False, "insufficient_story_signals")

    return SyndicatedValidation(True, "verified")


def build_tavily_query(candidate: Candidate) -> str:
    slug_words = _slug_words(candidate.story.source_url)
    if not slug_words:
        raise SyndicatedFinderError(
            "Reuters URL does not contain a searchable slug",
            error_code="invalid_source_url",
        )
    initial_phrase = " ".join(slug_words[:3])
    parts = [f'"{initial_phrase}"']
    number_phrases = []
    for index, word in enumerate(slug_words[:-1]):
        if re.fullmatch(r"\d+(?:\.\d+)?", word) and slug_words[index + 1] in {
            "million",
            "billion",
            "trillion",
        }:
            number_phrases.append(f"{word} {slug_words[index + 1]}")
    parts.extend(f'"{phrase}"' for phrase in number_phrases[:2])
    initial_words = set(slug_words[:3])
    for entity in _title_entities(candidate.story.title, set(slug_words)):
        if entity not in initial_words:
            parts.append(_source_title_spelling(candidate.story.title, entity))
        if len(parts) >= 4:
            break
    parts.append("Reuters")
    return " ".join(parts)[:400]


def _slug_words(url: str) -> list[str]:
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return []
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    slug = re.sub(r"-20\d{2}-\d{2}-\d{2}$", "", slug)
    return [word.lower() for word in _WORD.findall(slug)]


def _distinctive_slug_anchors(words: list[str]) -> list[str]:
    anchors = []
    for word in words:
        if (
            word not in _SLUG_STOP_WORDS
            and (len(word) >= 4 or word.isdigit())
            and word not in anchors
        ):
            anchors.append(word)
    return anchors


def _title_entities(title: str, slug_words: set[str]) -> list[str]:
    entities = []
    for word in _TITLE_WORD.findall(title):
        lowered = word.lower()
        if (
            lowered in slug_words
            and word not in _ENTITY_STOP_WORDS
            and (
                word[0].isupper() or any(character.isupper() for character in word[1:])
            )
            and lowered not in entities
        ):
            entities.append(lowered)
    return entities


def _source_title_spelling(title: str, lowered_word: str) -> str:
    for word in _TITLE_WORD.findall(title):
        if word.lower() == lowered_word:
            return word
    return lowered_word


def _source_date(url: str) -> date | None:
    match = _DATE_IN_URL.search(urlsplit(url).path)
    if match is None:
        return None
    try:
        return date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def _contains_nearby_date(text: str, source_date: date) -> bool:
    normalized = " ".join(text.lower().replace(",", " ").split())
    short_months = (
        "jan",
        "feb",
        "mar",
        "apr",
        "may",
        "jun",
        "jul",
        "aug",
        "sep",
        "oct",
        "nov",
        "dec",
    )
    long_months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    for offset in range(-2, 3):
        candidate_date = source_date + timedelta(days=offset)
        month_short = short_months[candidate_date.month - 1]
        month_long = long_months[candidate_date.month - 1]
        forms = {
            candidate_date.isoformat(),
            f"{month_short} {candidate_date.day}",
            f"{month_long} {candidate_date.day}",
            f"{candidate_date.day} {month_short}",
            f"{candidate_date.day} {month_long}",
            f"{candidate_date.month:02d}/{candidate_date.day:02d}/{candidate_date.year}",
        }
        if any(form in normalized for form in forms):
            return True
    return False
