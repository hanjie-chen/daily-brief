"""Offline contract and wire tests for same-day information comparison."""
from copy import deepcopy
from dataclasses import replace
import io
import json

import pytest

from daily_brief.llm import ContentDecision, ensure_content_decisions
from daily_brief.llm.content_deduplication import (
    CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION,
    build_content_deduplication_prompt, validate_content_decisions,
)
from daily_brief.llm.openrouter_backend import OpenRouterAPIError, OpenRouterBackend, OpenRouterResponseError
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


def test_valid_decisions_follow_input_order_and_keep_independent_analysis():
    decisions = validate_content_decisions(output(), candidates())
    assert [d.duplicate_of for d in decisions] == [None, '1', None]
    assert ensure_content_decisions(list(reversed(decisions)), candidates()) == decisions
    assert ensure_content_decisions([], []) == []


def test_forward_references_select_later_stronger_representative_in_input_order():
    rows = [ContentDecision('1', '5', 'Later item provides stronger supported coverage.'),
            ContentDecision('2', '5', 'Same core information with incidental additions.'),
            ContentDecision('5', None, 'Most complete and useful supported account.')]
    before = list(rows)
    decisions = ensure_content_decisions([rows[2], rows[0], rows[1]], candidates())
    assert decisions == before
    assert rows == before
    wire = {'decisions': [{'item_id': d.item_id, 'duplicate_of': d.duplicate_of,
                            'reason': d.reason} for d in reversed(rows)]}
    assert validate_content_decisions(wire, candidates()) == rows


def test_invalid_later_decision_rejects_entire_result_without_mutating_inputs():
    items = candidates()
    before_items = [replace(c) for c in items]
    rows = [ContentDecision('1', '5', 'Later stronger representative.'),
            ContentDecision('2', None, 'Independent.'),
            ContentDecision('5', 'unknown', 'Invalid later target.')]
    before_rows = list(rows)
    with pytest.raises(ValueError):
        ensure_content_decisions(rows, items)
    assert rows == before_rows
    assert items == before_items


def test_prompt_is_generic_and_selects_representative_by_quality_before_rank():
    instruction = CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION.lower()
    for known_case_term in ('php', 'security advisory', 'release announcement',
                            'support deadline', 'delayed remediation', 'for example'):
        assert known_case_term not in instruction
    assert 'independently' in instruction and 'of their order' in instruction
    assert 'most complete, useful, and evidence-supported' in instruction
    assert 'tie-break' in instruction
    assert 'including one later' in instruction
    prompt = build_content_deduplication_prompt(candidates())
    assert 'input rank breaks quality ties only' in prompt
    assert 'null or any retained input ID' in prompt
    assert 'earlier retained' not in prompt


def test_prompt_requires_positive_identity_evidence_and_full_information_coverage():
    instruction = CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION.lower()
    assert 'positive evidence' in instruction
    assert 'absence of distinguishing information' in instruction
    assert 'titles alone cannot resolve that gap' in instruction
    assert 'keep only the stronger representative' in instruction
    assert 'fully covered subset independent' in instruction
    assert 'intended audience' in instruction
    assert 'distinct factual' in instruction


def test_prompt_names_character_reason_limits_without_weakening_validation():
    assert 'at most 200 characters per reason' in CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION
    assert 'hard maximum is 500 characters' in CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION
    rows = [ContentDecision('1', None, 'x' * 500),
            ContentDecision('2', '1', '重复。'), ContentDecision('5', None, '独立。')]
    assert ensure_content_decisions(rows, candidates()) == rows
    rows[0] = replace(rows[0], reason='字' * 501)
    with pytest.raises(ValueError, match='reasons must be nonempty and bounded'):
        ensure_content_decisions(rows, candidates())


@pytest.mark.parametrize('rows', [
    [],
    [ContentDecision('1', None, 'Keep')],
    [ContentDecision('1', None, 'Keep')] * 3,
    [ContentDecision('1', '1', 'Self'), ContentDecision('2', None, 'Keep'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', '2', 'Cycle'), ContentDecision('2', '1', 'Cycle'), ContentDecision('5', None, 'Keep')],
    [ContentDecision('1', '2', 'Cycle'), ContentDecision('2', '5', 'Cycle'), ContentDecision('5', '1', 'Cycle')],
    [ContentDecision('1', '2', 'Chain'), ContentDecision('2', '5', 'Chain'), ContentDecision('5', None, 'Keep')],
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
                        'usage': {'prompt_tokens': 100, 'completion_tokens': 70, 'cost': .00004,
                                  'completion_tokens_details': {'reasoning_tokens': 30}}}
        else:
            response = {'status': 'completed', 'steps': [{'type': 'model_output', 'content': [
                {'type': 'text', 'text': json.dumps(self.result)}]}],
                'usage': {'total_input_tokens': 100, 'total_output_tokens': 70}}
        return io.BytesIO(json.dumps(response).encode())


@pytest.mark.parametrize('provider,cls,error', [('openrouter', OpenRouterBackend, OpenRouterResponseError),
                                               ('gemini', GeminiBackend, GeminiResponseError)])
def test_backend_uses_task_model_and_does_not_alter_summary_diagnostics(provider, cls, error):
    transport = Opener(provider, output())
    backend = cls(api_key='test-key', opener=transport, min_request_interval_seconds=0)
    before_model = backend.last_summary_model
    transport.result = {'decisions': [
        {'item_id': '1', 'duplicate_of': '5', 'reason': 'Later stronger representative.'},
        {'item_id': '2', 'duplicate_of': '5', 'reason': 'Same core information.'},
        {'item_id': '5', 'duplicate_of': None, 'reason': 'Most complete supported account.'},
    ]}
    assert [d.duplicate_of for d in backend.deduplicate(candidates())] == ['5', '5', None]
    request = transport.requests[0]
    expected_model = backend.summarizer_model if provider == 'openrouter' else backend.classifier_model
    assert request['model'] == expected_model
    assert backend.last_deduplication_model == expected_model
    assert backend.last_deduplication_usage['input_tokens'] == 100
    assert backend.last_summary_model == before_model
    if provider == 'openrouter':
        assert request['model'] == 'openai/gpt-6-luna'
        assert request['reasoning'] == {'effort': 'medium', 'exclude': True}
        assert request['max_tokens'] == 8192
        assert request['provider']['max_price'] == {'prompt': .10, 'completion': .50}
        assert request['messages'][0]['content'] == CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION
        assert backend.last_deduplication_reasoning_effort == 'medium'
        assert backend.last_deduplication_usage['thought_tokens'] == 30
        assert backend.request_records[0]['task'] == 'deduplicate'
        assert backend.request_records[0]['reasoning_effort'] == 'medium'
        assert backend.accounted_cost_usd == pytest.approx(.00004)
    else:
        assert request['generation_config'] == {'max_output_tokens': 2048}
    transport.result = {'decisions': output()['decisions'][:-1]}
    with pytest.raises(error):
        backend.deduplicate(candidates())
    assert backend.deduplicate([]) == []
    assert backend.last_deduplication_usage == {}
    if provider == 'openrouter':
        assert backend.last_deduplication_reasoning_effort is None


def test_openrouter_deduplication_shares_summary_pacing_and_budget_without_changing_classification():
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
    summary_output = {'status': 'sufficient', 'summary': '一个工具的摘要。', 'reason': ''}
    transport.result = summary_output
    backend.summarize(candidates()[0])
    diagnostics = deepcopy({k: v for k, v in vars(backend).items() if k.startswith('last_summary_')})
    transport.result = output()
    backend.deduplicate(candidates())
    assert {k: v for k, v in vars(backend).items() if k.startswith('last_summary_')} == diagnostics
    assert delays == [20]
    transport.result = {'decisions': [{'id': c.story.hn_item_id, 'label': 'core_non_ai'}
                                      for c in candidates()]}
    assert backend.classify(candidates()) == {'1': 'core_non_ai', '2': 'core_non_ai', '5': 'core_non_ai'}
    assert delays == [20]  # The separate classifier model needs no summary-model wait.
    request = transport.requests[-1]
    assert request['model'] == 'qwen/qwen3.8-flash'
    assert request['reasoning'] == {'enabled': False}
    assert request['max_tokens'] == 512
    assert request['provider']['max_price'] == {'prompt': .15, 'completion': .47}
    transport.result = summary_output
    backend.summarize(candidates()[0])
    assert delays == [20, 20]
    assert backend.accounted_cost_usd == pytest.approx(.00016)
    assert [record['task'] for record in backend.request_records] == ['summarize', 'deduplicate', 'classify', 'summarize']


def test_openrouter_deduplication_uses_configured_summary_model_and_price_caps():
    transport = Opener('openrouter', output())
    backend = OpenRouterBackend.from_environment({
        'OPENROUTER_API_KEY': 'test-key',
        'OPENROUTER_SUMMARIZER_MODEL': 'other/summarizer',
        'OPENROUTER_SUMMARIZER_INPUT_PRICE_CAP': '.3',
        'OPENROUTER_SUMMARIZER_OUTPUT_PRICE_CAP': '.8',
    }, opener=transport, min_request_interval_seconds=0)
    backend.deduplicate(candidates())
    assert backend.last_deduplication_model == 'other/summarizer'
    assert transport.requests[0]['model'] == 'other/summarizer'
    assert transport.requests[0]['provider']['max_price'] == {'prompt': .3, 'completion': .8}


def test_openrouter_deduplication_reserves_thinking_budget_before_sending():
    transport = Opener('openrouter', output())
    backend = OpenRouterBackend(api_key='test-key', opener=transport, min_request_interval_seconds=0)
    backend.deduplicate(candidates())
    # Less than the output-only reservation for 8,192 Luna tokens remains.
    backend.max_run_cost_usd = backend.accounted_cost_usd + .003
    with pytest.raises(OpenRouterAPIError) as caught:
        backend.deduplicate(candidates())
    assert caught.value.error_code == 'budget_exceeded'
    assert len(transport.requests) == 1
    assert backend.last_deduplication_reasoning_effort is None
    assert backend.last_deduplication_usage == {}
    assert backend.accounted_cost_usd == pytest.approx(.00004)


def test_openrouter_deduplication_rejects_truncated_thinking_output_without_retry():
    requests = []
    def opener(request, timeout):
        requests.append(json.loads(request.data))
        return io.BytesIO(json.dumps({
            'choices': [{'finish_reason': 'length', 'message': {'content': ''}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 8192, 'cost': .0042,
                      'completion_tokens_details': {'reasoning_tokens': 8192}},
        }).encode())
    backend = OpenRouterBackend(api_key='test-key', opener=opener, min_request_interval_seconds=0)
    with pytest.raises(OpenRouterResponseError) as caught:
        backend.deduplicate(candidates())
    assert caught.value.provider_status == 'length'
    assert len(requests) == 1
    assert backend.last_deduplication_usage['thought_tokens'] == 8192
    assert backend.accounted_cost_usd == pytest.approx(.0042)
