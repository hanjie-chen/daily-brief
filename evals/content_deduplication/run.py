"""Repeat the original order three times, then rotate the first five items once each."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from daily_brief.llm import ContentDecision, create_model_backend, ensure_content_decisions
from daily_brief.llm.content_deduplication import (
    CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION,
    build_content_deduplication_prompt,
)
from daily_brief.models import Candidate, Story
from daily_brief.network_recovery import suspend_network_recovery

ROOT = Path(__file__).resolve().parents[2]


def load_case(path: Path) -> tuple[dict, list[Candidate]]:
    case = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(case, dict) or set(case) != {'input', 'expected'}:
        raise ValueError('Case must contain only input and expected')
    if not isinstance(case['input'], list) or not 2 <= len(case['input']) <= 7:
        raise ValueError('Case must contain 2 to 7 input articles')
    candidates = []
    for row in case['input']:
        if not isinstance(row, dict) or set(row) != {'item_id', 'title', 'summary'}:
            raise ValueError('Input articles require item_id, title and summary')
        if not all(isinstance(value, str) and value.strip() for value in row.values()):
            raise ValueError('Input fields must be nonempty strings')
        if len(row['title']) > 500 or len(row['summary']) > 1000:
            raise ValueError('Input exceeds production title/summary limits (500/1000 characters)')
        candidates.append(Candidate(Story(
            source='hacker_news', hn_item_id=row['item_id'], title=row['title'],
            source_url='', hn_discussion_url='', created_at='', points=0, comments=0,
        ), summary=row['summary'], summary_status='success'))
    if not isinstance(case['expected'], list):
        raise ValueError('Expected results must be a list')
    for row in case['expected']:
        if not isinstance(row, dict) or set(row) != {'item_id', 'duplicate_of'}:
            raise ValueError('Expected results require item_id and duplicate_of')
        targets = row['duplicate_of']
        if targets is not None and (
            not isinstance(targets, list) or not targets
            or not all(isinstance(target, str) for target in targets)
            or len(set(targets)) != len(targets)
        ):
            raise ValueError('Expected duplicate_of must be null or a nonempty list of unique article IDs')
    ensure_content_decisions([
        ContentDecision(row['item_id'], (row['duplicate_of'] or [None])[0], 'Expected result')
        for row in case['expected']
    ], candidates)
    retained = {row['item_id'] for row in case['expected'] if row['duplicate_of'] is None}
    if any(target not in retained for row in case['expected'] for target in row['duplicate_of'] or []):
        raise ValueError('Every allowed duplicate target must be an article expected to be retained')
    return case, candidates


def save(path: Path, result: dict) -> None:
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run(case: dict, candidates: list[Candidate], backend, path: Path) -> dict:
    expected = {row['item_id']: row['duplicate_of'] if row['duplicate_of'] is not None else [None]
                for row in case['expected']}
    rotating, fixed = candidates[:5], candidates[5:]
    orders = [('original', 3, candidates)] + [
        (f'rotate_{shift}', 1, rotating[shift:] + rotating[:shift] + fixed)
        for shift in range(1, len(rotating))
    ]
    result = {
        'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'case': case,
        'system_instruction': CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION,
        'provider': backend.name,
        'config': {key: getattr(backend, key, None) for key in (
            'classifier_model', 'summarizer_model', 'max_retries', 'retry_base_seconds',
            'timeout_seconds', 'classifier_min_request_interval_seconds',
            'summarizer_min_request_interval_seconds', 'max_run_cost_usd',
        )},
        'planned_runs': sum(repeats for _, repeats, _ in orders), 'runs': [],
    }
    # Reserve a new result file before making any paid requests.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    for order, repeats, items in orders:
        prompt = build_content_deduplication_prompt(items)
        for repeat in range(1, repeats + 1):
            row = {'order': order, 'repeat': repeat, 'prompt': prompt,
                   'input_ids': [item.story.hn_item_id for item in items],
                   'passed': False, 'decisions': [], 'error_type': '', 'http_status': None}
            before = len(getattr(backend, 'request_records', []))
            start = time.monotonic()
            try:
                # Same optional-comparison recovery policy as generation.
                with suspend_network_recovery():
                    decisions = ensure_content_decisions(backend.deduplicate(items), items)
                row['decisions'] = [asdict(decision) for decision in decisions]
                row['passed'] = all(d.duplicate_of in expected[d.item_id] for d in decisions)
            except Exception as exc:
                # Provider exception messages can contain private request data.
                row['error_type'] = type(exc).__name__
                status = getattr(exc, 'http_status', None)
                row['http_status'] = status if isinstance(status, int) else None
            row.update(
                elapsed_seconds=time.monotonic() - start,
                model=getattr(backend, 'last_deduplication_model', ''),
                reasoning_effort=getattr(backend, 'last_deduplication_reasoning_effort', None),
                usage=dict(getattr(backend, 'last_deduplication_usage', {})),
                requests=getattr(backend, 'request_records', [])[before:],
            )
            result['runs'].append(row)
            save(path, result)
            status = 'PASS' if row['passed'] else 'ERROR' if row['error_type'] else 'FAIL'
            print(f'{order} {repeat}/{repeats}: {status}', flush=True)
    result.update(status='complete', finished_at=datetime.now(timezone.utc).isoformat(),
                  passed=sum(row['passed'] for row in result['runs']),
                  errors=sum(bool(row['error_type']) for row in result['runs']),
                  accounted_cost_usd=getattr(backend, 'accounted_cost_usd', None))
    save(path, result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', type=Path, help='Case JSON path')
    args = parser.parse_args(argv)
    case, candidates = load_case(args.case)
    backend = create_model_backend()
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = ROOT / 'data/content-dedup-evaluations' / f'{args.case.stem}-{timestamp}.json'
    print(f'Results: {path}', flush=True)
    result = run(case, candidates, backend, path)
    print(f"Passed {result['passed']}/{result['planned_runs']}; errors {result['errors']}", flush=True)
    return 0 if result['passed'] == result['planned_runs'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
