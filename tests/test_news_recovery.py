"""Synthetic news recovery regressions; no live pages or provider calls."""
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from daily_brief.article_fetcher import ArticleFetchError, ArticleFetchResult, SourceEvidence, SourceRelation
from daily_brief.generation.material import prepare_candidate_material
from daily_brief.models import Candidate, Story
from daily_brief.recovery import SameArticleCandidate, AlternateReportingCandidate, SyndicatedCandidate
from daily_brief.recovery.syndicated_copy import SyndicatedFinderError
from daily_brief.recovery.alternate_reporting import validate_alternate_reporting
from daily_brief.recovery.fetched_material import coerce_fetched_material
from daily_brief.recovery.search_recovery import attempt_alternate_reporting_recovery
from daily_brief.time_window import TimeWindow

ORIGINAL = 'https://www.techspot.com/news/123-florida-woman-claude-diary-anthropic-police.html'
YAHOO = 'https://www.yahoo.com/news/articles/claude-diary-police.html'
OTHER = 'https://decrypt.co/123/claude-diary-police'
BODY = (
    'A Florida woman used Claude as a diary and allegedly wrote an entry threatening a local police office. '
    'Anthropic reported the entry to police after a reviewer examined it, according to the arrest report.\n'
    'The woman now faces a felony charge for a written threat, and investigators described the sequence of events. '
    'The allegation has not been decided by a court, and the report distinguishes the charge from a conviction.\n'
    'The company explains that its emergency disclosure policy permits limited sharing to prevent serious harm. '
    'The reporting also describes questions about the privacy expectations of people writing to a chatbot.'
)


def candidate():
    return Candidate(Story(
        'algolia', '123', 'Anthropic reported diary entry to police, woman faces felony charge',
        ORIGINAL, 'https://news.ycombinator.com/item?id=123', '2026-10-05T05:37:40Z', 492, 421,
    ))


def fetched(url=YAHOO, body=BODY, published_at='2026-10-05T21:16:03Z'):
    return ArticleFetchResult(
        body, method='direct', extractor='trafilatura', retrieved_url=url,
        source_evidence=SourceEvidence(
            'Florida resident faces charge after Claude diary is reported',
            relations=(SourceRelation(OTHER, 'Publisher canonical', 'canonical'),),
            published_at=published_at,
        ),
    )


def validate(result, item=None):
    return validate_alternate_reporting(
        item or candidate(), AlternateReportingCandidate(candidate().story.title, result.retrieved_url),
        result.text, source_evidence=result.source_evidence,
    )


@pytest.mark.parametrize('syndicated_status', [None, 'not_found', 'finder_failed', 'exhausted'])
def test_same_article_candidate_is_reused_as_other_reporting_without_second_search(syndicated_status):
    item = candidate()
    original = ORIGINAL
    if syndicated_status:
        original = 'https://www.reuters.com/technology/anthropic-claude-diary-police-2026-10-05/'
        item.story = replace(item.story, source_url=original)
    calls = []
    syndicated_calls = []
    copy_url = 'https://finance.yahoo.com/news/claude-diary-police.html'

    def find_syndicated(item):
        syndicated_calls.append(item.story.source_url)
        if syndicated_status == 'finder_failed':
            raise SyndicatedFinderError('search unavailable', error_code='provider_http_error')
        if syndicated_status == 'exhausted':
            return [SyndicatedCandidate('Search title', copy_url)]
        return []

    def fetch(url, **kwargs):
        calls.append(url)
        if url == original:
            raise ArticleFetchError(
                'challenge', method='wayback', error_code='wayback_challenge_page',
                fallback_attempted=True, fallback_reason='challenge_page',
            )
        if url == copy_url:
            return fetched(copy_url, body='A teaser without substantive reporting.')
        assert url == YAHOO
        return fetched()

    same_finder = SimpleNamespace(provider='fake', find=lambda item: [SameArticleCandidate('Search title', YAHOO)])
    syndicated_finder = SimpleNamespace(provider='fake', find=find_syndicated)
    other_finder = SimpleNamespace(find=lambda item: pytest.fail('must reuse already fetched reporting'))
    window = TimeWindow(datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC), '2026-10-06')
    assert prepare_candidate_material(item, fetch, syndicated_finder, other_finder, window, same_article_finder=same_finder)
    assert calls == [original, YAHOO] + ([copy_url] if syndicated_status == 'exhausted' else [])
    assert syndicated_calls == ([original] if syndicated_status else [])
    assert item.article_retrieval.syndicated_recovery.status == (syndicated_status or 'not_attempted')
    assert item.story.source_url == original
    assert item.story.fetched_text == BODY
    assert item.article_retrieval.material_origin == 'alternate_reporting'
    assert item.article_retrieval.retrieved_url == YAHOO
    assert item.article_retrieval.origin_failure.error_code == 'wayback_challenge_page'
    assert item.article_retrieval.same_article_recovery.candidates[0].reason == 'missing_explicit_source_relation'
    assert item.article_retrieval.alternate_reporting_recovery.provider == 'same_article'


@pytest.mark.parametrize('limit', ['requests', 'deadline'])
def test_reuters_syndication_cannot_start_other_reporting_after_budget_exhaustion(limit):
    from daily_brief.article_fetcher.budget import retrieval_budget

    item = candidate()
    item.story = replace(item.story, source_url='https://www.reuters.com/technology/claude-diary-2026-10-05/')
    now = [0.0]
    calls = []

    with retrieval_budget(timeout_seconds=10, max_requests=2 if limit == 'requests' else 3, clock=lambda: now[0]) as budget:
        def fetch(url, **kwargs):
            budget.reserve_request()
            calls.append(url)
            raise ArticleFetchError(
                'blocked', method='wayback', error_code='wayback_no_capture',
                fallback_attempted=True, fallback_reason='datadome_challenge',
            )

        def find_syndicated(item):
            budget.reserve_request()
            if limit == 'deadline':
                now[0] = 11
            return []

        assert not prepare_candidate_material(
            item, fetch, SimpleNamespace(provider='fake', find=find_syndicated),
            SimpleNamespace(find=lambda item: pytest.fail('no budget for another route')),
            TimeWindow(datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC), '2026-10-06'),
            same_article_finder=SimpleNamespace(provider='fake', find=lambda item: []),
        )

    assert calls == [item.story.source_url]
    retrieval = item.article_retrieval
    assert retrieval.error_code == 'retrieval_budget_exhausted'
    assert retrieval.fallback_reason == 'datadome_challenge'
    assert retrieval.same_article_recovery.status == 'exhausted'
    assert retrieval.syndicated_recovery.status == 'not_found'
    assert retrieval.alternate_reporting_recovery.status == 'not_attempted'


def test_other_reporting_search_accepts_a_non_reuters_publisher():
    outcome = attempt_alternate_reporting_recovery(
        candidate(), lambda url: fetched(url),
        SimpleNamespace(provider='fake', find=lambda item: [AlternateReportingCandidate('Ignored', OTHER)]),
    )
    assert outcome.audit.status == 'success'
    assert outcome.material.retrieved_url == OTHER


@pytest.mark.parametrize('published_at', ['', '2025-10-05T21:16:03Z', '2026-09-05', 'October 5, 2025'])
def test_missing_or_stale_publication_date_is_rejected(published_at):
    assert validate(fetched(published_at=published_at)).reason == 'date_mismatch'


def test_old_publication_date_cannot_be_overridden_by_a_recent_body_date():
    result = fetched(body='October 5, 2026\n' + BODY, published_at='2025-10-05T21:16:03Z')
    assert validate(result).reason == 'date_mismatch'


def test_current_iso_timestamp_and_comma_date_are_accepted():
    assert validate(fetched()).accepted
    assert validate(fetched(published_at='October 5, 2026')).accepted


def test_metadata_and_search_title_cannot_rescue_unrelated_body():
    unrelated = (
        'Anthropic is discussing accelerator manufacturing with a chip company to increase computing capacity. '
        'The proposed hardware partnership is still being negotiated and no contract has yet been announced. '
    ) * 3
    result = fetched(body='Page metadata (publisher-provided context, not article body):\ntitle: '
                     + candidate().story.title + '\n\nExtracted body:\n' + unrelated)
    assert validate(result).reason == 'insufficient_event_signals'


def test_same_event_words_without_source_company_are_rejected():
    assert validate(fetched(body=BODY.replace('Anthropic', 'Another company'))).reason == 'entity_mismatch'


@pytest.mark.parametrize('target,reason', [
    ('http://127.0.0.1/private', 'redirected_to_unsupported_url'),
    ('https://www.reddit.com/r/news/item', 'redirected_to_unsupported_url'),
    (ORIGINAL, 'redirected_to_original'),
])
def test_other_reporting_rejects_unsafe_discussion_and_original_redirects(target, reason):
    outcome = attempt_alternate_reporting_recovery(
        candidate(), lambda url: fetched(target),
        SimpleNamespace(provider='fake', find=lambda item: [AlternateReportingCandidate('Ignored', YAHOO)]),
    )
    assert outcome.material is None
    assert outcome.audit.rejection_reasons == [reason]


def test_reused_invalid_page_is_not_fetched_again_and_validation_budget_is_shared():
    invalid = coerce_fetched_material(fetched(published_at=''), YAHOO)
    seen = []
    urls = [YAHOO] + [f'https://publisher.example/{i}' for i in range(4)]

    def fetch(url):
        seen.append(url)
        return fetched(url, published_at='')

    outcome = attempt_alternate_reporting_recovery(
        candidate(), fetch,
        SimpleNamespace(provider='fake', find=lambda item: [AlternateReportingCandidate('Ignored', url) for url in urls]),
        fetched_candidates=(invalid, invalid),
    )
    assert YAHOO not in seen
    assert len(seen) == 4
    assert outcome.audit.attempted_candidates == 5
    assert outcome.audit.rejection_reasons.count('duplicate_url') == 2
    assert outcome.material is None
