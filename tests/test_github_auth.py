"""Optional GitHub credentials stay on their initial API requests."""

import logging
from email.message import Message

import pytest

from daily_brief.article_fetcher import fetch_article, ArticleFetchError
from daily_brief.article_fetcher.http_safety import _SafeRedirectHandler
from test_article_fetcher import FakeResponse, make_jina_payload, resolver_for
from test_github_issue import URL, API_URL, response, issue_data


TOKEN = "synthetic_github_credential"
README_URL = "https://github.com/example/compiler"
README_API = "https://api.github.com/repos/example/compiler/readme"


def api_response(url):
    if url == API_URL:
        return response()
    assert url == README_API
    return FakeResponse(
        b"# Compiler\n\nA public project.", content_type="application/vnd.github.raw+json",
        final_url=README_API,
    )


@pytest.mark.parametrize("url", [URL, README_URL])
@pytest.mark.parametrize("token", [None, "", "  ", TOKEN, " " + TOKEN + "\n"])
def test_optional_token_authenticates_issue_and_readme(url, token, monkeypatch, caplog):
    if token is not None:
        monkeypatch.setenv("GITHUB_TOKEN", token)
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return api_response(request.full_url)

    with caplog.at_level(logging.INFO):
        result = fetch_article(url, opener=opener, resolver=resolver_for({}))
    assert result.text
    assert len(requests) == 1
    expected = f"Bearer {TOKEN}" if token and token.strip() else None
    assert requests[0].get_header("Authorization") == expected
    assert "Authorization" not in requests[0].headers
    assert TOKEN not in result.text
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("destination", [
    "https://api.github.com/repos/new/compiler/issues/42",
    "https://raw.githubusercontent.com/example/compiler/main/README.md",
    "https://other.example/page", "http://api.github.com/repos/example/compiler/issues/42",
])
@pytest.mark.parametrize("url", [URL, README_URL])
def test_redirects_never_forward_token(destination, url, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

    def opener(request, timeout):
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        redirected = _SafeRedirectHandler(resolver_for({})).redirect_request(
            request, None, 302, "Found", Message(), destination,
        )
        assert redirected.get_header("Authorization") is None
        return api_response(API_URL if url == URL else README_API)

    if url == URL and not destination.startswith("https://api.github.com/"):
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(url, opener=opener, resolver=resolver_for({}))
        assert caught.value.error_code == "invalid_response"
    else:
        fetch_article(url, opener=opener, resolver=resolver_for({}))


def test_issue_recovery_does_not_pass_github_token_to_reader(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    requests = []

    def opener(request, timeout):
        requests.append(request.full_url)
        if request.full_url == API_URL:
            assert request.get_header("Authorization") == f"Bearer {TOKEN}"
            return response(issue_data(body=None))
        assert request.full_url == "https://r.jina.ai/" + URL
        assert request.get_header("Authorization") is None
        return FakeResponse(make_jina_payload(url=URL), content_type="application/json")

    result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
    assert result.method == "jina"
    assert len(requests) == 2


@pytest.mark.parametrize("url", [
    "https://github.com/example/compiler/blob/main/README.md",
    "https://example.com/article",
])
def test_token_is_not_sent_to_raw_files_or_ordinary_pages(url, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)

    def opener(request, timeout):
        assert request.get_header("Authorization") is None
        return FakeResponse(b"Public facts.", content_type="text/plain", final_url=request.full_url)

    assert fetch_article(url, opener=opener, resolver=resolver_for({})).text == "Public facts."


@pytest.mark.parametrize("url", [URL, README_URL])
@pytest.mark.parametrize("token", [TOKEN + "\r\nInjected: value", TOKEN + "\x7f", TOKEN + "中文"])
def test_invalid_token_is_rejected_without_exposing_value(url, token, monkeypatch, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", token)

    def opener(request, timeout):
        pytest.fail("Invalid credentials must be rejected before making requests")

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(url, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == "invalid_configuration"
    assert TOKEN not in str(caught.value)
    assert TOKEN not in caplog.text
