import io
from dataclasses import replace
import json
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.llm.openrouter_backend import (
    CHAT_COMPLETIONS_URL, OpenRouterAPIError, OpenRouterBackend,
    OpenRouterConfigurationError, OpenRouterResponseError,
)
from daily_brief.llm.summarizer import InsufficientSummaryMaterial
from daily_brief.models import Candidate
from fakes import story


class Response(io.BytesIO):
    def geturl(self):
        return CHAT_COMPLETIONS_URL


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return Response(json.dumps(response).encode())


def completion(output, **overrides):
    return {
        'model': 'openai/gpt-6-luna', 'provider': 'OpenAI',
        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'total_tokens': 130,
                  'cost': .00004, 'completion_tokens_details': {'reasoning_tokens': 4},
                  'prompt_tokens_details': {'cached_tokens': 20, 'cache_write_tokens': 80}},
        **overrides,
    }


def material():
    item = Candidate(story('1', 'Useful tool'))
    item.story = replace(item.story, fetched_text='A tool for local backups.')
    item.summary_input_mode = 'materials'
    return item


def output(**overrides):
    return {'status': 'sufficient', 'summary': '一个本地备份工具。',
            'summary_sources': ['web_body'], 'reason': '', **overrides}


def backend(opener, **kwargs):
    return OpenRouterBackend(api_key='secret-key', opener=opener,
                             min_request_interval_seconds=0, sleeper=lambda _: None, **kwargs)


def test_classifier_contract_and_no_thinking():
    item = material()
    transport = Opener(completion({'decisions': [{'id': '1', 'label': 'core_non_ai'}]}))
    client = backend(transport)
    assert client.classify([]) == {}
    assert client.classify([item]) == {'1': 'core_non_ai'}
    request = transport.requests[0]
    payload = json.loads(request.data)
    assert request.full_url == CHAT_COMPLETIONS_URL
    assert request.get_header('Authorization') == 'Bearer secret-key'
    assert payload['model'] == 'qwen/qwen3.8-flash'
    assert payload['reasoning'] == {'enabled': False}
    assert payload['response_format']['json_schema']['strict'] is True
    assert payload['provider']['require_parameters'] is True
    assert 'models' not in payload
    assert 'secret-key' not in request.data.decode()


def test_summary_records_usage_cost_and_sources():
    item = material()
    transport = Opener(completion(output()))
    client = backend(transport)
    assert client.summarize(item) == '一个本地备份工具。'
    payload = json.loads(transport.requests[0].data)
    assert payload['reasoning'] == {'effort': 'medium', 'exclude': True}
    assert payload['max_tokens'] == 8192
    assert item.summary_sources_used == ['web_body']
    assert client.last_summary_usage['thought_tokens'] == 4
    assert client.last_summary_usage['cache_write_tokens'] == 80
    assert client.last_summary_usage['cost_usd'] == .00004
    assert client.last_summary_provider == 'OpenAI'
    assert client.last_summary_model == 'openai/gpt-6-luna'
    assert client.last_summary_attempts == 1
    assert client.accounted_cost_usd == pytest.approx(.00004)


def test_roundup_uses_comments_schema_and_formats_entries():
    item = material()
    item.summary_input_mode = 'legacy'
    item.content_kind = 'community_roundup'
    item.summary_basis = 'hn_comments'
    item.discussion_text = 'Readers discuss two books and explain what they cover.'
    result = {'status': 'sufficient', 'introduction': '近期读书推荐。', 'reason': '',
              'entries': [{'name': 'Book A', 'description': '解释系统思维。'},
                          {'name': 'Book B', 'description': '介绍设计决策。'}]}
    transport = Opener(completion(result))
    assert '- Book A：解释系统思维。' in backend(transport).summarize(item)
    payload = json.loads(transport.requests[0].data)
    assert payload['reasoning'] == {'effort': 'medium', 'exclude': True}
    schema = payload['response_format']['json_schema']['schema']
    assert schema['required'] == ['status', 'introduction', 'entries', 'reason']


def test_legacy_summary_still_supported():
    item = material()
    item.summary_input_mode = 'legacy'
    item.summary_basis = 'article_text'
    response = {'status': 'sufficient', 'summary': '本地备份工具。', 'reason': ''}
    assert backend(Opener(completion(response))).summarize(item) == '本地备份工具。'


def test_insufficient_is_not_retried_and_cost_is_retained():
    transport = Opener(completion(output(status='insufficient', summary='', summary_sources=[], reason='No usable evidence.')))
    client = backend(transport)
    with pytest.raises(InsufficientSummaryMaterial):
        client.summarize(material())
    assert len(transport.requests) == 1
    assert client.accounted_cost_usd > 0


@pytest.mark.parametrize('status', [401, 402, 403, 400, 302])
def test_permanent_http_error_not_retried_or_exposed(status):
    exc = HTTPError(CHAT_COMPLETIONS_URL, status, 'secret-key', {}, io.BytesIO(b'secret-key'))
    transport = Opener(exc)
    with pytest.raises(OpenRouterAPIError) as caught:
        backend(transport).summarize(material())
    assert caught.value.http_status == status
    assert 'secret-key' not in str(caught.value)
    assert len(transport.requests) == 1


@pytest.mark.parametrize('first', [
    HTTPError(CHAT_COMPLETIONS_URL, 503, 'down', {}, io.BytesIO(b'bad')),
    URLError('secret-key'), TimeoutError('secret-key'),
    {'error': {'code': 429, 'message': 'secret-key'}},
])
def test_transient_errors_retry_and_keep_unknown_cost_reservation(first):
    transport = Opener(first, completion(output()))
    client = backend(transport)
    assert client.summarize(material())
    assert client.last_summary_attempts == 2
    assert client.accounted_cost_usd > .00004
    assert 'secret-key' not in json.dumps(client.request_records)


@pytest.mark.parametrize('response', [
    [], {'choices': []}, completion(output(), choices=[{'finish_reason': 'length'}]),
    completion(output(), choices=[{'finish_reason': {}, 'message': {}}]),
    completion(output(), choices=[{'finish_reason': 'stop', 'message': {'content': '[]'}}]),
    completion(output(), choices=[{'finish_reason': 'stop', 'message': {'content': 'broken'}}]),
    completion(output(summary_sources=['missing_source'])),
])
def test_bad_output_is_not_retried(response):
    transport = Opener(response)
    with pytest.raises(OpenRouterResponseError):
        backend(transport).summarize(material())
    assert len(transport.requests) == 1


def test_budget_blocks_before_request_and_does_not_reset_between_calls():
    transport = Opener(completion(output(), usage={'cost': .009}))
    client = backend(transport, max_run_cost_usd=.01)
    client.summarize(material())
    with pytest.raises(OpenRouterAPIError, match='budget'):
        client.summarize(material())
    assert len(transport.requests) == 1


def test_failed_attempts_stop_at_budget_not_retry_count():
    transport = Opener(TimeoutError())
    client = backend(transport, max_run_cost_usd=.006)
    with pytest.raises(OpenRouterAPIError, match='budget'):
        client.summarize(material())
    assert len(transport.requests) == 1
    assert client.accounted_cost_usd > 0


@pytest.mark.parametrize('kwargs', [
    {'max_run_cost_usd': float('nan')}, {'max_retries': 1.5}, {'timeout_seconds': 0},
    {'summarizer_model': 'other/model'}, {'api_key': 'x\ny'},
    {'classifier_min_request_interval_seconds': -1}, {'max_run_cost_usd': True},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(OpenRouterConfigurationError):
        OpenRouterBackend(**{'api_key': 'test', **kwargs})


def test_environment_parses_budget_and_retry_settings():
    client = OpenRouterBackend.from_environment({'OPENROUTER_API_KEY': 'test',
        'DAILY_BRIEF_OPENROUTER_MAX_RUN_COST_USD': '0.15',
        'DAILY_BRIEF_OPENROUTER_MAX_RETRIES': '1'})
    assert client.max_run_cost_usd == .15
    assert client.max_retries == 1


def test_retry_after_is_bounded_and_pacing_uses_injected_clock():
    waits = []
    now = [0.0]
    def sleep(seconds):
        waits.append(seconds)
        now[0] += seconds
    error = HTTPError(CHAT_COMPLETIONS_URL, 429, 'limited', {'Retry-After': '99999'}, io.BytesIO())
    client = OpenRouterBackend(api_key='test', opener=Opener(error, completion(output())),
                               sleeper=sleep, clock=lambda: now[0])
    client.summarize(material())
    assert waits == [60]


def test_response_size_is_bounded_without_logging_body():
    class LargeResponse(Response):
        def read(self, amount=-1):
            assert amount == 256 * 1024 + 1
            return b'x' * amount
    transport_calls = []
    def opener(request, timeout):
        transport_calls.append(request)
        return LargeResponse()
    with pytest.raises(OpenRouterResponseError, match='size limit'):
        backend(opener).summarize(material())
    assert len(transport_calls) == 1


def test_default_transport_refuses_redirect_before_forwarding_auth():
    from daily_brief.llm.openrouter_backend import _NoRedirect
    from urllib.request import Request
    request = Request(CHAT_COMPLETIONS_URL, headers={'Authorization': 'Bearer test'})
    assert _NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://example.com') is None


def test_summary_diagnostics_reset_after_previous_success():
    client = backend(Opener(completion(output()), HTTPError(CHAT_COMPLETIONS_URL, 403, 'denied', {}, io.BytesIO())))
    client.summarize(material())
    with pytest.raises(OpenRouterAPIError):
        client.summarize(material())
    assert client.last_summary_usage == {}
    assert client.last_summary_provider == ''
    assert client.last_summary_attempts == 1
