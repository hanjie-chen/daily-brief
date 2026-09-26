from __future__ import annotations

import logging
from collections.abc import Callable
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

RecoveryAudit = SameArticleRecovery | SyndicatedRecovery | AlternateReportingRecovery


@dataclass(frozen=True)
class RecoveryOutcome:
    material: FetchedMaterial | None
    audit: RecoveryAudit


def attempt_same_article_recovery(
    candidate: Candidate,
    article_client,
    finder: SameArticleFinder | None,
) -> RecoveryOutcome:
    active_finder = finder or TavilySameArticleFinder.from_environment()
    audit = SameArticleRecovery(
        status="exhausted",
        provider=getattr(active_finder, "provider", "unknown"),
        query=candidate.story.title.strip()[:500],
    )
    try:
        discovered = active_finder.find(candidate)
    except Exception as exc:
        audit.error_code = _finder_error_code(exc, SameArticleFinderError)
        audit.status = "not_configured" if audit.error_code == "not_configured" else "finder_failed"
        LOGGER.info(
            "component=same_article_recovery item_id=%s status=%s code=%s",
            candidate.story.hn_item_id, audit.status, audit.error_code,
        )
        return RecoveryOutcome(None, audit)
    if not isinstance(discovered, list):
        audit.status, audit.error_code = "finder_failed", "malformed_results"
        return RecoveryOutcome(None, audit)

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
        return RecoveryOutcome(material, audit)
    LOGGER.info(
        "component=same_article_recovery item_id=%s status=exhausted attempted=%d",
        candidate.story.hn_item_id, audit.attempted_candidates,
    )
    return RecoveryOutcome(None, audit)


def attempt_alternate_reporting_recovery(
    candidate: Candidate,
    article_client,
    finder: AlternateReportingFinder | None,
) -> RecoveryOutcome:
    component = "alternate_reporting_recovery"
    active_finder = finder
    if active_finder is None:
        active_finder = TavilyAlternateReportingFinder.from_environment()
    provider, discovered, failure = _discover(
        candidate,
        active_finder,
        error_type=AlternateReportingFinderError,
        component=component,
        audit_type=AlternateReportingRecovery,
    )
    if failure is not None:
        return RecoveryOutcome(None, failure)

    rejection_reasons: list[str] = []
    seen_urls: set[str] = set()
    yahoo_candidates: list[tuple[AlternateReportingCandidate, str]] = []
    reuters_candidates: list[tuple[AlternateReportingCandidate, str]] = []
    for alternate in discovered[:MAX_ALTERNATE_REPORTING_CANDIDATES]:
        normalized_url = _allowed_candidate_url(
            alternate,
            AlternateReportingCandidate,
            normalize_alternate_reporting_url,
            seen_urls,
            rejection_reasons,
        )
        if normalized_url is None:
            continue
        target = (
            yahoo_candidates
            if is_yahoo_url(normalized_url)
            else reuters_candidates
        )
        target.append((alternate, normalized_url))

    attempted = 0

    def validate_group(
        group: list[tuple[AlternateReportingCandidate, str]],
    ) -> list[tuple[FetchedMaterial, AlternateReportingValidation]]:
        nonlocal attempted
        accepted = []
        for alternate, normalized_url in group:
            attempted += 1
            verified = _fetch_and_validate(
                candidate,
                alternate,
                normalized_url,
                article_client,
                normalize_url=normalize_alternate_reporting_url,
                validate=validate_alternate_reporting,
                component=component,
                provider=provider,
                rejection_reasons=rejection_reasons,
            )
            if verified is not None:
                accepted.append(verified)
        return accepted

    accepted = validate_group(yahoo_candidates)
    if not accepted:
        accepted = validate_group(reuters_candidates)

    if accepted and validations_conflict(
        [validation for _material, validation in accepted]
    ):
        rejection_reasons.append("event_identity_conflict")
        return RecoveryOutcome(
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
        selected, _validation = min(
            accepted,
            key=lambda item: (-len(item[0].text), item[0].retrieved_url),
        )
        audit = AlternateReportingRecovery(
            status="success",
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        )
        _log_success(component, candidate, provider, len(discovered), attempted)
        return RecoveryOutcome(material=selected, audit=audit)

    status = "not_found" if not discovered else "exhausted"
    return RecoveryOutcome(
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
) -> RecoveryOutcome:
    component = "syndicated_recovery"
    active_finder = finder
    if active_finder is None:
        active_finder = TavilySyndicatedCopyFinder.from_environment()
    provider, discovered, failure = _discover(
        candidate,
        active_finder,
        error_type=SyndicatedFinderError,
        component=component,
        audit_type=SyndicatedRecovery,
    )
    if failure is not None:
        return RecoveryOutcome(None, failure)

    attempted = 0
    rejection_reasons: list[str] = []
    seen_urls: set[str] = set()
    for syndicated in discovered[:MAX_SYNDICATED_CANDIDATES]:
        normalized_url = _allowed_candidate_url(
            syndicated,
            SyndicatedCandidate,
            normalize_allowed_candidate_url,
            seen_urls,
            rejection_reasons,
        )
        if normalized_url is None:
            continue
        attempted += 1
        verified = _fetch_and_validate(
            candidate,
            syndicated,
            normalized_url,
            article_client,
            normalize_url=normalize_allowed_candidate_url,
            validate=validate_syndicated_copy,
            component=component,
            provider=provider,
            rejection_reasons=rejection_reasons,
        )
        if verified is None:
            continue
        audit = SyndicatedRecovery(
            status="success",
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        )
        _log_success(component, candidate, provider, len(discovered), attempted)
        return RecoveryOutcome(material=verified[0], audit=audit)

    status = "not_found" if not discovered else "exhausted"
    audit = SyndicatedRecovery(
        status=status,
        provider=provider,
        discovered_candidates=len(discovered),
        attempted_candidates=attempted,
        rejection_reasons=rejection_reasons,
    )
    LOGGER.warning(
        "component=%s item_id=%s provider=%s status=%s discovered=%d attempted=%d",
        component,
        candidate.story.hn_item_id,
        provider,
        status,
        len(discovered),
        attempted,
    )
    return RecoveryOutcome(material=None, audit=audit)


def _finder_error_code(exc: Exception, error_type: type[Exception]) -> str:
    return exc.error_code if isinstance(exc, error_type) else "finder_failed"


def _discover(
    candidate: Candidate,
    finder,
    *,
    error_type: type[Exception],
    component: str,
    audit_type: type[SyndicatedRecovery] | type[AlternateReportingRecovery],
):
    """Run an allowlisted route's finder.

    Returns (provider, results, None), or (provider, None, failure_audit).
    """
    provider = getattr(finder, "provider", "unknown")
    try:
        discovered = finder.find(candidate)
    except Exception as exc:
        error_code = _finder_error_code(exc, error_type)
        LOGGER.warning(
            "component=%s item_id=%s provider=%s status=finder_failed code=%s",
            component,
            candidate.story.hn_item_id,
            provider,
            error_code,
        )
        return provider, None, audit_type(
            status="finder_failed", provider=provider, error_code=error_code
        )
    if not isinstance(discovered, list):
        return provider, None, audit_type(
            status="finder_failed", provider=provider, error_code="malformed_results"
        )
    return provider, discovered, None


def _allowed_candidate_url(
    item,
    candidate_type: type,
    normalize_url: Callable[[str], str | None],
    seen_urls: set[str],
    rejection_reasons: list[str],
) -> str | None:
    """Return a new allowlisted URL for a search result, or record why not."""
    if not isinstance(item, candidate_type):
        rejection_reasons.append("malformed_candidate")
        return None
    normalized_url = normalize_url(item.url)
    if normalized_url is None:
        rejection_reasons.append("unsupported_url")
        return None
    if normalized_url in seen_urls:
        rejection_reasons.append("duplicate_url")
        return None
    seen_urls.add(normalized_url)
    return normalized_url


def _fetch_and_validate(
    candidate: Candidate,
    alternative,
    url: str,
    article_client,
    *,
    normalize_url: Callable[[str], str | None],
    validate,
    component: str,
    provider: str,
    rejection_reasons: list[str],
):
    """Fetch one allowlisted page and validate it.

    Returns (material, validation) when accepted; otherwise records the reason
    and returns None. A redirect off the allowlist is rejected.
    """
    try:
        material = coerce_fetched_material(article_client(url), url)
    except Exception as exc:
        rejection_reasons.append("fetch_failed")
        LOGGER.warning(
            "component=%s item_id=%s provider=%s status=candidate_fetch_failed code=%s",
            component,
            candidate.story.hn_item_id,
            provider,
            getattr(exc, "error_code", "fetch_failed"),
        )
        return None
    effective_url = normalize_url(material.retrieved_url)
    if effective_url is None:
        rejection_reasons.append("redirected_to_unsupported_url")
        return None
    material = replace(material, retrieved_url=effective_url)
    validation = validate(candidate, alternative, material.text)
    if not validation.accepted:
        rejection_reasons.append(validation.reason)
        return None
    return material, validation


def _log_success(
    component: str, candidate: Candidate, provider: str, discovered: int, attempted: int
) -> None:
    LOGGER.info(
        "component=%s item_id=%s provider=%s status=success discovered=%d attempted=%d",
        component,
        candidate.story.hn_item_id,
        provider,
        discovered,
        attempted,
    )
