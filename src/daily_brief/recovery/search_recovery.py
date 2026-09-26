from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from ..article_fetcher import ArticleFetchResult
from ..models import (
    AlternateReportingRecovery,
    Candidate,
    SameArticleAttempt,
    SameArticleRecovery,
    SyndicatedRecovery,
)
from .alternate_reporting import (
    MAX_ALTERNATE_REPORTING_CANDIDATES,
    AlternateReportingCandidate,
    AlternateReportingFinder,
    AlternateReportingFinderError,
    AlternateReportingValidation,
    TavilyAlternateReportingFinder,
    is_yahoo_url,
    normalize_allowed_candidate_url as normalize_alternate_reporting_url,
    validate_alternate_reporting,
    validations_conflict,
)
from .fetched_material import FetchedMaterial, coerce_fetched_material
from .same_article import (
    MAX_SAME_ARTICLE_CANDIDATES,
    MAX_SAME_ARTICLE_FETCHES,
    SameArticleCandidate,
    SameArticleFinder,
    SameArticleFinderError,
    TavilySameArticleFinder,
    normalize_candidate_url as normalize_same_article_url,
    same_source_url,
    validate_same_article,
)
from .syndicated_copy import (
    MAX_SYNDICATED_CANDIDATES,
    SyndicatedCandidate,
    SyndicatedCopyFinder,
    SyndicatedFinderError,
    TavilySyndicatedCopyFinder,
    normalize_allowed_candidate_url,
    validate_syndicated_copy,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyndicatedOutcome:
    material: FetchedMaterial | None
    audit: SyndicatedRecovery


@dataclass(frozen=True)
class AlternateReportingOutcome:
    material: FetchedMaterial | None
    audit: AlternateReportingRecovery


@dataclass(frozen=True)
class SameArticleOutcome:
    material: FetchedMaterial | None
    audit: SameArticleRecovery


@dataclass(frozen=True)
class _ValidatedAlternateReporting:
    material: FetchedMaterial
    validation: AlternateReportingValidation


def attempt_same_article_recovery(
    candidate: Candidate,
    article_client,
    finder: SameArticleFinder | None,
) -> SameArticleOutcome:
    active_finder = finder or TavilySameArticleFinder.from_environment()
    audit = SameArticleRecovery(
        status="exhausted",
        provider=getattr(active_finder, "provider", "unknown"),
        query=candidate.story.title.strip()[:500],
    )
    try:
        discovered = active_finder.find(candidate)
    except Exception as exc:
        audit.error_code = (
            exc.error_code if isinstance(exc, SameArticleFinderError) else "finder_failed"
        )
        audit.status = "not_configured" if audit.error_code == "not_configured" else "finder_failed"
        LOGGER.info(
            "component=same_article_recovery item_id=%s status=%s code=%s",
            candidate.story.hn_item_id, audit.status, audit.error_code,
        )
        return SameArticleOutcome(None, audit)
    if not isinstance(discovered, list):
        audit.status, audit.error_code = "finder_failed", "malformed_results"
        return SameArticleOutcome(None, audit)

    bounded = discovered[:MAX_SAME_ARTICLE_CANDIDATES]
    audit.discovered_candidates = len(bounded)
    seen: list[str] = []
    for alternative in bounded:
        entry = SameArticleAttempt()
        audit.candidates.append(entry)
        if not isinstance(alternative, SameArticleCandidate):
            entry.reason = "malformed_candidate"
            continue
        url = normalize_same_article_url(alternative.url)
        if url is None:
            entry.reason = "unsupported_url"
            continue
        entry.url = url
        if urlsplit(url).hostname == "news.ycombinator.com":
            entry.reason = "hn_discussion"
            continue
        if same_source_url(url, candidate.story.source_url):
            entry.reason = "original_url"
            continue
        if any(same_source_url(url, previous) for previous in seen):
            entry.reason = "duplicate_url"
            continue
        seen.append(url)
        if audit.attempted_candidates >= MAX_SAME_ARTICLE_FETCHES:
            entry.reason = "fetch_budget_exhausted"
            continue
        audit.attempted_candidates += 1
        try:
            fetched = article_client(url)
            if not isinstance(fetched, ArticleFetchResult):
                entry.reason = "missing_source_evidence"
                continue
            material = coerce_fetched_material(fetched, url)
            entry.retrieved_url = material.retrieved_url
            entry.method = material.method
            if same_source_url(material.retrieved_url, candidate.story.source_url):
                entry.reason = "redirected_to_original"
                continue
            if urlsplit(material.retrieved_url).hostname == "news.ycombinator.com":
                entry.reason = "hn_discussion"
                continue
            validation = validate_same_article(candidate, alternative, fetched)
            entry.reason = validation.reason
            entry.evidence = [str(value)[:500] for value in validation.evidence[:5]]
            if not validation.accepted:
                continue
        except Exception as exc:
            entry.reason = (
                getattr(exc, "error_code", "") or "fetch_failed"
            )[:100]
            entry.status = "fetch_failed"
            continue
        entry.status = "accepted"
        audit.status = "success"
        LOGGER.info(
            "component=same_article_recovery item_id=%s status=success attempted=%d",
            candidate.story.hn_item_id, audit.attempted_candidates,
        )
        return SameArticleOutcome(material, audit)
    LOGGER.info(
        "component=same_article_recovery item_id=%s status=exhausted attempted=%d",
        candidate.story.hn_item_id, audit.attempted_candidates,
    )
    return SameArticleOutcome(None, audit)


def attempt_alternate_reporting_recovery(
    candidate: Candidate,
    article_client,
    finder: AlternateReportingFinder | None,
) -> AlternateReportingOutcome:
    active_finder = finder
    if active_finder is None:
        active_finder = TavilyAlternateReportingFinder.from_environment()
    provider = getattr(active_finder, "provider", "unknown")
    try:
        discovered = active_finder.find(candidate)
    except Exception as exc:
        error_code = (
            exc.error_code
            if isinstance(exc, AlternateReportingFinderError)
            else "finder_failed"
        )
        LOGGER.warning(
            "component=alternate_reporting_recovery item_id=%s provider=%s "
            "status=finder_failed code=%s",
            candidate.story.hn_item_id,
            provider,
            error_code,
        )
        return AlternateReportingOutcome(
            material=None,
            audit=AlternateReportingRecovery(
                status="finder_failed",
                provider=provider,
                error_code=error_code,
            ),
        )

    if not isinstance(discovered, list):
        return AlternateReportingOutcome(
            material=None,
            audit=AlternateReportingRecovery(
                status="finder_failed",
                provider=provider,
                error_code="malformed_results",
            ),
        )

    rejection_reasons: list[str] = []
    seen_urls: set[str] = set()
    yahoo_candidates: list[tuple[AlternateReportingCandidate, str]] = []
    reuters_candidates: list[tuple[AlternateReportingCandidate, str]] = []
    for alternate in discovered[:MAX_ALTERNATE_REPORTING_CANDIDATES]:
        if not isinstance(alternate, AlternateReportingCandidate):
            rejection_reasons.append("malformed_candidate")
            continue
        normalized_url = normalize_alternate_reporting_url(alternate.url)
        if normalized_url is None:
            rejection_reasons.append("unsupported_url")
            continue
        if normalized_url in seen_urls:
            rejection_reasons.append("duplicate_url")
            continue
        seen_urls.add(normalized_url)
        target = (
            yahoo_candidates
            if is_yahoo_url(normalized_url)
            else reuters_candidates
        )
        target.append((alternate, normalized_url))

    attempted = 0

    def validate_group(
        group: list[tuple[AlternateReportingCandidate, str]],
    ) -> list[_ValidatedAlternateReporting]:
        nonlocal attempted
        accepted: list[_ValidatedAlternateReporting] = []
        for alternate, normalized_url in group:
            attempted += 1
            try:
                material = coerce_fetched_material(
                    article_client(normalized_url),
                    normalized_url,
                )
            except Exception as exc:
                rejection_reasons.append("fetch_failed")
                LOGGER.warning(
                    "component=alternate_reporting_recovery item_id=%s "
                    "provider=%s status=candidate_fetch_failed code=%s",
                    candidate.story.hn_item_id,
                    provider,
                    getattr(exc, "error_code", "fetch_failed"),
                )
                continue
            effective_url = normalize_alternate_reporting_url(
                material.retrieved_url
            )
            if effective_url is None:
                rejection_reasons.append("redirected_to_unsupported_url")
                continue
            material = replace(material, retrieved_url=effective_url)
            validation = validate_alternate_reporting(
                candidate,
                alternate,
                material.text,
            )
            if not validation.accepted:
                rejection_reasons.append(validation.reason)
                continue
            accepted.append(
                _ValidatedAlternateReporting(
                    material=material,
                    validation=validation,
                )
            )
        return accepted

    accepted = validate_group(yahoo_candidates)
    if not accepted:
        accepted = validate_group(reuters_candidates)

    if accepted and validations_conflict(
        [item.validation for item in accepted]
    ):
        rejection_reasons.append("event_identity_conflict")
        return AlternateReportingOutcome(
            material=None,
            audit=AlternateReportingRecovery(
                status="conflict",
                provider=provider,
                discovered_candidates=len(discovered),
                attempted_candidates=attempted,
                rejection_reasons=rejection_reasons,
                error_code="event_identity_conflict",
            ),
        )

    if accepted:
        selected = min(
            accepted,
            key=lambda item: (
                -len(item.material.text),
                item.material.retrieved_url,
            ),
        )
        audit = AlternateReportingRecovery(
            status="success",
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        )
        LOGGER.info(
            "component=alternate_reporting_recovery item_id=%s provider=%s "
            "status=success discovered=%d attempted=%d",
            candidate.story.hn_item_id,
            provider,
            len(discovered),
            attempted,
        )
        return AlternateReportingOutcome(material=selected.material, audit=audit)

    status = "not_found" if not discovered else "exhausted"
    return AlternateReportingOutcome(
        material=None,
        audit=AlternateReportingRecovery(
            status=status,
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        ),
    )


def attempt_syndicated_recovery(
    candidate: Candidate,
    article_client,
    finder: SyndicatedCopyFinder | None,
) -> SyndicatedOutcome:
    active_finder = finder
    if active_finder is None:
        active_finder = TavilySyndicatedCopyFinder.from_environment()
    provider = getattr(active_finder, "provider", "unknown")
    try:
        discovered = active_finder.find(candidate)
    except Exception as exc:
        error_code = (
            exc.error_code
            if isinstance(exc, SyndicatedFinderError)
            else "finder_failed"
        )
        LOGGER.warning(
            "component=syndicated_recovery item_id=%s provider=%s "
            "status=finder_failed code=%s",
            candidate.story.hn_item_id,
            provider,
            error_code,
        )
        return SyndicatedOutcome(
            material=None,
            audit=SyndicatedRecovery(
                status="finder_failed",
                provider=provider,
                error_code=error_code,
            ),
        )

    if not isinstance(discovered, list):
        return SyndicatedOutcome(
            material=None,
            audit=SyndicatedRecovery(
                status="finder_failed",
                provider=provider,
                error_code="malformed_results",
            ),
        )

    attempted = 0
    rejection_reasons: list[str] = []
    seen_urls: set[str] = set()
    bounded_candidates = discovered[:MAX_SYNDICATED_CANDIDATES]
    for syndicated in bounded_candidates:
        if not isinstance(syndicated, SyndicatedCandidate):
            rejection_reasons.append("malformed_candidate")
            continue
        normalized_url = normalize_allowed_candidate_url(syndicated.url)
        if normalized_url is None:
            rejection_reasons.append("unsupported_url")
            continue
        if normalized_url in seen_urls:
            rejection_reasons.append("duplicate_url")
            continue
        seen_urls.add(normalized_url)
        attempted += 1
        try:
            material = coerce_fetched_material(
                article_client(normalized_url),
                normalized_url,
            )
        except Exception as exc:
            rejection_reasons.append("fetch_failed")
            LOGGER.warning(
                "component=syndicated_recovery item_id=%s provider=%s "
                "status=candidate_fetch_failed code=%s",
                candidate.story.hn_item_id,
                provider,
                getattr(exc, "error_code", "fetch_failed"),
            )
            continue
        effective_url = normalize_allowed_candidate_url(material.retrieved_url)
        if effective_url is None:
            rejection_reasons.append("redirected_to_unsupported_url")
            continue
        material = replace(material, retrieved_url=effective_url)
        validation = validate_syndicated_copy(candidate, syndicated, material.text)
        if not validation.accepted:
            rejection_reasons.append(validation.reason)
            continue
        audit = SyndicatedRecovery(
            status="success",
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        )
        LOGGER.info(
            "component=syndicated_recovery item_id=%s provider=%s "
            "status=success discovered=%d attempted=%d",
            candidate.story.hn_item_id,
            provider,
            len(discovered),
            attempted,
        )
        return SyndicatedOutcome(material=material, audit=audit)

    status = "not_found" if not discovered else "exhausted"
    audit = SyndicatedRecovery(
        status=status,
        provider=provider,
        discovered_candidates=len(discovered),
        attempted_candidates=attempted,
        rejection_reasons=rejection_reasons,
    )
    LOGGER.warning(
        "component=syndicated_recovery item_id=%s provider=%s "
        "status=%s discovered=%d attempted=%d",
        candidate.story.hn_item_id,
        provider,
        status,
        len(discovered),
        attempted,
    )
    return SyndicatedOutcome(material=None, audit=audit)
