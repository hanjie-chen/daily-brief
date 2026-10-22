"""Deterministic orchestration checks; mocked decisions do not assess model quality."""

import json
from pathlib import Path

import pytest

from daily_brief.generation import run_generate
from daily_brief.llm import ContentDecision
from fakes import FakeClassifier, FakeSummarizer, story


class ScriptedDeduplicator:
    last_deduplication_model = "test-content-model"

    def __init__(self, duplicate_of=None, *, failure=None):
        self.duplicate_of = duplicate_of or {}
        self.failure = failure
        self.calls = []

    def deduplicate(self, candidates):
        self.calls.append([candidate.story.hn_item_id for candidate in candidates])
        assert all(candidate.summary_status == "success" for candidate in candidates)
        if self.failure is not None:
            if isinstance(self.failure, Exception):
                raise self.failure
            return self.failure
        return [
            ContentDecision(
                candidate.story.hn_item_id,
                self.duplicate_of.get(candidate.story.hn_item_id),
                "Repeated core facts" if candidate.story.hn_item_id in self.duplicate_of
                else "Independent information or uncertain overlap",
            )
            for candidate in candidates
        ]


def core_stories(count):
    return [
        story(str(index), f"SQLite topic {index}", points=1000 - index * 50)
        for index in range(1, count + 1)
    ]


def generate(tmp_path, stories, deduplicator, **kwargs):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-10-22",
        algolia_stories=stories,
        hot_stories=[],
        summarizer=kwargs.pop("summarizer", FakeSummarizer()),
        classifier=kwargs.pop("classifier", FakeClassifier()),
        deduplicator=deduplicator,
        **kwargs,
    )
    payload = json.loads(result.public_json_path.read_text())
    records = {row["hn_item_id"]: row for row in json.loads(result.data_path.read_text())}
    ids = {
        section: [item["hn_item_id"] for item in value["items"]]
        for section, value in payload["sections"].items()
    }
    history = json.loads((tmp_path / "data/recommendation-history.json").read_text())
    return ids, records, history


def test_no_duplicates_costs_one_comparison_and_does_not_summarize_reserves(tmp_path):
    deduplicator = ScriptedDeduplicator()
    summarizer = FakeSummarizer()
    ids, records, history = generate(
        tmp_path, core_stories(7), deduplicator, summarizer=summarizer,
    )

    assert deduplicator.calls == [["1", "2", "3", "4", "5"]]
    assert len(summarizer.titles) == 5
    assert ids["ai"] == history["2026-10-22"] == ["1", "2", "3", "4", "5"]
    assert all(records[item_id]["content_deduplication"][0]["status"] == "retained"
               for item_id in ids["ai"])
    assert records["6"]["selected"] is False


def test_optional_comparison_does_not_exhaust_scheduled_network_recovery(tmp_path):
    from urllib.error import URLError
    from daily_brief.network_recovery import NetworkRecovery, recoverable_network

    class UnavailableDeduplicator:
        @recoverable_network("dedup-test")
        def deduplicate(self, candidates):
            raise URLError(TimeoutError("temporary outage"))

    with NetworkRecovery(max_pauses=0) as recovery:
        ids, records, _ = generate(tmp_path, core_stories(6), UnavailableDeduplicator())
        assert not recovery.exhausted
        assert recovery.pauses == 0
    assert ids["ai"] == ["1", "2", "3", "4", "5"]
    assert records["1"]["content_deduplication"][0]["status"] == "failed"


def test_refill_summary_inputs_remain_loadable_for_model_evaluation(tmp_path):
    from daily_brief.llm.model_evaluation import load_model_evaluation_input

    generate(tmp_path, core_stories(6), ScriptedDeduplicator({"2": "1"}),
             capture_model_inputs=True)
    captured = load_model_evaluation_input(tmp_path / "data/model-eval-inputs/2026-10-22.json")
    assert [item.story.hn_item_id for item in captured.summary_candidates] == [
        "1", "2", "3", "4", "5", "6",
    ]


def test_php_release_duplicate_preserves_foundation_analysis_refills_and_saves_final_history(tmp_path):
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/content_deduplication/php_security_2026_10_22.json").read_text())
    first, second, fifth = fixture["items"]
    rows = [first, second,
            {"hn_item_id": "101", "title": "Independent report A"},
            {"hn_item_id": "102", "title": "Independent report B"}, fifth,
            {"hn_item_id": "103", "title": "Independent report C"}]
    stories = [story(row["hn_item_id"], row["title"],
                     points=100000 // (index + 1),
                     url=row.get("source_url")) for index, row in enumerate(rows)]

    class PublicSummarySummarizer(FakeSummarizer):
        def summarize(self, candidate):
            super().summarize(candidate)
            return next((row["summary"] for row in fixture["items"]
                         if row["hn_item_id"] == candidate.story.hn_item_id),
                        "Independent report with substantive details.")

    classifier = FakeClassifier(default_label="core_non_ai")
    deduplicator = ScriptedDeduplicator({second["hn_item_id"]: first["hn_item_id"]})
    ids, records, history = generate(
        tmp_path, stories, deduplicator, summarizer=PublicSummarySummarizer(),
        classifier=classifier,
    )

    expected = [first["hn_item_id"], "101", "102", fifth["hn_item_id"], "103"]
    assert ids["ai"] == expected
    assert history["2026-10-22"] == expected
    assert len(classifier.seen_ids) == len(stories)  # Refill does not reclassify.
    assert len(deduplicator.calls) == 2
    duplicate = records[second["hn_item_id"]]
    assert duplicate["rejection_reason"] == "content_duplicate"
    assert duplicate["content_deduplication"][0]["duplicate_of"] == first["hn_item_id"]
    assert duplicate["content_deduplication"][0]["status"] == "duplicate"
    assert records[fifth["hn_item_id"]]["selected"] is True


def test_cross_section_duplicate_refills_its_own_section(tmp_path):
    deduplicator = ScriptedDeduplicator({"201": "1"})
    outside = [story(str(200 + index), f"History report {index}", points=900 - index * 50)
               for index in range(1, 4)]
    ids, records, history = generate(tmp_path, core_stories(6) + outside, deduplicator)

    assert ids["ai"] == ["1", "2", "3", "4", "5"]
    assert ids["non_ai_hot"] == ["202", "203"]
    assert records["6"]["selected"] is False
    assert "201" not in history["2026-10-22"]
    assert deduplicator.calls[0] == ["1", "2", "3", "4", "5", "201", "202"]
    assert deduplicator.calls[1][-1] == "203"


@pytest.mark.parametrize("failure", [RuntimeError("comparison unavailable"), [],
    [ContentDecision("1", None, "keep"), ContentDecision("2", "1", "duplicate")]])
def test_failed_or_partial_decisions_retain_entire_selection_and_stop_refill(tmp_path, failure):
    deduplicator = ScriptedDeduplicator(failure=failure)
    ids, records, _ = generate(tmp_path, core_stories(6), deduplicator)

    assert ids["ai"] == ["1", "2", "3", "4", "5"]
    assert len(deduplicator.calls) == 1
    assert records["6"]["selected"] is False
    assert all(records[item_id]["content_deduplication"][0]["status"] == "failed"
               for item_id in ids["ai"])


def test_failed_summary_is_retained_and_excluded_from_comparison(tmp_path):
    class OneFailureSummarizer(FakeSummarizer):
        def summarize(self, candidate):
            if candidate.story.hn_item_id == "2":
                raise RuntimeError("summary unavailable")
            return super().summarize(candidate)

    deduplicator = ScriptedDeduplicator()
    ids, records, _ = generate(
        tmp_path, core_stories(6), deduplicator, summarizer=OneFailureSummarizer(),
    )

    assert ids["ai"] == ["1", "2", "3", "4", "5"]
    assert deduplicator.calls == [["1", "3", "4", "5"]]
    assert records["2"]["summary_generation"]["status"] == "failed"
    assert records["2"]["content_deduplication"][0]["status"] == "skipped"


def test_repeated_duplicates_stop_after_three_replacement_candidates(tmp_path):
    deduplicator = ScriptedDeduplicator({item_id: "1" for item_id in ("2", "6", "7", "8")})
    summarizer = FakeSummarizer()
    ids, records, history = generate(
        tmp_path, core_stories(10), deduplicator, summarizer=summarizer,
    )

    assert ids["ai"] == ["1", "3", "4", "5"]
    assert len(summarizer.titles) == 8
    assert len(deduplicator.calls) == 4
    assert all(records[item_id]["rejection_reason"] == "content_duplicate"
               for item_id in ("2", "6", "7", "8"))
    assert records["9"]["summary_generation"]["status"] == "not_attempted"
    assert history["2026-10-22"] == ids["ai"]


def test_refill_excludes_recent_and_under_threshold_candidates(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "recommendation-history.json").write_text(json.dumps({"2026-10-21": ["301"]}))
    stories = core_stories(5) + [
        story("301", "SQLite recent", points=1200),
        story("302", "SQLite tiny", points=1, comments=0),
    ]
    deduplicator = ScriptedDeduplicator({"2": "1"})
    ids, records, _ = generate(tmp_path, stories, deduplicator)

    assert ids["ai"] == ["1", "3", "4", "5"]
    assert len(deduplicator.calls) == 1
    assert records["301"]["rejection_reason"] == "recently_selected"
    assert records["302"]["selected"] is False


def test_later_round_cannot_remove_a_previously_retained_item(tmp_path):
    class ChangingDeduplicator(ScriptedDeduplicator):
        def deduplicate(self, candidates):
            self.duplicate_of = {"2": "1"} if not self.calls else {"3": "1"}
            return super().deduplicate(candidates)

    deduplicator = ChangingDeduplicator()
    ids, records, _ = generate(tmp_path, core_stories(6), deduplicator)

    assert ids["ai"] == ["1", "3", "4", "5", "6"]
    assert records["3"]["content_deduplication"] == [{
        "round": 1, "status": "retained", "duplicate_of": None,
        "reason": "Independent information or uncertain overlap", "error_type": "", "model": "test-content-model",
    }]


def test_later_comparison_failure_keeps_refill_and_previous_removal(tmp_path):
    class FailingRefillDeduplicator(ScriptedDeduplicator):
        def deduplicate(self, candidates):
            if self.calls:
                self.failure = RuntimeError("refill comparison unavailable")
            return super().deduplicate(candidates)

    deduplicator = FailingRefillDeduplicator({"2": "1"})
    ids, records, history = generate(tmp_path, core_stories(8), deduplicator)

    assert ids["ai"] == history["2026-10-22"] == ["1", "3", "4", "5", "6"]
    assert len(deduplicator.calls) == 2
    assert records["2"]["rejection_reason"] == "content_duplicate"
    assert records["6"]["content_deduplication"][0]["status"] == "failed"
    assert records["7"]["selected"] is False


def test_failed_replacement_summary_stops_without_another_comparison(tmp_path):
    class FailedRefillSummarizer(FakeSummarizer):
        def summarize(self, candidate):
            if candidate.story.hn_item_id == "6":
                raise RuntimeError("replacement summary unavailable")
            return super().summarize(candidate)

    deduplicator = ScriptedDeduplicator({"2": "1"})
    ids, records, _ = generate(
        tmp_path, core_stories(8), deduplicator, summarizer=FailedRefillSummarizer(),
    )

    assert ids["ai"] == ["1", "3", "4", "5", "6"]
    assert len(deduplicator.calls) == 1
    assert records["6"]["content_deduplication"][0]["status"] == "skipped"
    assert records["7"]["selected"] is False


def test_prequalified_roundup_reserve_reuses_its_summary_when_promoted(tmp_path):
    from daily_brief.candidates import HNDiscussionResult

    comments = ("I built a local calendar with offline search. " * 12
                + "I built a voxel editor with GPU terrain rendering. " * 12)

    class RoundupClassifier(FakeClassifier):
        def classify(self, candidates):
            candidate = candidates[0]
            self.seen_ids.append(candidate.story.hn_item_id)
            label = "community_roundup" if candidate.content_kind == "article" else "core_non_ai"
            return {candidate.story.hn_item_id: label}

    roundup = story(
        "101", "Ask HN: What are you working on?", points=30, comments=5,
        story_text="Share the projects you are building.",
        url="https://news.ycombinator.com/item?id=101",
    )
    summarizer = FakeSummarizer()
    classifier = RoundupClassifier()
    deduplicator = ScriptedDeduplicator({"2": "1"})
    ids, records, _ = generate(
        tmp_path, core_stories(5) + [roundup], deduplicator,
        summarizer=summarizer, classifier=classifier,
        hn_discussion_fetcher=lambda item_id: HNDiscussionResult(
            text=comments, comments=3, chars=len(comments), requested_items=4, failed_items=0,
        ),
    )

    assert ids["ai"] == ["1", "3", "4", "5", "101"]
    assert summarizer.titles.count(roundup.title) == 1
    assert classifier.seen_ids == ["101", "101"]
    assert records["101"]["content_kind"] == "community_roundup"
    assert deduplicator.calls[1][-1] == "101"
