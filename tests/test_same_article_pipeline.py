import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from daily_brief.article_fetcher import (
    ArticleFetchError,
    ArticleFetchResult,
    SourceEvidence,
    SourceRelation,
)
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import material, run_generate, summaries
from daily_brief.models import Candidate, Story
from daily_brief.recovery import (
    SameArticleCandidate,
    SameArticleFinderError,
    attempt_same_article_recovery,
)
from daily_brief.time_window import TimeWindow


ORIGINAL = "https://origin.example/posts/original"
COPY = "https://publisher.example/posts/copy"
TITLE = "AI agents still use engine sockets"
BODY = "\n".join([
    "The experiment gives agents a chess task and a socket connected to the opponent engine. " * 4,
    "Several agents use the exposed interface to request moves instead of reasoning about the position. " * 4,
    "The authors discuss limitations of this small experiment and describe the full evaluation procedure. " * 4,
])
WINDOW = TimeWindow(datetime(2026, 9, 14, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC), "2026-09-15")


@pytest.fixture(autouse=True)
def no_search_credentials(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def candidate():
    return Candidate(Story("algolia", "123", TITLE, ORIGINAL,
                           "https://news.ycombinator.com/item?id=123",
                           "2026-09-14T06:00:00Z", 100, 30))


def blocked():
    return ArticleFetchError("blocked", method="wayback", attempts=4,
                             fallback_attempted=True, fallback_reason="vercel_challenge",
                             error_code="wayback_no_capture")


def recovered(url=COPY, method="direct", relation="crosspost"):
    return ArticleFetchResult(
        BODY, method=method, extractor="trafilatura", retrieved_url=url,
        source_evidence=SourceEvidence(TITLE, "A Writer", (
            SourceRelation(ORIGINAL, "Explicit publisher relationship", relation),
        )),
    )


class Finder:
    provider = "test"

    def __init__(self, urls=()):
        self.urls = urls
        self.calls = 0

    def find(self, item):
        self.calls += 1
        return [SameArticleCandidate(TITLE, url) for url in self.urls]


def test_regeneration_uses_same_article_before_alternative_and_preserves_public_source(tmp_path):
    item = candidate()
    same = Finder([COPY])
    alternative = Finder()
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        if url == ORIGINAL:
            raise blocked()
        return recovered(url)

    summary_inputs = []
    def summarize(item):
        summary_inputs.append(item.story.fetched_text)
        return "研究测试了模型是否利用对手引擎，并讨论了小规模实验的限制。"

    result = run_generate(
        tmp_path / "briefs", tmp_path / "data", date_label="2026-09-15",
        algolia_stories=[item.story], hot_stories=[], classifier=object(),
        summarizer=SimpleNamespace(summarize=summarize), article_fetcher=fetch,
        same_article_finder=same, alternate_reporting_finder=alternative,
        hn_discussion_fetcher=lambda item_id: HNDiscussionResult("", 0, 0, 1, 0),
    )
    audit = json.loads(result.data_path.read_text())[0]["article_retrieval"]
    assert seen == [ORIGINAL, COPY]
    assert summary_inputs == [BODY.strip()]
    assert same.calls == 1 and alternative.calls == 0
    assert audit["material_origin"] == "same_article"
    assert audit["origin_failure"]["error_code"] == "wayback_no_capture"
    assert audit["retrieved_url"] == COPY
    recovery = audit["same_article_recovery"]
    assert recovery["status"] == "success"
    assert recovery["query"] == TITLE
    assert recovery["candidates"][0]["status"] == "accepted"
    assert recovery["candidates"][0]["evidence"]
    public = json.loads(result.public_json_path.read_text())["sections"]["ai"]["items"][0]
    assert public["source_url"] == ORIGINAL
    assert public["content_status"] == "ok"
    assert COPY not in result.public_json_path.read_text()


def test_candidate_budget_skips_original_hn_duplicates_and_does_not_recurse():
    urls = [ORIGINAL, candidate().story.hn_discussion_url, COPY, COPY + '#section']
    urls += [f"https://publisher.example/copy/{i}" for i in range(5)]
    finder = Finder(urls)
    seen = []
    def fetch(url):
        seen.append(url)
        raise blocked()
    outcome = attempt_same_article_recovery(candidate(), fetch, finder)
    assert outcome.material is None
    assert finder.calls == 1
    assert len(seen) == outcome.audit.attempted_candidates == 3
    reasons = [x.reason for x in outcome.audit.candidates]
    assert "original_url" in reasons and "hn_discussion" in reasons
    assert "duplicate_url" in reasons and "fetch_budget_exhausted" in reasons


@pytest.mark.parametrize("method,kind,expected", [
    ("youtube_caption", "narration", True),
    ("youtube_caption", "crosspost", False),
    ("direct", "canonical", False),
])
def test_youtube_requires_explicit_narration_not_similar_topic(method, kind, expected):
    video = 'https://www.youtube.com/watch?v=abcdefghijk'
    outcome = attempt_same_article_recovery(
        candidate(), lambda url: recovered(url, method, kind), Finder([video]),
    )
    assert (outcome.material is not None) == expected


def test_same_article_failure_continues_to_hn_discussion_after_alternative():
    item = candidate()
    same, alternative = Finder([COPY]), Finder()
    def fetch(url, **kwargs):
        if url == ORIGINAL:
            raise blocked()
        # Identical title, enough text, no publisher-declared relationship.
        return replace(recovered(url), source_evidence=SourceEvidence(TITLE))
    discussion_calls = []
    def discussion(item_id):
        discussion_calls.append(item_id)
        return HNDiscussionResult(text="Readers discuss the result. " * 30,
                                      comments=3, chars=810, requested_items=3, failed_items=0)
    def summarize_comments(candidate):
        candidate.summary_sources_used = ["hn_comments"]
        return "根据 Hacker News 部分评论：评论者讨论了评估方法。"

    summaries.summarize_selected_candidates(
        [item], [], SimpleNamespace(summarize=summarize_comments),
        fetch, discussion, None, alternative, WINDOW, same_article_finder=same,
    )
    assert same.calls == alternative.calls == 1
    assert discussion_calls == ['123']
    assert item.summary_basis == 'hn_comments'
    assert item.article_retrieval.same_article_recovery.status == 'exhausted'
    assert item.article_retrieval.status == 'failed'


@pytest.mark.parametrize('mode,error', [
    (material.RETRIEVAL_MODE_CLASSIFICATION, blocked()),
    (material.RETRIEVAL_MODE_SUMMARY, ArticleFetchError('timeout', error_code='network_timeout')),
])
def test_search_never_runs_during_classification_or_nonchallenge_failure(mode, error):
    finder = Finder([COPY])
    def fetch(url, **kwargs):
        raise error
    item = candidate()
    assert not material.prepare_candidate_material(
        item, fetch, None, None, WINDOW, retrieval_mode=mode, same_article_finder=finder,
    )
    assert finder.calls == 0
    assert item.article_retrieval.same_article_recovery.status == 'not_attempted'


def test_no_credentials_skips_search_without_network_and_keeps_audit():
    outcome = attempt_same_article_recovery(candidate(), lambda url: pytest.fail('fetch'), None)
    assert outcome.audit.status == 'not_configured'
    assert outcome.audit.error_code == 'not_configured'
    assert outcome.audit.attempted_candidates == 0


def test_provider_failure_is_audited_without_exposing_exception_text():
    def fail(item):
        raise SameArticleFinderError('secret must not be logged', error_code='provider_http_error')
    outcome = attempt_same_article_recovery(
        candidate(), lambda url: pytest.fail('fetch'), SimpleNamespace(find=fail, provider='test'),
    )
    assert outcome.audit.status == 'finder_failed'
    assert outcome.audit.error_code == 'provider_http_error'
    assert 'secret' not in repr(outcome.audit)


@pytest.mark.parametrize("reason", [
    "http_403", "http_404", "http_410", "http_500", "empty_content",
    "network_timeout", "request_failed", "tls_issuer_unavailable",
])
def test_exhausted_ordinary_retrieval_tries_same_article_before_other_reporting(reason):
    item = candidate()
    same, alternative = Finder([COPY]), Finder()
    seen = []

    def fetch(url, **kwargs):
        seen.append(url)
        if url == ORIGINAL:
            raise ArticleFetchError(
                "backups exhausted", method="wayback", attempts=4,
                fallback_attempted=True, fallback_reason=reason,
                error_code="wayback_no_capture",
            )
        return recovered(url)

    assert material.prepare_candidate_material(
        item, fetch, None, alternative, WINDOW, same_article_finder=same,
    )
    assert seen == [ORIGINAL, COPY]
    assert same.calls == 1 and alternative.calls == 0
    assert item.article_retrieval.material_origin == "same_article"
    assert item.article_retrieval.origin_failure.fallback_reason == reason


@pytest.mark.parametrize("reason,method,code,attempted", [
    ("http_400", "wayback", "wayback_no_capture", True),
    ("http_401", "wayback", "wayback_no_capture", True),
    ("http_407", "wayback", "wayback_no_capture", True),
    ("http_422", "wayback", "wayback_no_capture", True),
    ("http_429", "wayback", "wayback_no_capture", True),
    ("http_451", "wayback", "wayback_no_capture", True),
    ("rate_limited", "wayback", "rate_limited", True),
    ("http_403", "wayback", "retrieval_budget_exhausted", True),
    ("http_403", "wayback", "unsafe_url", True),
    ("url_trailing_backslash", "direct", "http_404", True),
    ("http_403", "github_readme", "http_403", True),
    ("http_403", "youtube_caption", "http_403", True),
    ("http_403", "direct", "http_403", False),
    ("http_403", "wayback", "wayback_no_capture", False),
])
def test_search_excludes_terminal_specialized_and_unexhausted_failures(reason, method, code, attempted):
    item = candidate()
    same, alternative = Finder([COPY]), Finder()

    def fetch(url, **kwargs):
        raise ArticleFetchError(
            "terminal failure", method=method, error_code=code,
            fallback_attempted=attempted, fallback_reason=reason,
        )

    assert not material.prepare_candidate_material(
        item, fetch, None, alternative, WINDOW, same_article_finder=same,
    )
    assert same.calls == alternative.calls == 0
    assert item.article_retrieval.same_article_recovery.status == "not_attempted"


def test_item_deadline_is_shared_with_search_and_stops_recovery():
    from daily_brief.article_fetcher.budget import retrieval_budget

    now = [0.0]
    same, alternative = Finder([COPY]), Finder()
    item = candidate()

    def fetch(url, **kwargs):
        now[0] = 11.0
        raise blocked()

    with retrieval_budget(timeout_seconds=10, clock=lambda: now[0]):
        assert not material.prepare_candidate_material(
            item, fetch, None, alternative, WINDOW, same_article_finder=same,
        )
    assert same.calls == alternative.calls == 0
    assert item.article_retrieval.error_code == "retrieval_budget_exhausted"
    assert item.article_retrieval.fallback_reason == "vercel_challenge"


@pytest.mark.parametrize("max_requests,expected_same,expected_alternate", [
    (1, 0, 0),
    (2, 1, 0),
    (3, 1, 1),
])
def test_request_cap_stops_new_search_stages_and_preserves_finished_audits(
    max_requests, expected_same, expected_alternate,
):
    from daily_brief.article_fetcher.budget import retrieval_budget
    from daily_brief.recovery import AlternateReportingCandidate

    same, alternative = Finder([COPY]), Finder()
    alternate_url = "https://other.example/report"
    def find_alternative(item):
        alternative.calls += 1
        return [AlternateReportingCandidate(TITLE, alternate_url)]
    alternative.find = find_alternative
    item = candidate()
    seen = []

    with retrieval_budget(max_requests=max_requests) as budget:
        def fetch(url, **kwargs):
            budget.reserve_request()
            seen.append(url)
            if url == ORIGINAL:
                raise blocked()
            raise ArticleFetchError("candidate failed", error_code="http_403")

        assert not material.prepare_candidate_material(
            item, fetch, None, alternative, WINDOW, same_article_finder=same,
        )

    assert same.calls == expected_same
    assert alternative.calls == expected_alternate
    assert len(seen) == max_requests
    retrieval = item.article_retrieval
    assert retrieval.error_code == "retrieval_budget_exhausted"
    assert retrieval.fallback_reason == "vercel_challenge"
    if expected_same:
        assert retrieval.same_article_recovery.status == "exhausted"
        assert retrieval.same_article_recovery.attempted_candidates == 1
        assert retrieval.same_article_recovery.candidates[0].status == "fetch_failed"
    else:
        assert retrieval.same_article_recovery.status == "not_attempted"
    assert retrieval.alternate_reporting_recovery.status == (
        "exhausted" if expected_alternate else "not_attempted"
    )


def test_last_allowed_request_can_successfully_recover_same_article():
    from daily_brief.article_fetcher.budget import retrieval_budget

    item = candidate()
    same, alternative = Finder([COPY]), Finder()
    with retrieval_budget(max_requests=2) as budget:
        def fetch(url, **kwargs):
            budget.reserve_request()
            if url == ORIGINAL:
                raise blocked()
            return recovered(url)
        assert material.prepare_candidate_material(
            item, fetch, None, alternative, WINDOW, same_article_finder=same,
        )
    assert item.article_retrieval.status == "success"
    assert same.calls == 1 and alternative.calls == 0
