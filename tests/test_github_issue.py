"""GitHub issue acquisition uses structured bodies, never page chrome."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from urllib.error import URLError

import pytest

from daily_brief.article_fetcher import (
    CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY, ArticleFetchError, fetch_article,
)
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.llm.summarizer import build_summary_material_context, split_page_material
from daily_brief.models import Candidate
from fakes import story
from test_article_fetcher import FakeResponse, http_error, make_jina_payload, make_wayback_payload, resolver_for


URL = "https://github.com/example/compiler/issues/42"
API_URL = "https://api.github.com/repos/example/compiler/issues/42"
BODY = "A field assignment escapes the bound.\n\n```rust\nlet value  = 1;\n```\n\nExpected: reject."


def issue_data(**changes):
    return {
        "number": 42, "url": API_URL, "html_url": URL, "title": "A lifetime check is missing",
        "body": BODY, "created_at": "2026-10-13T10:00:00Z",
        "comments": 7, "comments_url": API_URL + "/comments", **changes,
    }


def response(data=None, **kwargs):
    return FakeResponse(
        json.dumps(issue_data() if data is None else data).encode(),
        content_type="application/json", final_url=API_URL, **kwargs,
    )


@pytest.mark.parametrize("suffix", ["", "/", "?view=1#issuecomment-123"])
@pytest.mark.parametrize("policy", [CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY])
def test_issue_api_body_reaches_summary_with_markdown_intact(suffix, policy):
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return response()

    with retrieval_budget(timeout_seconds=5, clock=lambda: 10) as budget:
        result = fetch_article(URL + suffix, opener=opener, resolver=resolver_for({}), policy=policy)
        assert budget.requests == 1
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == API_URL
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("Authorization") is None
    assert timeout == 5
    assert result.method == "github_issue"
    assert result.extractor == "github_api"
    assert result.attempts == 1
    assert result.retrieved_url == URL
    assert result.source_evidence.published_at == "2026-10-13T10:00:00Z"
    metadata, body = split_page_material(result.text)
    assert metadata == "title: A lifetime check is missing"
    assert body == BODY
    candidate = Candidate(story=replace(story("123", "A compiler issue"), fetched_text=result.text))
    context = build_summary_material_context(candidate)
    assert "web_body" in context.sections
    assert BODY in context.text
    assert "comments_url" not in context.text


@pytest.mark.parametrize("url", [
    "https://github.com/example/compiler/issues",
    "https://github.com/example/compiler/issues/new",
    "https://github.com/example/compiler/pull/42",
    "https://github.com/example/compiler/issues/42/comments",
    "https://github.com.evil.example/example/compiler/issues/42",
    "https://github.com/example%2Fother/compiler/issues/42",
    "https://github.com/example/compiler/issues/0",
    "https://github.com/example/compiler/issues/٤٢",
])
def test_other_github_urls_keep_their_existing_route(url):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return FakeResponse(b"Other page facts.", content_type="text/plain", final_url=url)

    result = fetch_article(url, opener=opener, resolver=resolver_for({}))
    assert result.method == "direct"
    assert requests == [url]


@pytest.mark.parametrize("data", [
    [], {"message": "API error"}, issue_data(number=True), issue_data(number=43),
    issue_data(html_url=URL.replace("example", "other")),
    issue_data(html_url="http://127.0.0.1/issue"),
    issue_data(html_url="https://github.com:bad/example/compiler/issues/42"),
    issue_data(body=[]), issue_data(title=None), issue_data(pull_request={}),
])
def test_invalid_or_wrong_issue_response_is_never_accepted(data):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return response(data)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert caught.value.method == "github_issue"
    assert requests == [API_URL]


@pytest.mark.parametrize("body", [None, "", " \n "])
def test_empty_issue_body_uses_reader_for_original_page(body):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            return response(issue_data(body=body))
        assert request.full_url == "https://r.jina.ai/" + URL
        return FakeResponse(make_jina_payload(content=BODY, url=URL), content_type="application/json")

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert result.method == "jina"
    assert BODY in result.text
    assert result.fallback_reason == "empty_content"
    assert result.attempts == 2
    assert requests == [API_URL, "https://r.jina.ai/" + URL]


def test_classification_empty_body_stays_failed_without_reader():
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=lambda request, timeout: response(issue_data(body=None)),
                      resolver=resolver_for({}), policy=CLASSIFICATION_FETCH_POLICY)
    assert caught.value.error_code == "empty_content"
    assert caught.value.method == "github_issue"
    assert caught.value.fallback_attempted is False


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_issue_transient_failure_retries_api_then_recovers_original_page(failure):
    requests, sleeps = [], []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            if failure == "timeout":
                raise URLError(TimeoutError("timed out"))
            raise http_error(API_URL, 503)
        assert request.full_url == "https://r.jina.ai/" + URL
        return FakeResponse(make_jina_payload(content=BODY, url=URL), content_type="application/json")

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}), sleeper=sleeps.append)
    assert requests == [API_URL, API_URL, "https://r.jina.ai/" + URL]
    assert result.method == "jina"
    assert result.attempts == 3
    assert sleeps == [1]


@pytest.mark.parametrize(("status", "headers", "code"), [
    (401, {}, "http_401"), (403, {}, "http_403"),
    (403, {"X_RateLimit_Remaining": "1"}, "http_403"),
])
def test_issue_permissions_do_not_start_recovery(status, headers, code):
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        raise http_error(API_URL, status, **headers)

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == code
    assert caught.value.method == "github_issue"
    assert requests == [API_URL]


@pytest.mark.parametrize(("limits", "code"), [
    ({"html_max_bytes": 10}, "response_too_large"),
    ({"extracted_max_bytes": 10}, "extracted_content_too_large"),
])
def test_issue_retains_download_and_extraction_limits(limits, code):
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=lambda request, timeout: response(),
                      resolver=resolver_for({}), **limits)
    assert caught.value.error_code == code
    assert caught.value.method == "github_issue"


def test_issue_api_and_reader_share_request_budget():
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        return response(issue_data(body=None))

    with retrieval_budget(max_requests=1):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "retrieval_budget_exhausted"
    assert requests == [API_URL]


def test_issue_api_redirect_to_private_address_is_rejected():
    def opener(request, timeout):
        result = response()
        result.final_url = "http://127.0.0.1/issue"
        return result

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({"127.0.0.1": "127.0.0.1"}))
    assert caught.value.error_code == "unsafe_url"


@pytest.mark.parametrize(("status", "headers"), [
    (403, {"X_RateLimit_Remaining": "0"}),
    (403, {"Retry_After": "60"}),
    (429, {}), (429, {"Retry_After": "1"}), (429, {"Retry_After": "3600"}),
])
@pytest.mark.parametrize("authenticated", [False, True])
@pytest.mark.parametrize("policy", [CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY])
def test_api_rate_limit_stops_api_and_recovers_only_for_summary(
    status, headers, authenticated, policy, monkeypatch,
):
    if authenticated:
        monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token")
    requests, sleeps = [], []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            expected = "Bearer synthetic-token" if authenticated else None
            assert request.get_header("Authorization") == expected
            raise http_error(API_URL, status, **headers)
        assert request.full_url == "https://r.jina.ai/" + URL
        assert request.get_header("Authorization") is None
        return FakeResponse(make_jina_payload(content=BODY, url=URL), content_type="application/json")

    if policy == CLASSIFICATION_FETCH_POLICY:
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}),
                          policy=policy, sleeper=sleeps.append)
        assert caught.value.error_code == "github_api_rate_limited"
        assert caught.value.method == "github_issue"
        assert not caught.value.fallback_attempted
        assert caught.value.attempts == 1
        assert requests == [API_URL]
    else:
        result = fetch_article(URL, opener=opener, resolver=resolver_for({}),
                               policy=policy, sleeper=sleeps.append)
        assert result.method == "jina"
        assert result.fallback_reason == "github_api_rate_limited"
        assert result.attempts == 2
        assert BODY in result.text
        assert requests == [API_URL, "https://r.jina.ai/" + URL]
    assert sleeps == []


def test_rate_limited_api_recovery_respects_shared_budget():
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        raise http_error(API_URL, 429)

    with retrieval_budget(max_requests=1):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "retrieval_budget_exhausted"
    assert requests == [API_URL]



def test_api_limit_then_reader_failure_recovers_archive_without_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token")
    requests = []
    timestamp = "20261012060000"
    replay_url = f"https://web.archive.org/web/{timestamp}id_/{URL}"
    fact = "A field assignment bypasses the compiler lifetime check and allows an invalid reference."

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            raise http_error(API_URL, 403, X_RateLimit_Remaining="0")
        assert request.get_header("Authorization") is None
        if request.full_url == "https://r.jina.ai/" + URL:
            raise http_error(request.full_url, 404)
        if request.full_url.startswith("https://web.archive.org/cdx/"):
            return FakeResponse(
                make_wayback_payload([timestamp, URL, "text/html", "200", "HASH", "512"]),
                content_type="application/json", final_url=request.full_url,
            )
        assert request.full_url == replay_url
        return FakeResponse(f"<html><body><article><p>{fact}</p></article></body></html>".encode(),
                            final_url=replay_url)

    result = fetch_article(
        URL, opener=opener, resolver=resolver_for({}),
        wayback_not_before=datetime(2026, 10, 12, tzinfo=UTC),
        wayback_not_after=datetime(2026, 10, 13, tzinfo=UTC),
    )
    assert result.method == "wayback"
    assert result.fallback_reason == "github_api_rate_limited"
    assert result.material_origin == "archived_copy"
    assert result.attempts == len(requests) == 4
    assert fact in result.text
