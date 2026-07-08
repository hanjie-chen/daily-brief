from __future__ import annotations

import re
from urllib.parse import urlparse

from .config import (
    ABBREVIATIONS,
    HIGH_WEIGHT_KEYWORDS,
    LOW_WEIGHT_KEYWORDS,
    MEDIUM_HIGH_WEIGHT_KEYWORDS,
    MEDIUM_WEIGHT_KEYWORDS,
    WEAK_KEYWORDS,
)
from .models import KeywordMatch

WEIGHT_BONUS = {
    "high": 4.0,
    "medium_high": 2.5,
    "medium": 1.5,
    "low": 0.5,
    "weak": 0.0,
}


def match_keywords(title: str, story_text: str, url: str) -> list[KeywordMatch]:
    primary_text = "\n".join(part for part in [title, story_text] if part)
    matches = _match_primary_text(primary_text)
    matches.extend(_match_url_tokens(url))
    return matches


def _match_primary_text(text: str) -> list[KeywordMatch]:
    matches: list[KeywordMatch] = []
    for weight, keywords in [
        ("high", HIGH_WEIGHT_KEYWORDS),
        ("medium_high", MEDIUM_HIGH_WEIGHT_KEYWORDS),
        ("medium", MEDIUM_WEIGHT_KEYWORDS),
        ("low", LOW_WEIGHT_KEYWORDS),
        ("weak", WEAK_KEYWORDS),
    ]:
        for keyword in keywords:
            for match in _iter_keyword_matches(text, keyword):
                span = match.span()
                matches.append(
                    KeywordMatch(
                        keyword=keyword,
                        weight=weight,
                        source="text",
                        bonus=WEIGHT_BONUS[weight],
                        start=span[0],
                        end=span[1],
                    )
                )

    filtered = [
        match
        for match in matches
        if not _covered_by_longer_match(match, matches)
    ]
    return sorted(filtered, key=lambda match: match.start)


def _iter_keyword_matches(text: str, keyword: str):
    if keyword in ABBREVIATIONS:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
        return re.finditer(pattern, text)

    flags = re.IGNORECASE
    pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword).replace(r'\ ', r'\s+')}(?![A-Za-z0-9])"
    return re.finditer(pattern, text, flags)


def _match_url_tokens(url: str) -> list[KeywordMatch]:
    if not url:
        return []

    parsed = urlparse(url)
    token_text = " ".join(
        part
        for part in [
            parsed.hostname or "",
            parsed.path.replace("/", " ").replace("-", " "),
        ]
        if part
    )
    matches: list[KeywordMatch] = []
    for keyword in ["AI", *WEAK_KEYWORDS]:
        for match in _iter_keyword_matches(token_text, keyword):
            matches.append(
                KeywordMatch(
                    keyword=keyword,
                    weight="weak",
                    source="url",
                    bonus=0.0,
                    start=match.start(),
                    end=match.end(),
                )
            )
            return matches
    return matches


def _covered_by_longer_match(match: KeywordMatch, matches: list[KeywordMatch]) -> bool:
    return any(
        other is not match
        and other.start <= match.start
        and other.end >= match.end
        and len(other.keyword) > len(match.keyword)
        for other in matches
    )
