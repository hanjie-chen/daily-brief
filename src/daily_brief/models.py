from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Story:
    source: str
    hn_item_id: str
    title: str
    source_url: str
    hn_discussion_url: str
    created_at: str
    points: int
    comments: int
    story_text: str = ""
    fetched_text: str = ""


@dataclass(frozen=True)
class KeywordMatch:
    keyword: str
    weight: str
    source: str
    bonus: float
    start: int
    end: int


@dataclass
class Candidate:
    story: Story
    matched_keywords: list[KeywordMatch] = field(default_factory=list)
    score: float = 0.0
    selected: bool = False
    section: str = ""
    rejection_reason: str = ""
    summary: str = ""
    why: str = ""
