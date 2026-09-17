import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from daily_brief import cli
from daily_brief.article_fetcher import ArticleFetchError, ArticleFetchResult
from daily_brief.models import Candidate, Story
from daily_brief.same_article import SameArticleCandidate, SameArticleFinderError
from daily_brief.source_evidence import SourceEvidence, SourceRelation
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

    result = cli.run_generate(
        tmp_path / "briefs", tmp_path / "data", date_label="2026-09-15",
        algolia_stories=[item.story], hot_stories=[], classifier=object(),
        summarizer=SimpleNamespace(summarize=summarize), article_fetcher=fetch,
        same_article_finder=same, alternate_reporting_finder=alternative,
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
    outcome = cli._attempt_same_article_recovery(candidate(), fetch, finder)
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
    outcome = cli._attempt_same_article_recovery(
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
        return cli.HNDiscussionResult(text="Readers discuss the result. " * 30,
                                      comments=3, chars=810, requested_items=3, failed_items=0)
    cli._summarize_selected_candidates(
        [item], [], SimpleNamespace(summarize=lambda c: "评论者讨论了评估方法。"),
        fetch, discussion, None, alternative, WINDOW, same_article_finder=same,
    )
    assert same.calls == alternative.calls == 1
    assert discussion_calls == ['123']
    assert item.summary_basis == 'hn_comments'
    assert item.article_retrieval.same_article_recovery.status == 'exhausted'
    assert item.article_retrieval.status == 'failed'


@pytest.mark.parametrize('mode,error', [
    (cli.RETRIEVAL_MODE_CLASSIFICATION, blocked()),
    (cli.RETRIEVAL_MODE_SUMMARY, ArticleFetchError('timeout', error_code='network_timeout')),
])
def test_search_never_runs_during_classification_or_nonchallenge_failure(mode, error):
    finder = Finder([COPY])
    def fetch(url, **kwargs):
        raise error
    item = candidate()
    assert not cli._prepare_candidate_material(
        item, fetch, None, None, WINDOW, retrieval_mode=mode, same_article_finder=finder,
    )
    assert finder.calls == 0
    assert item.article_retrieval.same_article_recovery.status == 'not_attempted'


def test_no_credentials_skips_search_without_network_and_keeps_audit():
    outcome = cli._attempt_same_article_recovery(candidate(), lambda url: pytest.fail('fetch'), None)
    assert outcome.audit.status == 'not_configured'
    assert outcome.audit.error_code == 'not_configured'
    assert outcome.audit.attempted_candidates == 0


def test_provider_failure_is_audited_without_exposing_exception_text():
    def fail(item):
        raise SameArticleFinderError('secret must not be logged', error_code='provider_http_error')
    outcome = cli._attempt_same_article_recovery(
        candidate(), lambda url: pytest.fail('fetch'), SimpleNamespace(find=fail, provider='test'),
    )
    assert outcome.audit.status == 'finder_failed'
    assert outcome.audit.error_code == 'provider_http_error'
    assert 'secret' not in repr(outcome.audit)
