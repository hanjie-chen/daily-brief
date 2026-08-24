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
class RetrievalFailure:
    method: str = ""
    extractor: str = ""
    attempts: int = 0
    fallback_attempted: bool = False
    fallback_reason: str = ""
    error_type: str = ""
    error_code: str = ""
    error_message: str = ""


@dataclass
class SyndicatedRecovery:
    status: str = "not_attempted"
    provider: str = ""
    discovered_candidates: int = 0
    attempted_candidates: int = 0
    rejection_reasons: list[str] = field(default_factory=list)
    error_code: str = ""


@dataclass
class ArticleRetrieval:
    status: str = "not_attempted"
    method: str = ""
    extractor: str = ""
    attempts: int = 0
    fallback_attempted: bool = False
    fallback_reason: str = ""
    error_type: str = ""
    error_code: str = ""
    error_message: str = ""
    retrieved_url: str = ""
    material_origin: str = ""
    origin_failure: RetrievalFailure | None = None
    syndicated_recovery: SyndicatedRecovery = field(default_factory=SyndicatedRecovery)

    @property
    def origin_blocked(self) -> bool:
        return self.fallback_reason in {
            "challenge_page",
            "cloudflare_challenge",
            "datadome_challenge",
            "vercel_challenge",
        }


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
    topic_route: str = "not_evaluated"
    summary_mode: str = "not_routed"
    summary_context_strategy: str = "not_prepared"
    summary_context_source_chars: int = 0
    summary_context_selected_chars: int = 0
    summary_context_sections: list[str] = field(default_factory=list)
    article_retrieval: ArticleRetrieval = field(default_factory=ArticleRetrieval)
    summary_basis: str = "not_generated"
    summary_status: str = "not_generated"
