"""Exercise official issue migrations through urllib's real redirect handler."""

import json
from email.message import Message
from io import BytesIO
from urllib.request import BaseHandler, ProxyHandler, build_opener
from urllib.response import addinfourl

import pytest

from daily_brief.article_fetcher import (
    CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY, ArticleFetchError, fetch_article,
)
from daily_brief.article_fetcher.budget import retrieval_budget
from daily_brief.article_fetcher.http_safety import _SafeRedirectHandler
from daily_brief.llm.summarizer import split_page_material
from test_article_fetcher import FakeResponse, resolver_for
from test_github_issue import API_URL, BODY, URL, issue_data


NEW_API_URL = "https://api.github.com/repos/new-owner/new-compiler/issues/71"
NEW_PAGE_URL = "https://github.com/new-owner/new-compiler/issues/71"
NUMERIC_API_URL = "https://api.github.com/repositories/1234/issues/71"


def transport(routes, requests):
    class FakeHTTP(BaseHandler):
        handler_order = 100

        def https_open(self, request):
            requests.append(request)
            assert request.full_url in routes, "Unexpected transport call"
            value = routes[request.full_url]
            headers = Message()
            if isinstance(value, tuple):
                status, location = value
                headers["Location"] = location
                payload = b""
            else:
                status = 200
                headers["Content-Type"] = "application/json"
                payload = json.dumps(value).encode()
            response = addinfourl(BytesIO(payload), headers, request.full_url, status)
            response.msg = "OK" if status == 200 else "Moved"
            return response

        http_open = https_open

    return build_opener(
        ProxyHandler({}), FakeHTTP(), _SafeRedirectHandler(resolver_for({})),
    ).open


@pytest.mark.parametrize("policy", [CLASSIFICATION_FETCH_POLICY, SUMMARY_FETCH_POLICY])
@pytest.mark.parametrize("destinations", [
    ["https://api.github.com/repos/new-owner/new-compiler/issues/42"],
    ["https://api.github.com/repositories/1234/issues/42"],
    [NEW_API_URL],
    [NUMERIC_API_URL],
    [NEW_API_URL, NUMERIC_API_URL],
])
def test_official_rename_or_transfer_preserves_issue_body(destinations, policy, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-token")
    number = int(destinations[-1].rsplit("/", 1)[1])
    canonical_api = NEW_API_URL.rsplit("/", 1)[0] + f"/{number}"
    canonical_page = NEW_PAGE_URL.rsplit("/", 1)[0] + f"/{number}"
    chain = [API_URL, *destinations]
    routes = {start: (301, end) for start, end in zip(chain, chain[1:])}
    routes[chain[-1]] = issue_data(number=number, url=canonical_api, html_url=canonical_page)
    requests = []
    with retrieval_budget(max_requests=1, timeout_seconds=5, clock=lambda: 10) as budget:
        result = fetch_article(URL, opener=transport(routes, requests),
                               resolver=resolver_for({}), policy=policy)
        # urllib redirect hops keep the existing separately bounded behavior.
        assert budget.requests == 1
    assert [request.full_url for request in requests] == chain
    assert requests[0].get_header("Authorization") == "Bearer synthetic-token"
    assert all(request.get_header("Authorization") is None for request in requests[1:])
    assert all(request.timeout == 5 for request in requests)
    assert result.method == "github_issue"
    assert result.retrieved_url == canonical_page
    assert result.material_origin == "original"
    assert result.fallback_reason == ""
    assert result.attempts == 1
    assert split_page_material(result.text)[1] == BODY


@pytest.mark.parametrize("destination", [NEW_API_URL, NUMERIC_API_URL])
@pytest.mark.parametrize("change", [
    {"number": 42}, {"number": True},
    {"url": API_URL}, {"html_url": URL},
    {"url": "https://other.example/issues/71"},
    {"url": "https://api.github.com/repos/new-owner/new-compiler/pulls/71"},
    {"html_url": NEW_PAGE_URL.replace("/issues/", "/pull/")},
    {"pull_request": {}}, {"url": None},
])
def test_migration_still_rejects_inconsistent_identity_and_prs(destination, change):
    data = issue_data(number=71, url=NEW_API_URL, html_url=NEW_PAGE_URL)
    data.update(change)
    requests = []
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=transport({API_URL: (301, destination), destination: data}, requests),
                      resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert caught.value.method == "github_issue"
    assert not caught.value.fallback_attempted
    assert [request.full_url for request in requests] == [API_URL, destination]


@pytest.mark.parametrize("destination", [API_URL, NEW_API_URL])
def test_named_endpoint_cannot_return_a_different_repository(destination):
    requests = []
    # Both body URLs agree, but are not the repository actually requested.
    number = int(destination.rsplit("/", 1)[1])
    data = issue_data(number=number,
                      url=f"https://api.github.com/repos/unrelated/compiler/issues/{number}",
                      html_url=f"https://github.com/unrelated/compiler/issues/{number}")
    routes = {destination: data}
    if destination != API_URL:
        routes[API_URL] = (301, destination)
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=transport(routes, requests), resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert not caught.value.fallback_attempted


@pytest.mark.parametrize("bad_url", [
    "https://other.example/redirect-back-to-api",
    "https://api.github.com.evil.example/repos/owner/repo/issues/71",
    "http://api.github.com/repos/owner/repo/issues/71",
    "https://user@api.github.com/repos/owner/repo/issues/71",
    "https://api.github.com:443/repos/owner/repo/issues/71",
    "https://api.github.com/repos/owner/repo/pulls/71",
    "https://api.github.com/repos/owner/repo/issues/71/comments",
    "https://api.github.com/repositories/0/issues/71",
    "https://api.github.com/repositories/1234/issues/0",
    "https://api.github.com/repositories/1234/issues/71?redirect=elsewhere",
    "https://api.github.com/repositories/1234/issues/71#comment",
])
@pytest.mark.parametrize("after_valid_hop", [False, True])
def test_every_redirect_hop_must_stay_on_a_github_issue_api_endpoint(bad_url, after_valid_hop):
    requests = []
    routes = {API_URL: (301, NEW_API_URL if after_valid_hop else bad_url)}
    if after_valid_hop:
        routes[NEW_API_URL] = (302, bad_url)
    # Even an off-site destination redirecting back to the API is not trusted.
    routes[bad_url] = (301, NUMERIC_API_URL)
    routes[NUMERIC_API_URL] = issue_data(number=71, url=NEW_API_URL, html_url=NEW_PAGE_URL)
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=transport(routes, requests), resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_response"
    assert not caught.value.fallback_attempted
    assert [request.full_url for request in requests] == ([API_URL, NEW_API_URL] if after_valid_hop else [API_URL])


@pytest.mark.parametrize("final_url", [
    "https://other.example/issues/42",
    "http://api.github.com/repos/example/compiler/issues/42",
    "https://api.github.com/repos/example/compiler/pulls/42",
])
def test_final_api_url_is_checked_even_with_an_injected_opener(final_url):
    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, resolver=resolver_for({}), opener=lambda request, timeout: FakeResponse(
            json.dumps(issue_data()).encode(), content_type="application/json", final_url=final_url,
        ))
    assert caught.value.error_code == "invalid_response"


def test_redirect_to_nonpublic_api_address_is_rejected():
    calls = 0

    def resolver(host, port, type):
        nonlocal calls
        if host == "api.github.com":
            calls += 1
        # Initial API resolution is public; re-resolution on redirect is private.
        address = "127.0.0.1" if calls >= 2 else "93.184.216.34"
        return [(2, type, 6, "", (address, port))]

    # A unit-level redirect invocation avoids making any live request.
    def open_response(request, timeout):
        _SafeRedirectHandler(resolver).redirect_request(
            request, None, 301, "Moved", Message(), NEW_API_URL,
        )
        pytest.fail("Private redirect must fail before reaching its destination")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=open_response, resolver=resolver)
    assert caught.value.error_code == "unsafe_url"
