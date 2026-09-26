"""Bounded discovery and conservative validation of cross-posted articles.

Search is only used to discover URLs.  It is deliberately not evidence that a
result is the same work: the fetched page must make that relationship explicit.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from ..article_fetcher import ArticleFetchResult
from ..models import Candidate
from .tavily import MAX_RESULT_TITLE_CHARS, MAX_RESULT_URL_CHARS, TavilyFinder

MAX_SAME_ARTICLE_CANDIDATES = 10
MAX_SAME_ARTICLE_FETCHES = 3
MIN_SAME_ARTICLE_BODY_CHARS = 800
HN_DOMAIN = "news.ycombinator.com"

_TITLE_WORD = re.compile(r"[\w]+", re.UNICODE)
_SITE_TITLE_SUFFIX = re.compile(r"\s+(?:[|–—-])\s+[^|–—-]{1,80}$")
_TEASER_SIGNALS = (
    "read the full article",
    "read more",
    "continue reading",
    "subscribe to continue",
    "sign in to continue reading",
    "get unlimited access",
    "log in to continue",
)
_SUMMARY_SIGNALS = (
    "summary of the article",
    "article summary",
    "this post summarizes",
    "here is a summary",
    "tl;dr",
)
_LESSWRONG_POST = re.compile(r"^/posts/([A-Za-z0-9]+)(?:/|$)")


@dataclass(frozen=True)
class SameArticleCandidate:
    title: str
    url: str


@dataclass(frozen=True)
class SameArticleValidation:
    accepted: bool
    reason: str
    evidence: tuple[str, ...] = ()


class SameArticleFinderError(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SameArticleFinder(Protocol):
    provider: str

    def find(self, candidate: Candidate) -> list[SameArticleCandidate]: ...


class TavilySameArticleFinder(TavilyFinder):
    def find(self, candidate: Candidate) -> list[SameArticleCandidate]:
        self._require_api_key(SameArticleFinderError)
        title = candidate.story.title.strip()
        if not title or len(title) > MAX_RESULT_TITLE_CHARS:
            raise SameArticleFinderError(
                "Source title is missing or too long", error_code="invalid_source_metadata"
            )
        found: list[SameArticleCandidate] = []
        seen: set[str] = set()
        for result_title, url in self._search(
            SameArticleFinderError,
            query=title,
            max_results=MAX_SAME_ARTICLE_CANDIDATES,
            exclude_domains=[HN_DOMAIN],
            exact_match=False,
        ):
            normalized_url = normalize_candidate_url(url)
            if normalized_url is None or normalized_url in seen:
                continue
            seen.add(normalized_url)
            found.append(SameArticleCandidate(title=result_title, url=normalized_url))
        return found


def normalize_candidate_url(url: str) -> str | None:
    """Normalize a candidate URL without doing DNS; fetcher safety does DNS."""
    if not isinstance(url, str) or len(url) > MAX_RESULT_URL_CHARS:
        return None
    if re.search(r"[\x00-\x20\\]", url):
        return None
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        return None
    if port is not None and port < 1:
        return None
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def same_source_url(left: str, right: str) -> bool:
    """Compare exact normalized URLs, with the narrowly known LessWrong ID rule."""
    normalized_left, normalized_right = normalize_candidate_url(left), normalize_candidate_url(right)
    if not normalized_left or not normalized_right:
        return False
    if normalized_left == normalized_right:
        return True
    parsed_left, parsed_right = urlsplit(normalized_left), urlsplit(normalized_right)
    left_host, right_host = parsed_left.hostname, parsed_right.hostname
    if {left_host, right_host} - {"lesswrong.com", "www.lesswrong.com"}:
        return False
    if parsed_left.port is not None or parsed_right.port is not None:
        return False
    if parsed_left.query != parsed_right.query:
        return False
    left_match = _LESSWRONG_POST.match(parsed_left.path)
    right_match = _LESSWRONG_POST.match(parsed_right.path)
    return bool(left_match and right_match and left_match.group(1) == right_match.group(1))


def validate_same_article(
    source: Candidate,
    alternative: SameArticleCandidate,
    fetched: ArticleFetchResult,
) -> SameArticleValidation:
    """Accept only an explicit, substantial fetched cross-post or narration."""
    del alternative  # Search-result metadata is untrusted discovery material.
    evidence_obj = getattr(fetched, "source_evidence", None)
    page_title = getattr(evidence_obj, "title", "") if evidence_obj else ""
    if not isinstance(page_title, str) or not _titles_match(source.story.title, page_title):
        return SameArticleValidation(False, "page_title_mismatch")

    relation_evidence: list[str] = []
    is_youtube = fetched.method == "youtube_caption" or fetched.extractor == "youtube_caption"
    for relation in getattr(evidence_obj, "relations", ()) if evidence_obj else ():
        kind = getattr(relation, "kind", "")
        relation_url = getattr(relation, "url", "")
        context = getattr(relation, "context", "")
        if not isinstance(kind, str) or not isinstance(relation_url, str):
            continue
        if not same_source_url(source.story.source_url, relation_url):
            continue
        if kind in {"crosspost", "republication"} and not is_youtube:
            relation_evidence.append(f"{kind}:{relation_url}")
        elif is_youtube and kind == "narration":
            relation_evidence.append(f"narration:{relation_url}")
        else:
            continue
        # A canonical link identifies a URL but does not establish a cross-post.
        if relation_evidence and isinstance(context, str) and context.strip():
            relation_evidence[-1] += f":{context[:300]}"
    if not relation_evidence:
        return SameArticleValidation(False, "missing_explicit_source_relation")
    text = _body_without_metadata(fetched.text)
    if len(text) < MIN_SAME_ARTICLE_BODY_CHARS:
        return SameArticleValidation(False, "body_too_short")
    folded_text = " ".join(text.casefold().split())
    if any(signal in folded_text for signal in _TEASER_SIGNALS):
        return SameArticleValidation(False, "teaser_content")
    if any(signal in folded_text for signal in _SUMMARY_SIGNALS):
        return SameArticleValidation(False, "summary_or_aggregator")
    if not _has_substantive_paragraphs(text, is_youtube=is_youtube):
        return SameArticleValidation(False, "insufficient_substantive_material")
    return SameArticleValidation(True, "verified", tuple(relation_evidence[:4]))


def _body_without_metadata(text: str) -> str:
    marker = "\n\nExtracted body:\n"
    return text.split(marker, 1)[-1].strip()


def _title_words(title: str) -> tuple[str, ...]:
    title = " ".join(title.split())
    return tuple(word.casefold() for word in _TITLE_WORD.findall(title))


def _titles_match(story_title: str, page_title: str) -> bool:
    expected = _title_words(story_title)
    return bool(expected and expected in (
        _title_words(page_title), _title_words(_SITE_TITLE_SUFFIX.sub("", page_title))
    ))


def _has_substantive_paragraphs(text: str, *, is_youtube: bool = False) -> bool:
    parts = re.split(r"[\n]+", text)
    paragraphs = [" ".join(part.split()) for part in parts]
    substantive = [part for part in paragraphs if len(part) >= 120]
    if len(substantive) >= 3:
        return True
    if is_youtube:
        sentences = re.split(r"(?<=[.!?。！？])\s+", text)
        return sum(len(sentence.strip()) >= 40 for sentence in sentences) >= 5
    return False
