"""Offline contract and wire tests for same-day information comparison."""
from dataclasses import replace
import io
import json

import pytest

from daily_brief.llm import ContentDecision, ensure_content_decisions
from daily_brief.llm.content_deduplication import (
    CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION,
    build_content_deduplication_prompt, validate_content_decisions,
)
from daily_brief.llm.openrouter_backend import OpenRouterBackend, OpenRouterResponseError
from daily_brief.llm.gemini_backend import GeminiBackend, GeminiResponseError
from daily_brief.models import Candidate
from fakes import story


def candidates():
    return [Candidate(story(str(i), title), summary=summary) for i, title, summary in (
        (1, 'PHP security advisory', '修复两个安全漏洞，需要升级。'),
        (2, 'PHP maintenance release', '修复同样两个漏洞，并提醒支持期限。'),
        (5, 'PHP foundation investigation', '解释修复延迟的原因和安全流程调整。'),
    )]


def output():
    return {'decisions': [
        {'item_id': '1', 'duplicate_of': None, 'reason': 'Primary advisory.'},
        {'item_id': '2', 'duplicate_of': '1', 'reason': 'Same fixes; support deadline is incidental.'},
        {'item_id': '5', 'duplicate_of': None, 'reason': 'Independent causes and process analysis.'},
    ]}


def test_valid_decisions_follow_preference_order_and_keep_independent_analysis():
    decisions = validate_content_decisions(output(), candidates())
    assert [d.duplicate_of for d in decisions] == [None, '1', None]
    assert ensure_content_decisions(list(reversed(decisions)), candidates()) == decisions
    assert ensure_content_decisions([], []) == []


@pytest.mark.parametrize('rows', [
    [],
    [ContentDecision('1', None, 'Keep')],
    [ContentDecision('1', None, 'Keep')] * 3,
    [ContentDecision('1', '1', 'Self'), ContentDecision('2', None, 'Keep'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', '5', 'Later'), ContentDecision('2', None, 'Keep'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', None, 'Keep'), ContentDecision('2', '1', 'Same'), ContentDecision('5', '2', 'Chain')],
    [ContentDecision('1', None, 'Keep'), ContentDecision('2', 'missing', 'Unknown'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', None, ' '), ContentDecision('2', None, 'Keep'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', None, 'x' * 501), ContentDecision('2', None, 'Keep'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', None, 'Keep'), ContentDecision('2', [], 'Bad type'), ContentDecision('5', None, 'Keep')],
])
def test_invalid_decisions_fail_as_one_result(rows):
    with pytest.raises(ValueError):
        ensure_content_decisions(rows, candidates())


@pytest.mark.parametrize('invalid', [None, [], {}, {'decisions': {}}, {'decisions': [{}]},
                                     {'decisions': output()['decisions'], 'extra': 1}])
def test_invalid_wire_shapes_are_rejected(invalid):
    with pytest.raises(ValueError):
        validate_content_decisions(invalid, candidates())


def test_prompt_has_bounded_separate_untrusted_sources_and_omission_notices():
    items = candidates()[:1]
    item = items[0]
    item.story = replace(item.story, fetched_text='web ' * 10000, story_text='post ' * 10000)
    item.discussion_text = 'comment ' * 10000
    original = item.story.fetched_text
    prompt = build_content_deduplication_prompt(items)
    rows = json.loads(prompt.split('BEGIN UNTRUSTED CANDIDATE DATA\n')[1].split('\nEND UNTRUSTED')[0])
    evidence = rows[0]['evidence']
    assert set(evidence) == {'retrieved_webpage', 'hn_post', 'hn_comments'}
    assert sum(map(len, evidence.values())) <= 4000
    assert 2000 < len(evidence['retrieved_webpage']) <= 3000
    assert len(evidence['hn_post']) <= 500
    assert len(evidence['hn_comments']) <= 500
    assert all('omitted' in excerpt or 'omissions' in excerpt for excerpt in evidence.values())
    assert item.story.fetched_text == original
    assert 'untrusted' in prompt
    assert 'Sharing a topic or event alone NEVER' in CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION
    assert 'uncertainty means retain' in CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION


def test_roundup_evidence_prioritizes_answers_over_question():
    item = candidates()[0]
    item.content_kind = 'community_roundup'
    item.story = replace(item.story, story_text='question context ' * 1000,
                         fetched_text='irrelevant webpage')
    item.discussion_text = 'concrete tools and implementation experiences ' * 1000
    prompt = build_content_deduplication_prompt([item])
    rows = json.loads(prompt.split('BEGIN UNTRUSTED CANDIDATE DATA\n')[1].split('\nEND UNTRUSTED')[0])
    evidence = rows[0]['evidence']
    assert set(evidence) == {'hn_post', 'hn_comments'}
    assert len(evidence['hn_comments']) > len(evidence['hn_post'])
    assert sum(map(len, evidence.values())) <= 4000


class Opener:
    def __init__(self, provider, result):
        self.requests = []
        self.provider = provider
        self.result = result

    def __call__(self, request, timeout):
        self.requests.append(json.loads(request.data))
        if self.provider == 'openrouter':
            response = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(self.result)}}],
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 70, 'cost': .00004}}
        else:
            response = {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
                {'type': 'text', 'text': json.dumps(self.result)}]}],
                'usage': {'total_input_tokens': 100, 'total_output_tokens': 70}}
        return io.BytesIO(json.dumps(response).encode())


@pytest.mark.parametrize('provider,cls,error', [('openrouter', OpenRouterBackend, OpenRouterResponseError),
                                               ('gemini', GeminiBackend, GeminiResponseError)])
def test_backend_uses_classifier_and_does_not_alter_summary_diagnostics(provider, cls, error):
    transport = Opener(provider, output())
    backend = cls(api_key='test-key', opener=transport, min_request_interval_seconds=0)
    before_model = backend.last_summary_model
    assert backend.deduplicate(candidates())[1].duplicate_of == '1'
    request = transport.requests[0]
    assert request['model'] == backend.classifier_model
    assert backend.last_deduplication_model == backend.classifier_model
    assert backend.last_deduplication_usage['input_tokens'] == 100
    assert backend.last_summary_model == before_model
    if provider == 'openrouter':
        assert request['reasoning'] == {'enabled': False}
        assert request['max_tokens'] == 2048
        assert request['provider']['max_price'] == {'prompt': .15, 'completion': .47}
        assert backend.request_records[0]['task'] == 'deduplicate'
        assert backend.accounted_cost_usd == pytest.approx(.00004)
    else:
        assert request['generation_config'] == {'max_output_tokens': 2048}
    transport.result = {'decisions': output()['decisions'][:-1]}
    with pytest.raises(error):
        backend.deduplicate(candidates())
    assert backend.deduplicate([]) == []
    assert backend.last_deduplication_usage == {}


def test_openrouter_deduplication_shares_classifier_pacing_and_budget():
    transport = Opener('openrouter', output())
    now = [0.0]
    delays = []
    def sleep(delay):
        delays.append(delay)
        now[0] += delay
    backend = OpenRouterBackend(api_key='test-key', opener=transport,
                                classifier_min_request_interval_seconds=7,
                                summarizer_min_request_interval_seconds=20,
                                sleeper=sleep, clock=lambda: now[0])
    backend.deduplicate(candidates())
    transport.result = {'decisions': [{'id': c.story.hn_item_id, 'label': 'core_non_ai'}
                                      for c in candidates()]}
    assert backend.classify(candidates()) == {'1': 'core_non_ai', '2': 'core_non_ai', '5': 'core_non_ai'}
    assert delays == [7]
    assert backend.accounted_cost_usd == pytest.approx(.00008)
    assert [record['task'] for record in backend.request_records] == ['deduplicate', 'classify']
