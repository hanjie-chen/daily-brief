"""Deterministic shared retrieval-budget tests; no network or real sleeping."""

from io import BytesIO

import pytest

from daily_brief.article_fetcher.budget import (
    budgeted_opener,
    budgeted_sleep,
    check_budget,
    remaining_seconds,
    retrieval_budget,
    with_retrieval_budget,
)
from daily_brief.article_fetcher.contracts import ArticleFetchError
from daily_brief.recovery.tavily import TavilyFinder


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def assert_exhausted(caught):
    assert caught.value.error_code == "retrieval_budget_exhausted"
    assert str(caught.value) == "Retrieval budget exhausted"


def test_unscoped_wrappers_preserve_original_calls():
    calls = []
    opener = budgeted_opener(lambda *args, **kwargs: calls.append((args, kwargs)))
    opener("request", timeout=15)
    opener("request")
    slept = []
    budgeted_sleep(300, slept.append)
    assert calls == [(("request",), {"timeout": 15}), (("request",), {})]
    assert slept == [300]
    assert remaining_seconds() is None


def test_nested_and_decorated_retrieval_share_count_and_do_not_double_wrap():
    clock = Clock()
    calls = []
    opener = budgeted_opener(lambda *args, **kwargs: calls.append(kwargs["timeout"]))
    assert budgeted_opener(opener) is opener

    @with_retrieval_budget
    def inner():
        opener("request", timeout=15)

    with retrieval_budget(timeout_seconds=10, max_requests=2, clock=clock) as outer:
        inner()
        clock.advance(3)
        with retrieval_budget(timeout_seconds=999, max_requests=999) as nested:
            assert nested is outer
            inner()
        check_budget()  # The final allowed response can still be used.
        with pytest.raises(ArticleFetchError) as caught:
            inner()
        assert_exhausted(caught)
        assert outer.requests == 2
    assert calls == [10, 7]
    assert remaining_seconds() is None


def test_failed_requests_consume_budget_and_new_scope_resets_it():
    clock = Clock()
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        raise OSError("injected private endpoint")

    opener = budgeted_opener(failing)
    with retrieval_budget(max_requests=1, clock=clock):
        with pytest.raises(OSError):
            opener("private endpoint", timeout=15)
        with pytest.raises(ArticleFetchError) as caught:
            opener("private endpoint", timeout=15)
        assert_exhausted(caught)
    with retrieval_budget(max_requests=1, clock=clock):
        with pytest.raises(OSError):
            opener("request", timeout=15)
    assert len(calls) == 2


def test_expiry_preserves_completed_response_but_prevents_next_request():
    clock = Clock()
    response = BytesIO(b"late")
    calls = []

    def slow(*args, **kwargs):
        calls.append(kwargs["timeout"])
        clock.advance(6)
        return response

    opener = budgeted_opener(slow)
    with retrieval_budget(timeout_seconds=5, clock=clock):
        with opener("request", timeout=15) as completed:
            assert completed.read() == b"late"
        assert response.closed  # The caller still owns response cleanup.
        with pytest.raises(ArticleFetchError) as caught:
            opener("request", timeout=15)
        assert_exhausted(caught)
    assert calls == [5]


def test_timeout_clipping_respects_smaller_and_positional_timeouts():
    calls = []
    opener = budgeted_opener(lambda *args, **kwargs: calls.append((args, kwargs)))
    with retrieval_budget(timeout_seconds=10, clock=Clock()):
        opener("request", timeout=2)
        opener("request", None, 30)
        opener("request", timeout=None)
    assert calls == [
        (("request",), {"timeout": 2}),
        (("request", None, 10), {}),
        (("request",), {"timeout": 10}),
    ]


def test_retry_after_wait_is_honored_in_full_or_rejected():
    clock = Clock()
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)

    with retrieval_budget(timeout_seconds=10, clock=clock):
        budgeted_sleep(4, sleep)
        assert remaining_seconds() == 6
        for seconds in (6, 30):
            with pytest.raises(ArticleFetchError) as caught:
                budgeted_sleep(seconds, sleep)
            assert_exhausted(caught)
    assert slept == [4]


def test_injected_sleep_cannot_silently_overrun_deadline():
    clock = Clock()
    with retrieval_budget(timeout_seconds=5, clock=clock):
        with pytest.raises(ArticleFetchError) as caught:
            budgeted_sleep(1, lambda seconds: clock.advance(8))
        assert_exhausted(caught)


def test_tavily_shares_article_transport_budget_and_retains_error_code():
    clock = Clock()
    requests = []

    class FinderError(RuntimeError):
        def __init__(self, message, *, error_code):
            super().__init__(message)
            self.error_code = error_code

    def opener(*args, **kwargs):
        requests.append(kwargs["timeout"])
        return BytesIO(b'{"results": []}')

    finder = TavilyFinder(api_key="test-key", opener=opener)
    with retrieval_budget(timeout_seconds=8, max_requests=1, clock=clock):
        assert finder._search(FinderError, query="title", max_results=3, exact_match=False) == []
        with pytest.raises(FinderError) as caught:
            finder._search(FinderError, query="title", max_results=3, exact_match=False)
        assert caught.value.error_code == "retrieval_budget_exhausted"
        assert "test-key" not in str(caught.value)
    assert requests == [8]


def test_tavily_preserves_completed_search_but_cannot_start_another():
    clock = Clock()

    class SlowResponse(BytesIO):
        def read(self, *args):
            clock.advance(9)
            return super().read(*args)

    class FinderError(RuntimeError):
        def __init__(self, message, *, error_code):
            super().__init__(message)
            self.error_code = error_code

    finder = TavilyFinder(api_key="test-key", opener=lambda *args, **kwargs: SlowResponse(b'{"results": []}'))
    with retrieval_budget(timeout_seconds=8, clock=clock):
        assert finder._search(FinderError, query="title", max_results=3, exact_match=False) == []
        with pytest.raises(FinderError) as caught:
            finder._search(FinderError, query="title", max_results=3, exact_match=False)
        assert caught.value.error_code == "retrieval_budget_exhausted"
