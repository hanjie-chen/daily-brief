"""Classification uses the same recovered evidence later supplied to summaries."""
import time
from dataclasses import replace

import pytest

from daily_brief.article_fetcher import ArticleFetchResult, DEFAULT_FETCH_POLICY
from daily_brief.generation.classification import classify_and_select_candidates
from daily_brief.generation.summaries import summarize_selected_candidates
from daily_brief.models import Candidate
from daily_brief.recovery import AlternateReportingCandidate, SyndicatedCandidate
from fakes import (
    CapturingSummarizer, FakeAlternateReportingFinder, FakeClassifier,
    FakeSyndicatedFinder, alternate_reporting_body, anthropic_nytimes_story,
    origin_block_failure, reuters_story_url, story, verified_reuters_copy_body,
    yahoo_anthropic_url, yahoo_story_url,
)
from test_same_article_pipeline import BODY, COPY, WINDOW, Finder, candidate, recovered


@pytest.mark.parametrize("route", ["jina", "wayback", "same_article", "syndicated", "alternate"])
def test_recovered_body_is_classified_then_reused_with_original_identity(route):
    item = candidate()
    body, target = BODY, COPY
    same, syndicated, alternate = Finder(), FakeSyndicatedFinder([]), FakeAlternateReportingFinder([])
    if route == "same_article":
        same = Finder([COPY])
    elif route == "syndicated":
        item = Candidate(story("123", "Nvidia scales back OpenAI data center guarantee", url=reuters_story_url()))
        body, target = verified_reuters_copy_body(), yahoo_story_url()
        syndicated = FakeSyndicatedFinder([SyndicatedCandidate(item.story.title, target)])
    elif route == "alternate":
        item = Candidate(anthropic_nytimes_story())
        body, target = alternate_reporting_body(), yahoo_anthropic_url()
        alternate = FakeAlternateReportingFinder([AlternateReportingCandidate(item.story.title, target)])
    item.story = replace(item.story, points=500)
    item.score = 10
    original = item.story.source_url
    fetches, discussion_calls = [], []

    def fetch(url, **kwargs):
        fetches.append(url)
        assert kwargs["policy"] is DEFAULT_FETCH_POLICY
        assert kwargs["wayback_not_after"] == WINDOW.end
        if route in {"jina", "wayback"}:
            return ArticleFetchResult(body, route, fallback_reason="cloudflare_challenge",
                                      material_origin="archived_copy" if route == "wayback" else "original")
        if url == original:
            raise origin_block_failure("cloudflare_challenge", method="wayback")
        if route == "same_article":
            return recovered()
        return ArticleFetchResult(body, "direct", retrieved_url=url)

    class Classifier(FakeClassifier):
        def classify(self, items):
            assert items[0].story.fetched_text == body.strip()
            assert items[0].story.source_url == original
            assert items[0].discussion_text == ""
            assert discussion_calls == []
            return super().classify(items)

    from daily_brief.candidates import HNDiscussionResult
    def discussion(item_id):
        discussion_calls.append(item_id)
        return HNDiscussionResult("", 0, 0, 1, 0)

    summarizer = CapturingSummarizer()
    selection = classify_and_select_candidates(
        [item], Classifier(), fetch, syndicated, alternate, WINDOW, time.monotonic,
        summary_client=summarizer, hn_discussion_fetcher=discussion, same_article_finder=same,
    )
    assert selection.selected_hot_items == [item]
    assert summarizer.fetched_texts == []
    assert item.article_retrieval.material_origin == {
        "jina": "original", "wayback": "archived_copy", "same_article": "same_article",
        "syndicated": "syndicated_copy", "alternate": "alternate_reporting",
    }[route]
    expected_fetches = [original] if route in {"jina", "wayback"} else [original, target]
    assert fetches == expected_fetches
    summarize_selected_candidates(
        [], selection.selected_hot_items, summarizer, fetch, discussion,
        syndicated, alternate, WINDOW, same_article_finder=same,
    )
    assert fetches == expected_fetches
    assert summarizer.fetched_texts == [body.strip()]
    assert discussion_calls == [item.story.hn_item_id]
    assert item.story.source_url == original


def test_failed_recovery_still_spends_inspection_slot_without_using_comments(monkeypatch):
    from daily_brief.generation import classification
    monkeypatch.setattr(classification, "EXPLORATION_CLASSIFIER_MAX_CANDIDATES", 1)
    items = [Candidate(story(str(i), "Outside story", points=500), score=10-i) for i in range(2)]
    fetches = []
    classifier = FakeClassifier()

    def fail(url, **kwargs):
        fetches.append(url)
        raise origin_block_failure("cloudflare_challenge", method="wayback")

    def forbidden(*args):
        pytest.fail("failed external classification must not use HN comments")

    selection = classify_and_select_candidates(
        items, classifier, fail, FakeSyndicatedFinder([]), FakeAlternateReportingFinder([]),
        WINDOW, time.monotonic, summary_client=CapturingSummarizer(),
        hn_discussion_fetcher=forbidden, same_article_finder=Finder(),
    )
    assert fetches == [items[0].story.source_url]
    assert classifier.seen_ids == []
    assert items[0].rejection_reason == "topic_unknown"
    assert items[1].topic_route == "not_evaluated"
    assert selection.selected_hot_items == []
