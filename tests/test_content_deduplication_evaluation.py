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
        self.expected = {row['item_id']: row['duplicate_of'] for row in case['expected']}
        self.calls = []

    def deduplicate(self, candidates):
        self.calls.append(deepcopy(candidates))
        return [ContentDecision(c.story.hn_item_id, self.expected[c.story.hn_item_id], 'Fake decision')
                for c in candidates]


def test_six_comparisons_preserve_inputs_and_checkpoint_each_result(runner, case_path, tmp_path):
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
    assert result['passed'] == 6 and result['errors'] == 0
    ids = [c.story.hn_item_id for c in candidates]
    assert [[c.story.hn_item_id for c in call] for call in backend.calls] == [ids] * 3 + [ids[::-1]] * 3
    assert candidates == before
    assert backend.max_retries == 2 and backend.summarizer_min_request_interval_seconds == 1
    for row in result['runs']:
        data = json.loads(row['prompt'].split('BEGIN UNTRUSTED CANDIDATE DATA\n')[1].split('\nEND UNTRUSTED')[0])
        assert data == (case['input'] if row['order'] == 'normal' else case['input'][::-1])
        assert all(set(item) == {'item_id', 'title', 'summary'} for item in data)
    assert 'SECRET_NOT_FOR_RESULTS' not in path.read_text()
    assert json.loads(path.read_text()) == result
    with pytest.raises(FileExistsError):
        runner.run(case, candidates, backend, path)
    assert len(backend.calls) == 6


def test_errors_and_wrong_representatives_are_not_semantically_retried(runner, case_path, tmp_path):
    case, candidates = runner.load_case(case_path)

    class MixedBackend(FakeBackend):
        def deduplicate(self, items):
            decisions = super().deduplicate(items)
            if len(self.calls) == 1:
                raise RuntimeError('SECRET_NOT_FOR_RESULTS in provider response')
            if len(self.calls) == 2:
                # Same retained set, wrong representative: must fail scoring.
                return [ContentDecision(d.item_id, '50182987' if d.duplicate_of else None, d.reason)
                        for d in decisions]
            if len(self.calls) == 3:
                return decisions[:-1]
            return decisions

    backend = MixedBackend(case)
    path = tmp_path / 'results.json'
    result = runner.run(case, candidates, backend, path)
    assert len(backend.calls) == 6
    assert result['passed'] == 3 and result['errors'] == 2
    assert result['runs'][0]['error_type'] == 'RuntimeError'
    assert result['runs'][1]['error_type'] == '' and not result['runs'][1]['passed']
    assert result['runs'][2]['error_type'] == 'ValueError'
    assert 'SECRET_NOT_FOR_RESULTS' not in path.read_text()


@pytest.mark.parametrize('problem', ['extra_input', 'missing_result', 'chain', 'long_summary'])
def test_invalid_cases_fail_before_backend_creation(runner, case_path, tmp_path, monkeypatch, problem):
    case = json.loads(case_path.read_text())
    if problem == 'extra_input':
        case['input'][0]['expected'] = 'do not send'
    elif problem == 'missing_result':
        case['expected'].pop()
    elif problem == 'chain':
        case['expected'][0]['duplicate_of'] = '50182987'
    else:
        case['input'][0]['summary'] = 'x' * 1001
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
    assert json.loads(results[0].read_text())['passed'] == 6
