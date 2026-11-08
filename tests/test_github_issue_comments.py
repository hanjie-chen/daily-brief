"""Issue updates remain bounded, attributed, and tied to the requested issue."""

import json
import re
from urllib.error import URLError

import pytest

from daily_brief.article_fetcher import (
    fetch_article,
)
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.article_fetcher.http_safety import _SafeRedirectHandler
from test_article_fetcher import FakeResponse, http_error, resolver_for
from test_github_issue import API_URL, BODY, URL, issue_data, response


def comment(identifier, *, api=API_URL, page=URL, **changes):
    repository = api.rsplit("/issues/", 1)[0]
    return {
        "id": identifier,
        "url": f"{repository}/issues/comments/{identifier}",
        "issue_url": api,
        "html_url": f"{page}#issuecomment-{identifier}",
        "body": f"Update body {identifier}.",
        "user": {"login": "reporter"},
        "author_association": "MEMBER",
        "created_at": "2026-11-06T10:00:00Z",
        "updated_at": "2026-11-07T11:00:00Z",
        **changes,
    }


def page_url(page, api=API_URL):
    return f"{api}/comments?per_page=30&page={page}"


def json_response(data, url):
    return FakeResponse(json.dumps(data).encode(), content_type="application/json", final_url=url)


def opener_for(routes, requests):
    def opener(request, timeout):
        requests.append(request)
        value = routes[request.full_url]
        if isinstance(value, Exception):
            raise value
        return value
    return opener


def included_ids(text):
    return [int(value) for value in re.findall(r"^Comment (\d+)", text, re.MULTILINE)]


@pytest.mark.parametrize("count", [1, 30, 31, 60, 91])
def test_recent_comments_use_at_most_two_tail_pages(count):
    last = (count + 29) // 30
    requests = []
    routes = {API_URL: response(issue_data(comments=count, comments_url="http://127.0.0.1/private"))}
    expected_pages = [last]
    if count % 30 and last > 1:
        expected_pages.append(last - 1)
    for page in expected_pages:
        ids = range((page - 1) * 30 + 1, min(page * 30, count) + 1)
        routes[page_url(page)] = json_response([comment(i) for i in ids], page_url(page))
    result = fetch_article(URL, opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert [r.full_url for r in requests] == [API_URL, *[page_url(p) for p in expected_pages]]
    assert included_ids(result.text) == list(range(max(1, count - 29), count + 1))
    assert f"Reported comment count: {count};" in result.text
    assert f"Included comment count: {min(count, 30)}." in result.text
    assert BODY in result.text
    assert "reporter; repository association: MEMBER" in result.text
    assert "2026-11-06T10:00:00+00:00" in result.text
    assert "2026-11-07T11:00:00+00:00" in result.text
    assert "not proof of employment" in result.text
    assert result.source_evidence.published_at == "2026-10-13T10:00:00Z"


@pytest.mark.parametrize("anchor", [1, 60])
def test_linked_comment_is_fetched_first_and_added_without_duplication(anchor):
    target_url = API_URL.rsplit("/issues/", 1)[0] + f"/issues/comments/{anchor}"
    requests = []
    routes = {
        API_URL: response(issue_data(comments=60)),
        target_url: json_response(comment(anchor), target_url),
        page_url(2): json_response([comment(i) for i in range(31, 61)], page_url(2)),
    }
    result = fetch_article(URL + f"#issuecomment-{anchor}", opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert [r.full_url for r in requests] == [API_URL, target_url, page_url(2)]
    expected = ([1] if anchor == 1 else []) + list(range(31, 61))
    assert included_ids(result.text) == expected
    assert f"Included comment count: {len(expected)}." in result.text
    assert f"Comment {anchor} (linked target)" in result.text


@pytest.mark.parametrize("change", [
    {"id": True}, {"id": 2}, {"issue_url": API_URL + "0"},
    {"url": "https://api.github.com/repos/other/compiler/issues/comments/1"},
    {"html_url": URL + "#issuecomment-2"}, {"body": None},
])
def test_invalid_target_comment_does_not_suppress_recent_comments(change):
    target_url = API_URL.rsplit("/issues/", 1)[0] + "/issues/comments/1"
    requests = []
    routes = {
        API_URL: response(issue_data(comments=1)),
        target_url: json_response(comment(1, **change), target_url),
        page_url(1): json_response([comment(3)], page_url(1)),
    }
    result = fetch_article(URL + "#issuecomment-1", opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert BODY in result.text
    assert included_ids(result.text) == [3]
    assert [r.full_url for r in requests] == [API_URL, target_url, page_url(1)]
    assert "retrieval: partial" in result.text


@pytest.mark.parametrize("body", [BODY, ""])
@pytest.mark.parametrize("failure", ["timeout", "500", "403", "json"])
def test_target_failure_still_acquires_recent_comments(body, failure):
    target_url = API_URL.rsplit("/issues/", 1)[0] + "/issues/comments/1"
    failures = {
        "timeout": URLError(TimeoutError("timed out")),
        "500": http_error(target_url, 500),
        "403": http_error(target_url, 403),
        "json": FakeResponse(b"{", content_type="application/json", final_url=target_url),
    }
    routes = {
        API_URL: response(issue_data(body=body, comments=1)),
        target_url: failures[failure],
        page_url(1): json_response([comment(2)], page_url(1)),
    }
    requests = []
    result = fetch_article(URL + "#issuecomment-1", opener=opener_for(routes, requests),
                           resolver=resolver_for({}))
    assert [r.full_url for r in requests] == [API_URL, target_url, page_url(1)]
    assert included_ids(result.text) == [2]
    assert "retrieval: partial" in result.text
    assert "Included comment count: 1." in result.text
    assert result.method == "github_issue"


@pytest.mark.parametrize("failure", ["429", "quota", "retry_after", "budget"])
def test_target_global_limit_stops_recent_comment_requests(failure):
    from daily_brief.article_fetcher import ArticleFetchError

    target_url = API_URL.rsplit("/issues/", 1)[0] + "/issues/comments/1"
    failures = {
        "429": http_error(target_url, 429),
        "quota": http_error(target_url, 403, X_RateLimit_Remaining="0"),
        "retry_after": http_error(target_url, 403, Retry_After="60"),
        "budget": ArticleFetchError("exhausted", error_code="retrieval_budget_exhausted"),
    }
    requests = []
    routes = {API_URL: response(issue_data(comments=1)), target_url: failures[failure]}
    result = fetch_article(URL + "#issuecomment-1", opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert [r.full_url for r in requests] == [API_URL, target_url]
    assert BODY in result.text
    assert not included_ids(result.text)


def test_comments_can_supply_evidence_when_initial_body_is_empty():
    routes = {
        API_URL: response(issue_data(body=None, comments=1)),
        page_url(1): json_response([comment(1)], page_url(1)),
    }
    result = fetch_article(URL, opener=opener_for(routes, []), resolver=resolver_for({}))
    assert result.method == "github_issue"
    assert "Update body 1." in result.text
    assert "Empty initial body" in result.text


@pytest.mark.parametrize("limit", [1, 2])
def test_exhausted_budget_keeps_initial_body_and_completed_comment_page(limit):
    requests = []
    routes = {
        API_URL: response(issue_data(comments=31)),
        page_url(2): json_response([comment(31)], page_url(2)),
    }
    with retrieval_budget(max_requests=limit):
        result = fetch_article(URL, opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert BODY in result.text
    assert len(requests) == limit
    assert included_ids(result.text) == ([31] if limit == 2 else [])
    if limit == 2:
        assert "retrieval: partial" in result.text


def test_comment_page_completed_after_deadline_is_preserved():
    now = [0]
    def opener(request, timeout):
        if request.full_url == API_URL:
            return response(issue_data(comments=1))
        assert request.full_url == page_url(1)
        now[0] = 10
        return json_response([comment(1)], page_url(1))
    with retrieval_budget(timeout_seconds=5, clock=lambda: now[0]):
        result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert included_ids(result.text) == [1]


@pytest.mark.parametrize("failure", ["429", "timeout", "json", "wrong_type", "wrong_url"])
def test_optional_comment_failure_keeps_body_without_issue_retry(failure):
    requests = []
    failures = {
        "429": http_error(page_url(1), 429),
        "timeout": URLError(TimeoutError("timed out")),
        "json": FakeResponse(b"{", content_type="application/json", final_url=page_url(1)),
        "wrong_type": json_response({}, page_url(1)),
        "wrong_url": json_response([comment(1)], page_url(1).replace("api.github.com", "example.com")),
    }
    routes = {API_URL: response(issue_data(comments=1)), page_url(1): failures[failure]}
    result = fetch_article(URL, opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert BODY in result.text
    assert not included_ids(result.text)
    assert [r.full_url for r in requests] == [API_URL, page_url(1)]
    assert result.method == "github_issue"
    assert result.fallback_reason == ""


def test_linked_comment_gets_character_budget_before_recent_comments():
    target_url = API_URL.rsplit("/issues/", 1)[0] + "/issues/comments/1"
    routes = {
        API_URL: response(issue_data(comments=30)),
        target_url: json_response(comment(1, body="TARGET " + "甲" * 9000), target_url),
        page_url(1): json_response([comment(i, body="乙" * 9000) for i in range(31, 61)], page_url(1)),
    }
    result = fetch_article(URL + "#issuecomment-1", opener=opener_for(routes, []), resolver=resolver_for({}))
    comments_text = result.text.split("GitHub issue comments", 1)[1]
    assert len("GitHub issue comments" + comments_text) <= 24_000
    assert "TARGET " in comments_text
    assert "甲" * 6001 not in comments_text
    assert "乙" * 6001 not in comments_text
    assert "[Comment truncated]" in comments_text
    assert included_ids(result.text)[0] == 1
    assert len(included_ids(result.text)) < 30
    assert f"Included comment count: {len(included_ids(result.text))}." in comments_text


def test_tight_utf8_extraction_limit_retains_body():
    routes = {API_URL: response(issue_data(body="界" * 100, comments=30))}
    requests = []
    result = fetch_article(URL, opener=opener_for(routes, requests), resolver=resolver_for({}), extracted_max_bytes=600)
    assert "界" * 100 in result.text
    assert len(result.text.encode("utf-8")) <= 600
    assert [r.full_url for r in requests] == [API_URL]


@pytest.mark.parametrize("destination", [
    "https://example.com/comments", "http://api.github.com/repos/example/compiler/issues/42/comments",
    API_URL + "/comments?per_page=30&page=2",
])
def test_comment_redirect_is_rejected_before_credential_forwarding(destination, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-comment-token")
    requests = []
    def opener(request, timeout):
        requests.append(request)
        assert request.get_header("Authorization") == "Bearer synthetic-comment-token"
        if request.full_url == API_URL:
            return response(issue_data(comments=1))
        _SafeRedirectHandler(resolver_for({})).redirect_request(request, None, 302, "Found", {}, destination)
        pytest.fail("Unsafe comment redirect was accepted")
    result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert BODY in result.text
    assert len(requests) == 2
    assert not included_ids(result.text)


def test_deleted_linked_comment_still_reads_recent_updates():
    target_url = API_URL.rsplit("/issues/", 1)[0] + "/issues/comments/1"
    routes = {
        API_URL: response(issue_data(comments=1)),
        target_url: http_error(target_url, 404),
        page_url(1): json_response([comment(2)], page_url(1)),
    }
    result = fetch_article(URL + "#issuecomment-1", opener=opener_for(routes, []), resolver=resolver_for({}))
    assert included_ids(result.text) == [2]
    assert "retrieval: partial" in result.text


@pytest.mark.parametrize("numeric", [False, True])
def test_migrated_issue_fetches_comments_from_validated_canonical_repository(numeric, monkeypatch):
    from test_github_issue_redirects import transport

    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-comment-token")
    new_api = "https://api.github.com/repos/new-owner/new-repo/issues/71"
    new_page = "https://github.com/new-owner/new-repo/issues/71"
    destination = "https://api.github.com/repositories/1234/issues/71" if numeric else new_api
    routes = {
        API_URL: (301, destination),
        destination: issue_data(number=71, url=new_api, html_url=new_page, comments=1),
        page_url(1, new_api): [comment(1, api=new_api, page=new_page)],
    }
    requests = []
    result = fetch_article(URL, opener=transport(routes, requests), resolver=resolver_for({}))
    assert result.retrieved_url == new_page
    assert included_ids(result.text) == [1]
    assert requests[-1].full_url == page_url(1, new_api)
    assert all(r.get_header("Authorization") == "Bearer synthetic-comment-token" for r in requests)


def test_empty_initial_body_and_empty_comment_keep_existing_recovery():
    from test_article_fetcher import make_jina_payload

    requests = []
    reader = "https://r.jina.ai/" + URL
    routes = {
        API_URL: response(issue_data(body=None, comments=1)),
        page_url(1): json_response([comment(1, body="  ")], page_url(1)),
        reader: FakeResponse(make_jina_payload(content=BODY, url=URL), content_type="application/json"),
    }
    result = fetch_article(URL, opener=opener_for(routes, requests), resolver=resolver_for({}))
    assert result.method == "jina"
    assert result.fallback_reason == "empty_content"
    assert [r.full_url for r in requests] == [API_URL, page_url(1), reader]
