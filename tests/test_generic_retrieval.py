"""Fallback behavior without vendor-specific challenge headers."""

from datetime import UTC, datetime
from urllib.error import URLError

import pytest

from daily_brief.article_fetcher import (
    ArticleFetchError, ArticleFetchPolicy, CLASSIFICATION_FETCH_POLICY, fetch_article,
)
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.article_fetcher.fetch import _http_retry_delay
from test_article_fetcher import (
    FakeResponse, http_error, make_jina_payload, make_wayback_payload, resolver_for,
)

SOURCE = "https://example.com/article"
READER = "https://r.jina.ai/" + SOURCE


@pytest.mark.parametrize("status", [403, 405, 418, 499, 408, 500, 502, 503, 504, 511])
def test_http_failure_uses_reader_without_challenge_markers(status):
    calls, sleeps = [], []

    def open_response(request, timeout):
        calls.append(request.full_url)
        if request.full_url == SOURCE:
            raise http_error(SOURCE, status, server="AkamaiGHost")
        assert request.full_url == READER
        return FakeResponse(make_jina_payload("Recovered article."),
                            content_type="application/json", final_url=READER)

    result = fetch_article(SOURCE, opener=open_response, resolver=resolver_for({}),
                           sleeper=sleeps.append)
    direct_attempts = 2 if status == 408 or status >= 500 else 1
    assert calls == [SOURCE] * direct_attempts + [READER]
    assert result.method == "jina"
    assert result.fallback_reason == f"http_{status}"
    assert result.attempts == direct_attempts + 1
    assert result.text == "Recovered article."


@pytest.mark.parametrize("status", [400, 401, 407, 422, 451])
def test_terminal_http_errors_do_not_enter_backup_transports(status):
    calls = []

    def deny(request, timeout):
        calls.append(request.full_url)
        raise http_error(SOURCE, status)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=deny, resolver=resolver_for({}))
    assert calls == [SOURCE]
    assert caught.value.error_code == f"http_{status}"
    assert not caught.value.fallback_attempted


@pytest.mark.parametrize("status", [403, 404, 410, 408, 429, 503])
def test_classification_stays_one_direct_request(status):
    calls, sleeps = [], []

    def deny(request, timeout):
        calls.append(request.full_url)
        raise http_error(SOURCE, status)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=deny, resolver=resolver_for({}),
                      sleeper=sleeps.append, policy=CLASSIFICATION_FETCH_POLICY)
    assert calls == [SOURCE]
    assert sleeps == []
    assert caught.value.error_code == f"http_{status}"
    assert not caught.value.fallback_attempted


@pytest.mark.parametrize("status", [404, 410])
def test_missing_page_goes_to_archive_without_reader(status):
    calls = []

    def respond(request, timeout):
        calls.append(request.full_url)
        if request.full_url == SOURCE:
            raise http_error(SOURCE, status)
        assert request.full_url.startswith("https://web.archive.org/cdx/search/cdx?")
        return FakeResponse(make_wayback_payload(), content_type="application/json",
                            final_url=request.full_url)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=respond, resolver=resolver_for({}),
                      policy=ArticleFetchPolicy(jina_enabled=False))
    assert len(calls) == 2
    assert caught.value.error_code == "wayback_no_capture"
    assert caught.value.fallback_reason == f"http_{status}"
    assert caught.value.fallback_attempted
    assert caught.value.attempts == 2


@pytest.mark.parametrize("failure", [URLError("connection reset"), ConnectionResetError(),
                                     ConnectionRefusedError()])
def test_connection_failure_retries_then_uses_reader(failure):
    calls, sleeps = [], []

    def respond(request, timeout):
        calls.append(request.full_url)
        if request.full_url == SOURCE:
            raise failure
        return FakeResponse(make_jina_payload(), content_type="application/json",
                            final_url=READER)

    result = fetch_article(SOURCE, opener=respond, resolver=resolver_for({}), sleeper=sleeps.append)
    assert calls == [SOURCE, SOURCE, READER]
    assert sleeps == [1]
    assert result.fallback_reason == "request_failed"


@pytest.mark.parametrize("headers", [{}, {"x_vercel_mitigated": "challenge"}])
def test_retry_after_on_429_waits_then_stops_without_reader(headers):
    calls, sleeps = [], []

    def deny(request, timeout):
        calls.append(request.full_url)
        raise http_error(SOURCE, 429, retry_after="3", **headers)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=deny, resolver=resolver_for({}), sleeper=sleeps.append)
    assert calls == [SOURCE, SOURCE]
    assert sleeps == [3]
    assert caught.value.error_code == "http_429"


@pytest.mark.parametrize("status", [429, 503])
def test_long_retry_after_defers_source_instead_of_shortening_wait(status):
    calls, sleeps = [], []

    def deny(request, timeout):
        calls.append(request.full_url)
        raise http_error(SOURCE, status, retry_after="3600")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=deny, resolver=resolver_for({}), sleeper=sleeps.append)
    assert calls == [SOURCE]
    assert sleeps == []
    assert caught.value.error_code == f"http_{status}"


def test_retry_after_http_date_and_invalid_values():
    now = datetime(2026, 10, 11, tzinfo=UTC)
    for value, expected in [
        ("Sun, 11 Oct 2026 00:00:07 GMT", 7),
        ("Sun, 11 Oct 2026 00:01:00 GMT", None),
        ("Sun, 11 Oct 2026 00:00:00 GMT", 0),
        ("not a date", 1), ("-8", 1), ("9" * 100, None),
    ]:
        assert _http_retry_delay(http_error(SOURCE, 429, retry_after=value), now=now) == expected


def test_shared_request_budget_stops_before_archive():
    calls = []

    def deny(request, timeout):
        calls.append(request.full_url)
        raise http_error(request.full_url, 403)

    with retrieval_budget(max_requests=2):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(SOURCE, opener=deny, resolver=resolver_for({}))
    assert calls == [SOURCE, READER]
    assert caught.value.error_code == "retrieval_budget_exhausted"


def test_reader_content_checks_still_apply_after_generic_403():
    calls = []

    def respond(request, timeout):
        calls.append(request.full_url)
        if request.full_url == SOURCE:
            raise http_error(SOURCE, 403)
        return FakeResponse(make_jina_payload(http_status=403),
                            content_type="application/json", final_url=READER)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(SOURCE, opener=respond, resolver=resolver_for({}),
                      policy=ArticleFetchPolicy(wayback_enabled=False))
    assert calls == [SOURCE, READER]
    assert caught.value.error_code == "jina_origin_status"
    assert caught.value.fallback_reason == "http_403"


def test_github_candidate_shares_request_budget_and_timeout():
    calls = []

    def respond(request, timeout):
        calls.append((request.full_url, timeout))
        return FakeResponse(b"Repository documentation.", content_type="text/plain",
                            final_url=request.full_url)

    with retrieval_budget(max_requests=1, timeout_seconds=4, clock=lambda: 0):
        result = fetch_article("https://github.com/example/project", opener=respond,
                               resolver=resolver_for({}))
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article("https://github.com/example/second", opener=respond,
                          resolver=resolver_for({}))
    assert result.method == "github_readme"
    assert calls == [("https://api.github.com/repos/example/project/readme", 4)]
    assert caught.value.error_code == "retrieval_budget_exhausted"
