from datetime import date, datetime

import pytest

from daily_brief.config import TIMEZONE
from daily_brief.keyword_evaluation import collect_corpus, evaluate_corpus
from daily_brief.models import Story


def test_collect_corpus_uses_inclusive_production_brief_windows():
    captured = []

    def fetcher(window):
        captured.append(window)
        return [
            Story(
                source="algolia",
                hn_item_id="1",
                title="A title",
                source_url="https://example.com/article",
                hn_discussion_url="https://news.ycombinator.com/item?id=1",
                created_at="2026-08-20T00:00:00Z",
                points=1,
                comments=0,
                story_text="A self-post",
            )
        ]

    corpus = collect_corpus(
        date(2026, 8, 17),
        date(2026, 8, 30),
        fetcher=fetcher,
    )

    assert captured[0].start == datetime(2026, 8, 16, 8, tzinfo=TIMEZONE)
    assert captured[-1].end == datetime(2026, 8, 30, 8, tzinfo=TIMEZONE)
    assert len(captured) == 14
    assert corpus["stories"][0] == {
        "hn_item_id": "1",
        "title": "A title",
        "story_text": "A self-post",
        "url": "https://example.com/article",
        "created_at": "2026-08-20T00:00:00Z",
    }


def test_collect_corpus_rejects_reversed_dates():
    with pytest.raises(ValueError, match="first brief date"):
        collect_corpus(date(2026, 8, 30), date(2026, 8, 17))


def test_evaluate_corpus_uses_title_story_text_and_url_inputs():
    corpus = {
        "schema_version": 1,
        "first_brief_date": "2026-08-30",
        "last_brief_date": "2026-08-30",
        "window_start": "2026-08-29T08:00:00+08:00",
        "window_end": "2026-08-30T08:00:00+08:00",
        "stories": [
            {
                "hn_item_id": "1",
                "title": "Claude release",
                "story_text": "",
                "url": "https://example.com/release",
                "created_at": "2026-08-29T00:00:00Z",
            },
            {
                "hn_item_id": "2",
                "title": "Ask HN: ordinary title",
                "story_text": "How should I use OpenAI?",
                "url": "https://news.ycombinator.com/item?id=2",
                "created_at": "2026-08-29T01:00:00Z",
            },
            {
                "hn_item_id": "3",
                "title": "An ordinary release",
                "story_text": "",
                "url": "https://example.com/developer/tools",
                "created_at": "2026-08-29T02:00:00Z",
            },
        ],
    }

    report = evaluate_corpus(corpus)

    assert report["keyword_counts"]["Claude"] == 1
    assert report["keyword_counts"]["OpenAI"] == 1
    assert report["keyword_counts"]["developer tools"] == 1
    assert [hit["hn_item_id"] for hit in report["hits"]] == ["1", "2", "3"]
