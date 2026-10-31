"""Offline checks for the live-evaluation runner; never call model providers."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from daily_brief.llm import ContentDecision


@pytest.fixture
def runner():
    path = Path(__file__).parents[1] / 'evals/content_deduplication/run.py'
    spec = importlib.util.spec_from_file_location('content_deduplication_eval', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def case_path():
    return Path(__file__).parents[1] / 'evals/content_deduplication/php-2026-10-22.json'


class FakeBackend:
    name = 'fake'
    max_retries = 2
    summarizer_min_request_interval_seconds = 1
    last_deduplication_model = 'test/model'
    last_deduplication_reasoning_effort = 'medium'
    last_deduplication_usage = {'cost_usd': 0.001}
    accounted_cost_usd = 0.006
    api_key = 'SECRET_NOT_FOR_RESULTS'

    def __init__(self, case):
        self.expected = {row['item_id']: (row['duplicate_of'] or [None])[0] for row in case['expected']}
        self.calls = []

    def deduplicate(self, candidates):
        self.calls.append(deepcopy(candidates))
        return [ContentDecision(c.story.hn_item_id, self.expected[c.story.hn_item_id], 'Fake decision')
                for c in candidates]


def test_seven_comparisons_preserve_inputs_and_checkpoint_each_result(runner, case_path, tmp_path):
    case, candidates = runner.load_case(case_path)
    before = deepcopy(candidates)
    path = tmp_path / 'results.json'

    class CheckpointBackend(FakeBackend):
        def deduplicate(self, items):
            saved = json.loads(path.read_text())
            assert saved['status'] == 'running'
            assert len(saved['runs']) == len(self.calls)
            return super().deduplicate(items)

    backend = CheckpointBackend(case)
    result = runner.run(case, candidates, backend, path)
    assert result['status'] == 'complete'
    assert result['planned_runs'] == result['passed'] == 7 and result['errors'] == 0
    ids = [c.story.hn_item_id for c in candidates]
    permutations = [
        [1, 2, 3, 4, 5, 6, 7], [2, 3, 4, 5, 1, 6, 7],
        [3, 4, 5, 1, 2, 6, 7], [4, 5, 1, 2, 3, 6, 7], [5, 1, 2, 3, 4, 6, 7],
    ]
    expected_orders = [[ids[i - 1] for i in order] for order in permutations]
    actual_orders = [[c.story.hn_item_id for c in call] for call in backend.calls]
    assert actual_orders == [expected_orders[0]] * 3 + expected_orders[1:]
    assert [row['order'] for row in result['runs']] == ['original'] * 3 + [f'rotate_{i}' for i in range(1, 5)]
    assert [row['repeat'] for row in result['runs']] == [1, 2, 3, 1, 1, 1, 1]
    for position in range(5):
        assert {order[position] for order in expected_orders} == set(ids[:5])
    assert candidates == before
    assert backend.max_retries == 2 and backend.summarizer_min_request_interval_seconds == 1
    by_id = {item['item_id']: item for item in case['input']}
    for row, input_ids in zip(result['runs'], actual_orders, strict=True):
        assert row['input_ids'] == input_ids
        data = json.loads(row['prompt'].split('BEGIN UNTRUSTED CANDIDATE DATA\n')[1].split('\nEND UNTRUSTED')[0])
        assert data == [by_id[item_id] for item_id in input_ids]
        assert all(set(item) == {'item_id', 'title', 'summary'} for item in data)
    assert 'SECRET_NOT_FOR_RESULTS' not in path.read_text()
    assert json.loads(path.read_text()) == result
    with pytest.raises(FileExistsError):
        runner.run(case, candidates, backend, path)
    assert len(backend.calls) == 7


def test_errors_and_wrong_representatives_are_not_semantically_retried(runner, case_path, tmp_path):
    case, candidates = runner.load_case(case_path)

    class MixedBackend(FakeBackend):
        def deduplicate(self, items):
            decisions = super().deduplicate(items)
            if len(self.calls) == 1:
                raise RuntimeError('SECRET_NOT_FOR_RESULTS in provider response')
            if len(self.calls) == 2:
                # Same retained set, wrong representative: must fail scoring.
                return [ContentDecision(d.item_id, '50177916' if d.duplicate_of else None, d.reason)
                        for d in decisions]
            if len(self.calls) == 3:
                return decisions[:-1]
            return decisions

    backend = MixedBackend(case)
    path = tmp_path / 'results.json'
    result = runner.run(case, candidates, backend, path)
    assert len(backend.calls) == 7
    assert result['passed'] == 4 and result['errors'] == 2
    assert result['runs'][0]['error_type'] == 'RuntimeError'
    assert result['runs'][1]['error_type'] == '' and not result['runs'][1]['passed']
    assert result['runs'][2]['error_type'] == 'ValueError'
    assert 'SECRET_NOT_FOR_RESULTS' not in path.read_text()


@pytest.mark.parametrize('problem', [
    'extra_input', 'missing_result', 'chain', 'long_summary', 'empty_targets',
    'scalar_target', 'unknown_target', 'self_target', 'duplicate_targets', 'null_target',
])
def test_invalid_cases_fail_before_backend_creation(runner, case_path, tmp_path, monkeypatch, problem):
    case = json.loads(case_path.read_text())
    if problem == 'extra_input':
        case['input'][0]['expected'] = 'do not send'
    elif problem == 'missing_result':
        case['expected'].pop()
    elif problem == 'chain':
        case['expected'][0]['duplicate_of'] = ['50182987']
    elif problem == 'long_summary':
        case['input'][0]['summary'] = 'x' * 1001
    else:
        case['expected'][1]['duplicate_of'] = {
            'empty_targets': [], 'scalar_target': '50172041',
            'unknown_target': ['50172041', 'unknown'],
            'self_target': ['50172041', '50180873'],
            'duplicate_targets': ['50172041', '50172041'],
            'null_target': ['50172041', None],
        }[problem]
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(case))
    monkeypatch.setattr(runner, 'create_model_backend', lambda: pytest.fail('Must not create a backend'))
    with pytest.raises(ValueError):
        runner.main([str(path)])


def test_cli_uses_production_factory_without_overrides_and_writes_to_data(runner, case_path, tmp_path, monkeypatch):
    case, _ = runner.load_case(case_path)
    backend = FakeBackend(case)
    calls = []

    def factory(*args, **kwargs):
        calls.append((args, kwargs))
        return backend

    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'create_model_backend', factory)
    assert runner.main([str(case_path)]) == 0
    assert calls == [((), {})]
    results = list((tmp_path / 'data/content-dedup-evaluations').glob('*.json'))
    assert len(results) == 1
    assert json.loads(results[0].read_text())['passed'] == 7


@pytest.mark.parametrize('count', [2, 3])
def test_short_inputs_rotate_only_existing_items(runner, case_path, tmp_path, count):
    case = json.loads(case_path.read_text())
    case['input'] = case['input'][:count]
    ids = [item['item_id'] for item in case['input']]
    case['expected'] = [row for row in case['expected'] if row['item_id'] in ids]
    for row in case['expected']:
        if row['duplicate_of'] is not None:
            row['duplicate_of'] = [target for target in row['duplicate_of'] if target in ids]
    path = tmp_path / 'case.json'
    path.write_text(json.dumps(case))
    case, candidates = runner.load_case(path)
    backend = FakeBackend(case)
    result = runner.run(case, candidates, backend, tmp_path / 'results.json')
    assert result['planned_runs'] == result['passed'] == count + 2
    orders = [row['input_ids'] for row in result['runs']]
    assert orders[:3] == [ids] * 3
    unique = {tuple(order) for order in orders}
    assert len(unique) == count
    assert all(len(order) == count and set(order) == set(ids) for order in orders)
    for position in range(count):
        assert {order[position] for order in unique} == set(ids)


@pytest.mark.parametrize('target', ['50172041', '50182987'])
def test_either_allowed_representative_passes(runner, case_path, tmp_path, target):
    case, candidates = runner.load_case(case_path)
    backend = FakeBackend(case)
    backend.expected['50180873'] = target
    result = runner.run(case, candidates, backend, tmp_path / 'results.json')
    assert result['passed'] == 7 and result['errors'] == 0


@pytest.mark.parametrize('item_id,target', [
    ('50180873', None),
    ('50172041', '50182987'),
    ('50182987', '50172041'),
    ('50177916', '50172041'),
])
def test_allowed_targets_do_not_relax_keep_or_drop_requirements(runner, case_path, tmp_path, item_id, target):
    case, candidates = runner.load_case(case_path)
    backend = FakeBackend(case)
    if item_id == '50172041':
        backend.expected['50180873'] = '50182987'
    backend.expected[item_id] = target
    result = runner.run(case, candidates, backend, tmp_path / 'results.json')
    assert result['passed'] == 0 and result['errors'] == 0
