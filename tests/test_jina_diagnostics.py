"""Reader network diagnostics retain types without copying private messages."""

import logging
import socket
from urllib.error import URLError

import pytest

from daily_brief.article_fetcher import ArticleFetchError, fetch_article
from daily_brief.article_fetcher.jina import _fetch_jina_reader
from test_article_fetcher import FakeResponse, http_error, resolver_for


URL = "https://example.com/article?gift=test-only-gift"
PRIVATE = "https://user:private-password@example.com/?token=private-key"


@pytest.mark.parametrize(
    ("failure", "exception_type", "reason_type"),
    [
        (TimeoutError(PRIVATE), "TimeoutError", "unknown"),
        (URLError(TimeoutError(PRIVATE)), "URLError", "TimeoutError"),
        (URLError(socket.gaierror(-3, PRIVATE)), "URLError", "gaierror"),
        (URLError(PRIVATE), "URLError", "unknown"),
        (RuntimeError(PRIVATE), "RuntimeError", "unknown"),
        (type("Bad\n" + PRIVATE, (Exception,), {})(PRIVATE), "unknown", "unknown"),
    ],
)
def test_reader_preserves_safe_exception_types(failure, exception_type, reason_type, caplog):
    calls = []

    def opener(request, timeout):
        calls.append(request)
        raise failure

    with caplog.at_level(logging.INFO), pytest.raises(ArticleFetchError) as caught:
        _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))

    assert caught.value.error_code == "request_failed"
    assert caught.value.method == caught.value.extractor == "jina"
    assert caught.value.attempts == len(calls) == 1
    for output in (str(caught.value), caplog.text):
        assert f"exception_type={exception_type} reason_type={reason_type}" in output
        assert PRIVATE not in output
        assert "private-password" not in output
        assert "private-key" not in output
        assert "test-only-gift" not in output


def test_authenticated_timeout_retains_diagnostics_without_retrying_again(monkeypatch, caplog):
    monkeypatch.setenv("JINA_API_KEY", "private-key")
    calls = []

    def opener(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(request.full_url, 401)
        raise URLError(TimeoutError(PRIVATE))

    with caplog.at_level(logging.INFO), pytest.raises(ArticleFetchError) as caught:
        _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))

    assert caught.value.error_code == "request_failed"
    assert caught.value.attempts == len(calls) == 2
    assert "anonymous=http_401; api_key=request_failed" in str(caught.value)
    for output in (str(caught.value), caplog.text):
        assert "exception_type=URLError reason_type=TimeoutError" in output
        assert "private-key" not in output
        assert "private-password" not in output
        assert "test-only-gift" not in output


def test_timeout_diagnostic_survives_wayback_failure():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if request.full_url == URL:
            raise http_error(URL, 403, cf_mitigated="challenge")
        if request.full_url.startswith("https://r.jina.ai/"):
            raise URLError(TimeoutError(PRIVATE))
        return FakeResponse(
            b"[]", content_type="application/json", final_url=request.full_url,
        )

    with pytest.raises(ArticleFetchError) as caught:
        fetch_article(URL, opener=opener, resolver=resolver_for({}))

    assert caught.value.error_code == "wayback_no_capture"
    assert caught.value.method == "wayback"
    assert caught.value.attempts == len(calls) == 3
    assert "jina=Jina Reader request failed: exception_type=URLError reason_type=TimeoutError" in str(caught.value)
    assert "private-key" not in str(caught.value)
    assert "private-password" not in str(caught.value)
