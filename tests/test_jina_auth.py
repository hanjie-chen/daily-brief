"""Deterministic anonymous-first Jina authentication and recovery tests."""
import logging
from urllib.error import HTTPError

import pytest

from daily_brief.article_fetcher import ArticleFetchError, fetch_article
from daily_brief.article_fetcher.http_safety import _SafeRedirectHandler
from daily_brief.article_fetcher.jina import _fetch_jina_reader
from test_article_fetcher import FakeResponse, http_error, make_jina_payload, resolver_for

URL = 'https://example.com/article'
READER = f'https://r.jina.ai/{URL}'
KEY = 'test-only-secret'


def response(payload=None):
    return FakeResponse(payload if payload is not None else make_jina_payload(),
                        content_type='application/json', final_url=READER)


@pytest.fixture(autouse=True)
def api_key(monkeypatch, clear_jina_api_key):
    monkeypatch.setenv('JINA_API_KEY', KEY)


def test_anonymous_success_never_sends_key(caplog):
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        return response()
    with caplog.at_level(logging.INFO):
        result = _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))
    assert result.attempts == 1
    assert len(calls) == 1
    assert calls[0].get_header('Authorization') is None
    assert 'auth=anonymous status=success' in caplog.text


@pytest.mark.parametrize('status', [401, 429])
def test_authenticated_retry_and_redirect_safety(status, caplog):
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(READER, status)
        assert kwargs['timeout'] == 7
        return response()
    with caplog.at_level(logging.INFO):
        result = _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}),
                                    timeout_seconds=7)
    assert result.attempts == 2
    assert calls[0].get_header('Authorization') is None
    assert calls[1].get_header('Authorization') == f'Bearer {KEY}'
    redirect = _SafeRedirectHandler(resolver_for({})).redirect_request(
        calls[1], None, 302, 'Found', {}, 'https://other.example/article')
    assert redirect.get_header('Authorization') is None
    assert 'auth=api_key status=success' in caplog.text
    assert KEY not in caplog.text


@pytest.mark.parametrize('key', ['', '  '])
def test_missing_key_does_not_retry(monkeypatch, key):
    monkeypatch.setenv('JINA_API_KEY', key)
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        raise http_error(READER, 401)
    with pytest.raises(ArticleFetchError) as caught:
        _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))
    assert caught.value.error_code == 'http_401'
    assert caught.value.attempts == 1
    assert len(calls) == 1


@pytest.mark.parametrize('failure', [403, 500, 'timeout', 'json', 'empty', 'origin401', 'origin429', 'challenge'])
def test_content_and_other_failures_do_not_spend_key(failure):
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        if isinstance(failure, int):
            raise http_error(READER, failure)
        if failure == 'timeout':
            raise TimeoutError('timeout')
        payload = {
            'json': b'not json', 'empty': make_jina_payload(''),
            'origin401': make_jina_payload(http_status=401),
            'origin429': make_jina_payload(http_status=429),
            'challenge': make_jina_payload('Vercel Security Checkpoint\nWe are verifying your browser'),
        }[failure]
        return response(payload)
    with pytest.raises(ArticleFetchError):
        _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))
    assert len(calls) == 1
    assert calls[0].get_header('Authorization') is None


@pytest.mark.parametrize('second_failure', ['http', 'exception', 'invalid_body'])
def test_second_failure_is_terminal_and_sanitized(second_failure, caplog):
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(READER, 401)
        if second_failure == 'http':
            raise HTTPError(READER, 401, KEY, {}, None)
        if second_failure == 'exception':
            raise RuntimeError(KEY)
        return response(b'bad json')
    with caplog.at_level(logging.INFO), pytest.raises(ArticleFetchError) as caught:
        _fetch_jina_reader(URL, opener=opener, resolver=resolver_for({}))
    assert len(calls) == 2
    assert caught.value.attempts == 2
    assert 'anonymous=http_401' in str(caught.value)
    assert KEY not in str(caught.value) + caplog.text


@pytest.mark.parametrize('succeeds', [True, False])
def test_recovery_counts_authenticated_request_and_continues_wayback(succeeds):
    calls = []
    def opener(request, **kwargs):
        calls.append(request)
        if len(calls) == 1:
            raise http_error(URL, 403, cf_mitigated='challenge')
        if len(calls) == 2:
            raise http_error(READER, 401)
        if len(calls) == 3:
            if succeeds:
                return response()
            raise http_error(READER, 429)
        assert 'archive.org' in request.full_url
        assert request.get_header('Authorization') is None
        raise http_error(request.full_url, 503)
    if succeeds:
        result = fetch_article(URL, opener=opener, resolver=resolver_for({}))
        assert result.method == 'jina'
        assert result.attempts == 3
    else:
        with pytest.raises(ArticleFetchError) as caught:
            fetch_article(URL, opener=opener, resolver=resolver_for({}))
        assert caught.value.method == 'wayback'
        assert caught.value.attempts == 4
        assert 'api_key=http_429' in str(caught.value)
