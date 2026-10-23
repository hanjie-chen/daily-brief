"""Replay frozen information-overlap cases without generation or publication."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from daily_brief.llm import create_model_backend, ensure_content_decisions
from daily_brief.models import Candidate, Story
from daily_brief.network_recovery import suspend_network_recovery

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals/content_deduplication/cases-v2.json"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_./:-]{1,160}$")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_cases(path=DEFAULT_CASES):
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise ValueError("case file exceeds one megabyte")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("cases"), list):
        raise ValueError("unsupported case schema")
    cases = payload["cases"]
    if not 1 <= len(cases) <= 30:
        raise ValueError("expected 1 to 30 cases")
    seen = set()
    for case in cases:
        if not isinstance(case.get("id"), str) or not SAFE_IDENTIFIER.fullmatch(case["id"]) or case["id"] in seen:
            raise ValueError("case IDs must be unique safe identifiers")
        seen.add(case["id"])
        if case.get("category") not in {"development_regression", "authored_holdout"}:
            raise ValueError("invalid case category")
        if case.get("evaluation_group", case["category"]) not in {
            "development_regression", "authored_holdout", "previously_evaluated_untuned",
            "first_view_v2_holdout",
        }:
            raise ValueError("invalid evaluation group")
        if not isinstance(case.get("items"), list) or not 2 <= len(case["items"]) <= 7:
            raise ValueError("cases require 2 to 7 items")
        ids = []
        for item in case["items"]:
            if set(item) != {"item_id", "title", "summary", "evidence", "source_url"}:
                raise ValueError("invalid item fields")
            if any(not isinstance(value, str) or len(value) > 30_000 for value in item.values()):
                raise ValueError("item fields must be bounded strings")
            if not item["item_id"].isdigit() or not item["summary"].strip():
                raise ValueError("items require numeric IDs and nonempty summaries")
            ids.append(item["item_id"])
        retained = case.get("expected_retained_ids")
        targets = case.get("expected_duplicate_targets")
        if len(ids) != len(set(ids)) or not isinstance(retained, list) or not retained:
            raise ValueError("invalid item IDs or retained set")
        if len(set(retained)) != len(retained) or not set(retained) <= set(ids):
            raise ValueError("invalid expected retained IDs")
        if not isinstance(targets, dict) or set(targets) != set(ids) - set(retained):
            raise ValueError("expected decisions must cover each dropped item")
        for allowed in targets.values():
            if not isinstance(allowed, list) or not allowed or not set(allowed) <= set(retained):
                raise ValueError("duplicates must target independently retained items")
    return cases


def ignored_path(path):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to((ROOT / "data").resolve()):
        raise ValueError("private evidence and results must be under the ignored project data/ directory")
    return resolved


def candidate_inputs(case, order, php_evidence_dir=None):
    rows = case["items"] if order == "normal" else list(reversed(case["items"]))
    candidates = []
    for row in rows:
        evidence = row["evidence"]
        if php_evidence_dir is not None and case["id"] == "php-security-2026-10-22":
            path = ignored_path(ignored_path(php_evidence_dir) / f"{row['item_id']}-article.txt")
            if path.stat().st_size > 1_000_000:
                raise ValueError("local evidence exceeds one megabyte")
            evidence = path.read_text(encoding="utf-8")
            if not evidence.strip():
                raise ValueError("explicit PHP evidence must be nonempty")
        candidate = Candidate(story=Story(
            source="evaluation", hn_item_id=row["item_id"], title=row["title"],
            source_url=row["source_url"], hn_discussion_url="", created_at="",
            points=0, comments=0, fetched_text=evidence,
        ))
        candidate.summary = row["summary"]
        candidate.summary_status = "success"
        candidate.article_retrieval.material_origin = "original" if evidence else ""
        candidates.append(candidate)
    return candidates


def expected_mapping(case):
    return {row["item_id"]: ([None] if row["item_id"] in case["expected_retained_ids"]
                             else case["expected_duplicate_targets"][row["item_id"]])
            for row in case["items"]}


def score_decisions(case, decisions):
    actual = {decision.item_id: decision.duplicate_of for decision in decisions}
    expected = expected_mapping(case)
    retained = sorted(item_id for item_id, target in actual.items() if target is None)
    passed = (set(actual) == set(expected)
              and all(actual[item_id] in allowed for item_id, allowed in expected.items())
              and retained == sorted(case["expected_retained_ids"]))
    return passed, retained


def safe_usage(backend):
    source = getattr(backend, "last_deduplication_usage", {})
    if not isinstance(source, dict):
        return {}
    return {key: value for key, value in source.items()
            if key in {"input_tokens", "output_tokens", "thought_tokens", "total_tokens", "cost_usd"}
            and (value is None or (type(value) in {int, float} and math.isfinite(value) and value >= 0))}


def safe_id(value):
    return value if isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value) else ""


def run_suite(cases, backend, *, repeats=3, max_requests=100, php_evidence_dir=None,
              clock=time.monotonic):
    """No answers/rationales are passed to the backend. Every call uses fresh inputs."""
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats must be between 1 and 10")
    retries = getattr(backend, "max_retries", 0)
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError("evaluation backend max_retries must be 0 to 2")
    calls = len(cases) * 2 * repeats
    # max_requests bounds transport attempts, including configured short retries.
    if type(max_requests) is not int or not 1 <= max_requests <= 600 or calls * (retries + 1) > max_requests:
        raise ValueError("planned calls including retries exceed max_requests (1 to 600)")
    # Import only for recording the production input and frozen system snapshot;
    # expectation fields never enter either prompt builder or backend.
    from daily_brief.llm.content_deduplication import (
        CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION, build_content_deduplication_prompt,
    )
    system = CONTENT_DEDUPLICATION_SYSTEM_INSTRUCTION
    runs = []
    for case in cases:
        for order in ("normal", "reversed"):
            for repeat in range(1, repeats + 1):
                candidates = candidate_inputs(case, order, php_evidence_dir)
                prompt = build_content_deduplication_prompt(candidates)
                row = {
                    "case_id": case["id"], "category": case["category"], "order": order,
                    "evaluation_group": case.get("evaluation_group", case["category"]),
                    "repeat": repeat, "input_ids": [c.story.hn_item_id for c in candidates],
                    "input_sha256": digest([asdict(c) for c in candidates]),
                    "prompt_sha256": digest(prompt), "prompt": prompt,
                    "expected": expected_mapping(case),
                    "expected_retained_ids": case["expected_retained_ids"],
                    "decisions": [], "retained_ids": [], "passed": False,
                    "error_type": "", "error_code": "",
                    "authoritative": not (case["id"] == "php-security-2026-10-22" and php_evidence_dir is None),
                    "evidence": [{"item_id": c.story.hn_item_id,
                                  "available": bool(c.story.fetched_text.strip()),
                                  "chars": len(c.story.fetched_text),
                                  "sha256": digest(c.story.fetched_text),
                                  "local_override": php_evidence_dir is not None and case["id"] == "php-security-2026-10-22"}
                                 for c in candidates],
                }
                records = getattr(backend, "request_records", [])
                before = len(records)
                started = clock()
                try:
                    with suspend_network_recovery():
                        decisions = ensure_content_decisions(backend.deduplicate(candidates), candidates)
                    row["decisions"] = [asdict(decision) for decision in decisions]
                    row["passed"], row["retained_ids"] = score_decisions(case, decisions)
                except Exception as exc:
                    # Provider messages/tracebacks may contain credentials or raw requests.
                    row["error_type"] = type(exc).__name__
                    row["error_code"] = safe_id(getattr(exc, "error_code", ""))
                row["latency_seconds"] = max(0, clock() - started)
                row["model"] = safe_id(getattr(backend, "last_deduplication_model", ""))
                row["usage"] = safe_usage(backend)
                row["cost_usd"] = row["usage"].get("cost_usd")
                new_records = records[before:]
                attempts = len(new_records) or getattr(backend, "_last_request_attempts_by_model", {}).get(row["model"])
                row["attempts"] = attempts
                if new_records:
                    costs = [record.get("cost_usd") for record in new_records]
                    row["cost_usd"] = (sum(costs) if all(type(cost) in {int, float} and math.isfinite(cost) and cost >= 0 for cost in costs) else None)
                    row["accounted_cost_usd"] = sum(record.get("cost_usd") if type(record.get("cost_usd")) in {int, float} else record.get("reserved_usd", 0) for record in new_records)
                runs.append(row)
    stats = []
    for case in cases:
        rows = [row for row in runs if row["case_id"] == case["id"]]
        valid = [row for row in rows if not row["error_type"]]
        stats.append({
            "case_id": case["id"], "category": case["category"],
            "evaluation_group": case.get("evaluation_group", case["category"]),
            "passed": sum(row["passed"] for row in rows), "runs": len(rows),
            "normal_passed": sum(row["passed"] for row in rows if row["order"] == "normal"),
            "reversed_passed": sum(row["passed"] for row in rows if row["order"] == "reversed"),
            "errors": len(rows) - len(valid),
            "authoritative": all(row["authoritative"] for row in rows),
            "retained_set_consistent": len(valid) == len(rows) and len({tuple(row["retained_ids"]) for row in valid}) == 1,
        })
    groups = []
    for group in sorted({row["evaluation_group"] for row in runs}):
        rows = [row for row in runs if row["evaluation_group"] == group]
        groups.append({
            "evaluation_group": group,
            "cases": len({row["case_id"] for row in rows}), "runs": len(rows),
            "passed": sum(row["passed"] for row in rows),
            "errors": sum(bool(row["error_type"]) for row in rows),
            "normal_passed": sum(row["passed"] for row in rows if row["order"] == "normal"),
            "reversed_passed": sum(row["passed"] for row in rows if row["order"] == "reversed"),
            "authoritative": all(row["authoritative"] for row in rows),
        })
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "suite_sha256": digest(cases), "system_instruction": system,
        "system_sha256": digest(system), "repeats": repeats,
        "planned_calls": calls, "max_transport_attempts": calls * (retries + 1),
        "php_evidence_dir": str(ignored_path(php_evidence_dir)) if php_evidence_dir else None,
        "passed": sum(row["passed"] for row in runs), "runs": runs, "case_stats": stats,
        "group_stats": groups,
        "limitations": "Small authored set with development, previously evaluated, and fresh holdout groups; report groups separately, not as a fully held-out aggregate. Not a production accuracy estimate. Provider cost/attempt data may be unavailable (null).",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--max-retries", type=int, choices=range(3), default=0)
    parser.add_argument("--budget-usd", type=float, default=0.25)
    parser.add_argument("--php-evidence-dir", type=Path)
    args = parser.parse_args(argv)
    output = ignored_path(args.output)
    cases = load_cases(args.cases)
    if not math.isfinite(args.budget_usd) or args.budget_usd <= 0 or args.budget_usd > 5:
        parser.error("budget-usd must be positive and at most 5")
    env = dict(os.environ)
    configured_budget = float(env.get("OPENROUTER_RUN_BUDGET_USD", args.budget_usd))
    env["OPENROUTER_RUN_BUDGET_USD"] = str(min(configured_budget, args.budget_usd))
    backend = create_model_backend(env=env, evaluation=True)
    backend.max_retries = args.max_retries
    result = run_suite(cases, backend, repeats=args.repeats, max_requests=args.max_requests,
                       php_evidence_dir=args.php_evidence_dir)
    result["budget_usd"] = float(env["OPENROUTER_RUN_BUDGET_USD"]) if env.get("LLM_PROVIDER", "gemini") == "openrouter" else None
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{result['passed']}/{result['planned_calls']} passed; results: {output}")
    return 0 if result["passed"] == result["planned_calls"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
