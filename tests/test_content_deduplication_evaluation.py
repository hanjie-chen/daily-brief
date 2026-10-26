"""Evaluation runner checks use fakes only; no model credentials or live services."""
import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

from daily_brief.llm import ContentDecision


@pytest.fixture
def evaluator():
    path = Path(__file__).parents[1] / "scripts/evaluate_content_deduplication.py"
    spec = importlib.util.spec_from_file_location("content_evaluation_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExpectedBackend:
    max_retries = 0
    last_deduplication_model = "test/model"
    last_deduplication_usage = {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.001}

    def __init__(self, cases):
        self.cases = cases
        self.calls = []
        self.request_records = []

    def deduplicate(self, candidates):
        ids = {candidate.story.hn_item_id for candidate in candidates}
        case = next(case for case in self.cases if ids == {row["item_id"] for row in case["items"]})
        self.calls.append(candidates)
        self.request_records.append({"cost_usd": 0.001, "reserved_usd": 0.002})
        return [ContentDecision(candidate.story.hn_item_id,
                                case["expected_duplicate_targets"].get(candidate.story.hn_item_id, [None])[0],
                                "A bounded explanation.") for candidate in candidates]


def test_frozen_suite_has_independent_gold_and_required_coverage(evaluator):
    cases = evaluator.load_cases()
    assert len(cases) == 11
    assert cases[0]["category"] == "development_regression"
    assert sum(case["category"] == "authored_holdout" for case in cases) == 6
    assert cases[0]["expected_retained_ids"] == ["50172041", "50182987"]
    assert all(not item["evidence"] for item in cases[0]["items"])
    assert {case["id"]: case["expected_retained_ids"] for case in cases}["laboratory-water-correction"] == ["2502"]
    database = next(case for case in cases if case["id"] == "alderdb-benchmark-corrected-measurement")
    assert database["category"] == database["evaluation_group"] == "development_regression"
    assert database["expected_retained_ids"] == ["4102"]
    assert database["expected_duplicate_targets"] == {"4101": ["4102"]}
    for case in cases:
        normal = evaluator.candidate_inputs(case, "normal")
        reverse = evaluator.candidate_inputs(case, "reversed")
        assert [candidate.story.hn_item_id for candidate in normal] == list(reversed([candidate.story.hn_item_id for candidate in reverse]))
        # Production receives Candidate data only, never rationale or gold fields.
        assert all(not hasattr(candidate, "expected_retained_ids") for candidate in normal)


def test_runner_repeats_both_orders_with_matching_hashes_and_cost_records(evaluator):
    cases = evaluator.load_cases()
    backend = ExpectedBackend(cases)
    output = evaluator.run_suite(cases, backend, repeats=3)
    assert output["planned_calls"] == output["passed"] == len(backend.calls) == 66
    assert output["max_transport_attempts"] == 66
    assert output["system_instruction"]
    assert len(output["system_sha256"]) == 64
    assert all(row["passed"] == row["runs"] == 6 for row in output["case_stats"])
    assert all(row["retained_set_consistent"] for row in output["case_stats"])
    rows = output["runs"][:6]
    assert rows[0]["prompt_sha256"] == rows[1]["prompt_sha256"]
    assert rows[0]["input_sha256"] == rows[1]["input_sha256"]
    assert rows[0]["prompt_sha256"] != rows[3]["prompt_sha256"]
    assert all(row["attempts"] == 1 and row["cost_usd"] == 0.001 for row in output["runs"])
    assert all(row["usage"]["input_tokens"] == 100 for row in output["runs"])
    assert all(not row["authoritative"] for row in output["runs"][:6])
    assert all(row["authoritative"] for row in output["runs"][6:])
    assert all(not item["available"] for item in output["runs"][0]["evidence"])
    groups = {row["evaluation_group"]: row for row in output["group_stats"]}
    assert groups["development_regression"]["runs"] == groups["development_regression"]["passed"] == 30
    assert groups["previously_evaluated_untuned"]["runs"] == groups["previously_evaluated_untuned"]["passed"] == 18
    assert groups["first_view_v2_holdout"]["runs"] == groups["first_view_v2_holdout"]["passed"] == 18
    assert all("evaluation_group" in row for row in output["runs"] + output["case_stats"])


def test_group_defaults_to_category_when_unspecified(evaluator, tmp_path):
    cases = evaluator.load_cases()[:1]
    cases[0].pop("evaluation_group")
    path = tmp_path / "ungrouped.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": cases}))
    cases = evaluator.load_cases(path)
    result = evaluator.run_suite(cases, ExpectedBackend(cases), repeats=1)
    assert result["planned_calls"] == result["passed"] == 2
    assert all(row["evaluation_group"] == row["category"] for row in result["runs"] + result["case_stats"])


def test_v2_fresh_cases_and_gold_remain_frozen(evaluator):
    assert hashlib.sha256(evaluator.DEFAULT_CASES.read_bytes()).hexdigest() == "106a552a4fad284fc2b74463e093059f7bd301fd12cde96c769774d4b0c76749"
    cases = evaluator.load_cases()
    fresh = [case for case in cases if case["evaluation_group"] == "first_view_v2_holdout"]
    assert len(fresh) == 3
    assert all(case["provenance"]["introduced_in"] == "v2" for case in fresh)
    assert all(item["evidence"].strip() for case in fresh for item in case["items"])


def test_scoring_checks_target_mapping_not_just_retained_set(evaluator):
    case = evaluator.load_cases()[0]
    decisions = [ContentDecision("50172041", None, "retain"),
                 ContentDecision("50180873", "50182987", "incorrect target"),
                 ContentDecision("50182987", None, "retain")]
    passed, retained = evaluator.score_decisions(case, decisions)
    assert retained == sorted(case["expected_retained_ids"])
    assert not passed


def test_wrong_semantics_and_provider_errors_are_recorded_without_error_message(evaluator):
    case = evaluator.load_cases()[1]

    class BadBackend(ExpectedBackend):
        def deduplicate(self, candidates):
            self.calls.append(candidates)
            if len(self.calls) == 1:
                raise RuntimeError("SECRET_API_KEY: must never be serialized")
            return [ContentDecision(candidate.story.hn_item_id, None, "retain everything")
                    for candidate in candidates]

    output = evaluator.run_suite([case], BadBackend([case]), repeats=1)
    assert output["passed"] == 0
    assert output["case_stats"][0]["errors"] == 1
    assert output["runs"][0]["error_type"] == "RuntimeError"
    assert "SECRET_API_KEY" not in json.dumps(output)
    assert not output["case_stats"][0]["retained_set_consistent"]


def test_invalid_response_fails_validation_before_semantic_scoring(evaluator):
    case = evaluator.load_cases()[0]

    class PartialBackend(ExpectedBackend):
        def deduplicate(self, candidates):
            return [ContentDecision(candidates[0].story.hn_item_id, None, "partial")]

    output = evaluator.run_suite([case], PartialBackend([case]), repeats=1)
    assert all(row["error_type"] == "ValueError" and not row["decisions"] for row in output["runs"])


@pytest.mark.parametrize("repeats,max_requests,retries", [(0, 100, 0), (11, 100, 0), (3, 41, 0), (3, 100, 2)])
def test_request_limits_reject_before_any_backend_call(evaluator, repeats, max_requests, retries):
    cases = evaluator.load_cases()
    backend = ExpectedBackend(cases)
    backend.max_retries = retries
    with pytest.raises(ValueError):
        evaluator.run_suite(cases, backend, repeats=repeats, max_requests=max_requests)
    assert not backend.calls


def test_loading_rejects_missing_gold_and_bad_targets(evaluator, tmp_path):
    cases = evaluator.load_cases()
    cases[0]["expected_duplicate_targets"]["50180873"] = ["50180873"]
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"schema_version": 1, "cases": cases}))
    with pytest.raises(ValueError, match="independently retained"):
        evaluator.load_cases(path)


def test_local_evidence_requires_explicit_ignored_directory(evaluator, tmp_path, monkeypatch):
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    cases = evaluator.load_cases()
    private = tmp_path / "data/private-inputs"
    private.mkdir(parents=True)
    case = cases[0]
    for row in case["items"]:
        (private / f"{row['item_id']}-article.txt").write_text("Locally supplied article body.")
    default = evaluator.candidate_inputs(case, "normal")
    explicit = evaluator.candidate_inputs(case, "normal", private)
    assert all(not candidate.story.fetched_text for candidate in default)
    assert all(candidate.story.fetched_text == "Locally supplied article body." for candidate in explicit)
    result = evaluator.run_suite([case], ExpectedBackend([case]), repeats=1, php_evidence_dir=private)
    assert result["case_stats"][0]["authoritative"]
    assert all(item["local_override"] and item["available"]
               and item["sha256"] == evaluator.digest("Locally supplied article body.")
               for item in result["runs"][0]["evidence"])
    with pytest.raises(ValueError, match="ignored"):
        evaluator.candidate_inputs(case, "normal", tmp_path)


def test_cli_uses_budgeted_backend_writes_only_requested_private_result(evaluator, tmp_path, monkeypatch):
    monkeypatch.setattr(evaluator, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_RUN_BUDGET_USD", "0.10")
    cases = evaluator.load_cases()
    backend = ExpectedBackend(cases)
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return backend

    monkeypatch.setattr(evaluator, "create_model_backend", factory)
    output = tmp_path / "data/eval-results/results.json"
    assert evaluator.main(["--output", str(output), "--repeats", "1", "--budget-usd", "0.2"]) == 0
    assert factory_calls[0]["env"]["OPENROUTER_RUN_BUDGET_USD"] == "0.1"
    assert factory_calls[0]["evaluation"] is True
    assert output.exists()
    assert not (tmp_path / "briefs").exists()
    assert not (tmp_path / "data/recommendation-history.json").exists()
