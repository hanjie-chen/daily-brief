"""Run-level material recovery uses fake requests and fake ten-minute waits."""

import errno

import pytest

from daily_brief.article_fetcher import ArticleFetchError
from daily_brief.article_fetcher.budget import budgeted_opener, remaining_seconds
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import material
from daily_brief.generation.summaries import summarize_selected_candidates
from daily_brief.models import Candidate
from daily_brief.network_recovery import NetworkRecovery, observe_network_failure, recoverable_network
from daily_brief.recovery import SameArticleFinderError
from daily_brief.time_window import daily_window
from fakes import FakeSummarizer, story


@pytest.fixture(autouse=True)
def unavailable_hn(monkeypatch):
    checks = []

    def check():
        assert remaining_seconds() is None
        checks.append("HN")
        raise TimeoutError("HN unavailable")

    monkeypatch.setattr("daily_brief.network_recovery.check_hn_connection", check)
    return checks


def no_comments(item_id):
    return HNDiscussionResult(text="", comments=0, chars=0, requested_items=1, failed_items=0)


def prepare(candidate, fetcher, **kwargs):
    return material.prepare_candidate_material(
        candidate, fetcher, None, None, daily_window(), **kwargs,
    )


def summarize(candidates, fetcher, comments=no_comments, summarizer=None):
    summarizer = summarizer or FakeSummarizer()
    summarize_selected_candidates(
        candidates, [], summarizer, fetcher, comments, None, None, daily_window(),
    )
    return summarizer


def test_source_retry_has_new_request_budget_and_waits_outside_deadline():
    candidate = Candidate(story("1", "Rust release"))
    calls = []
    slept = []
    opener = budgeted_opener(lambda *args, **kwargs: None)

    def fetch(url, **kwargs):
        calls.append(url)
        for _ in range(24):
            opener(url)
        if len(calls) == 1:
            raise OSError(errno.ENETDOWN, "Local network unavailable")
        return "Recovered source body."

    def sleep(seconds):
        assert remaining_seconds() is None
        slept.append(seconds)

    with NetworkRecovery(sleeper=sleep, max_pauses=1) as recovery:
        assert prepare(candidate, fetch)
    assert recovery.pauses == 1
    assert slept == [600]
    assert len(calls) == 2
    assert candidate.story.fetched_text == "Recovered source body."


def test_observed_local_failure_survives_unavailable_archive(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    candidate = Candidate(story("1", "Rust release"))
    calls = []
    slept = []

    def fetch(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            observe_network_failure(OSError(errno.ENETDOWN, "Local network unavailable"))
            try:
                raise ArticleFetchError("No capture", error_code="wayback_no_capture")
            except ArticleFetchError as exc:
                raise ArticleFetchError(
                    "Source network failed; archive unavailable",
                    error_code="wayback_no_capture", method="wayback",
                    fallback_reason="request_failed", fallback_attempted=True,
                ) from exc
        return "Recovered source body."

    with NetworkRecovery(sleeper=slept.append, max_pauses=1):
        assert prepare(candidate, fetch)
    assert slept == [600]
    assert len(calls) == 2


@pytest.mark.parametrize("body_source", ["web", "post", "comments"])
def test_available_material_avoids_long_wait(body_source, unavailable_hn):
    candidate = Candidate(story(
        "1", "Rust release", story_text="Useful post." if body_source == "post" else "",
    ))
    slept = []

    def fetch(url, **kwargs):
        if body_source == "web":
            return "Useful source body."
        raise ArticleFetchError("Network timeout", error_code="network_timeout")

    def comments(item_id):
        if body_source == "comments":
            return HNDiscussionResult(
                text="Useful discussion.", comments=1, chars=18,
                requested_items=2, failed_items=0,
            )
        raise TimeoutError("Discussion connection timeout")

    with NetworkRecovery(sleeper=slept.append, max_pauses=1):
        summarize([candidate], fetch, comments)
    assert slept == []
    assert candidate.summary_status == "success"
    assert unavailable_hn == []


def test_resume_current_item_preserves_completed_summary():
    first = Candidate(story("1", "Rust release"))
    second = Candidate(story("2", "Python release"))
    calls = []
    slept = []

    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith("/2") and calls.count(url) == 1:
            raise OSError(errno.ENETUNREACH, "Local route unavailable")
        return "Useful source body."

    with NetworkRecovery(sleeper=slept.append, max_pauses=1):
        summarizer = summarize([first, second], fetch)
    assert calls == [first.story.source_url, second.story.source_url, second.story.source_url]
    assert summarizer.titles == [first.story.title, second.story.title]
    assert slept == [600]
    assert all(item.summary_status == "success" for item in (first, second))


def test_candidates_share_pause_allowance():
    candidates = [Candidate(story(str(i), "Rust release")) for i in (1, 2)]
    calls = []
    slept = []

    def fetch(url, **kwargs):
        calls.append(url)
        raise OSError(errno.ENETDOWN, "Local network unavailable")

    with NetworkRecovery(sleeper=slept.append, max_pauses=1) as recovery:
        summarize(candidates, fetch)
    assert slept == [600]
    assert recovery.exhausted
    assert calls == [candidates[0].story.source_url] * 2 + [candidates[1].story.source_url]
    assert all(item.summary_status == "skipped" for item in candidates)


def test_network_failed_search_is_observed_before_fallback_audit(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    candidate = Candidate(story("1", "Rust release"))
    searched = []
    fetched = []
    slept = []

    class Finder:
        provider = "fake"

        def find(self, candidate):
            searched.append(candidate.story.hn_item_id)
            raise SameArticleFinderError("Search disconnected", error_code="provider_request_failed") from OSError(errno.ENETDOWN, "Local network unavailable")

    def fetch(url, **kwargs):
        fetched.append(url)
        if len(fetched) == 1:
            raise ArticleFetchError(
                "No capture", method="wayback", error_code="wayback_no_capture",
                fallback_reason="http_404", fallback_attempted=True,
            )
        return "Recovered source body."

    with NetworkRecovery(sleeper=slept.append, max_pauses=1):
        assert prepare(candidate, fetch, same_article_finder=Finder())
    assert searched == ["1"]
    assert slept == [600]


def test_definitive_missing_source_does_not_wait(monkeypatch, unavailable_hn):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    candidate = Candidate(story("1", "Rust release"))
    slept = []

    def fetch(url, **kwargs):
        raise ArticleFetchError(
            "No capture", method="wayback", error_code="wayback_no_capture",
            fallback_reason="http_404", fallback_attempted=True,
        )

    with NetworkRecovery(sleeper=slept.append, max_pauses=1):
        assert not prepare(candidate, fetch)
    assert slept == []
    assert unavailable_hn == []


def test_empty_discussion_with_failed_comment_requests_is_retried():
    candidate = Candidate(story("1", "Rust release"))
    calls, waits = [], []

    def fetch(url, **kwargs):
        raise ArticleFetchError("Gone", error_code="http_404")

    def comments(item_id):
        calls.append(item_id)
        if len(calls) == 1:
            # The real per-comment HTTP boundary observes before the bounded
            # sampler turns three failed child requests into an empty sample.
            observe_network_failure(TimeoutError(), critical=True)
            return HNDiscussionResult("", 0, 0, 4, 3)
        return HNDiscussionResult("Recovered discussion.", 1, 21, 2, 0)

    with NetworkRecovery(sleeper=waits.append, max_pauses=1):
        summarize([candidate], fetch, comments)
    assert calls == ["1", "1"]
    assert waits == [600]
    assert candidate.summary_status == "success"
    assert candidate.discussion_text == "Recovered discussion."


def test_exhausted_classification_does_not_replace_files_with_no_content(tmp_path):
    from daily_brief.generation import SourceCollectionError, run_generate
    from fakes import FakeClassifier

    class Classifier(FakeClassifier):
        @recoverable_network("classification")
        def classify(self, candidates):
            raise TimeoutError()

    output_dir = tmp_path / "briefs"
    output_dir.mkdir()
    existing = output_dir / "2026-10-12.json"
    existing.write_text("existing brief")
    with NetworkRecovery(sleeper=lambda _: pytest.fail("no allowance"), max_pauses=0):
        with pytest.raises(SourceCollectionError, match="empty selection is inconclusive"):
            run_generate(
                output_dir=output_dir, data_dir=tmp_path / "data",
                date_label="2026-10-12", algolia_stories=[],
                hot_stories=[story("1", "Ocean currents discovery", points=500, comments=200)],
                article_fetcher=lambda *a, **kw: "A substantial science article.",
                classifier=Classifier(), summarizer=FakeSummarizer(),
            )
    assert existing.read_text() == "existing brief"
    assert not (output_dir / "2026-10-12.no-content").exists()
