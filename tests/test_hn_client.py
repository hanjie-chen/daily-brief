from datetime import datetime
from zoneinfo import ZoneInfo

from daily_brief.hn_client import fetch_algolia_stories, fetch_hot_stories, parse_algolia_hit, parse_hn_item
from daily_brief.time_window import TimeWindow


def test_parse_algolia_hit_to_story():
    hit = {
        "objectID": "123",
        "title": "AI coding agent",
        "url": "https://example.com/post",
        "created_at": "2026-07-08T00:00:00Z",
        "points": 42,
        "num_comments": 7,
        "story_text": "A small demo",
    }

    story = parse_algolia_hit(hit)

    assert story.source == "algolia"
    assert story.hn_item_id == "123"
    assert story.title == "AI coding agent"
    assert story.source_url == "https://example.com/post"
    assert story.hn_discussion_url == "https://news.ycombinator.com/item?id=123"
    assert story.created_at == "2026-07-08T00:00:00Z"
    assert story.points == 42
    assert story.comments == 7
    assert story.story_text == "A small demo"


def test_parse_algolia_hit_uses_discussion_url_when_source_url_missing():
    story = parse_algolia_hit({"objectID": "124", "story_title": "Ask HN", "points": None})

    assert story.title == "Ask HN"
    assert story.source_url == "https://news.ycombinator.com/item?id=124"
    assert story.points == 0
    assert story.comments == 0


def test_parse_hn_item_to_story():
    item = {
        "id": 456,
        "title": "SQLite release",
        "url": "https://sqlite.org/release",
        "time": 1783500000,
        "score": 320,
        "descendants": 80,
    }

    story = parse_hn_item(item)

    assert story.hn_item_id == "456"
    assert story.source == "hn_official"
    assert story.title == "SQLite release"
    assert story.source_url == "https://sqlite.org/release"
    assert story.points == 320
    assert story.comments == 80
    assert story.hn_discussion_url == "https://news.ycombinator.com/item?id=456"
    assert story.created_at == "2026-07-08T08:40:00+00:00"


def test_parse_hn_item_uses_discussion_url_and_defaults_when_fields_missing():
    story = parse_hn_item({"id": 789, "time": 0, "text": "Launch notes"})

    assert story.source_url == "https://news.ycombinator.com/item?id=789"
    assert story.points == 0
    assert story.comments == 0
    assert story.story_text == "Launch notes"


def test_fetch_algolia_stories_pages_through_time_window(monkeypatch):
    seen_urls = []

    def fake_get_json(url):
        seen_urls.append(url)
        if "page=0" in url:
            return {
                "nbPages": 2,
                "hits": [{"objectID": "1", "title": "First", "created_at": "2026-07-07T00:00:00Z"}],
            }
        return {
            "nbPages": 2,
            "hits": [{"objectID": "2", "title": "Second", "created_at": "2026-07-07T01:00:00Z"}],
        }

    monkeypatch.setattr("daily_brief.hn_client._get_json", fake_get_json)
    window = TimeWindow(
        start=datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore")),
        end=datetime(2026, 7, 8, 8, 0, tzinfo=ZoneInfo("Asia/Singapore")),
        date_label="2026-07-08",
    )

    stories = fetch_algolia_stories(window, page_size=50)

    assert [story.hn_item_id for story in stories] == ["1", "2"]
    assert len(seen_urls) == 2
    assert "tags=story" in seen_urls[0]
    assert "numericFilters=created_at_i%3E1783382400%2Ccreated_at_i%3C%3D1783468800" in seen_urls[0]
    assert "hitsPerPage=50" in seen_urls[0]


def test_fetch_hot_stories_dedupes_ids_and_keeps_only_stories(monkeypatch):
    responses = {
        "https://hacker-news.firebaseio.com/v0/topstories.json": [1, 2, 3],
        "https://hacker-news.firebaseio.com/v0/beststories.json": [2, 4],
        "https://hacker-news.firebaseio.com/v0/item/1.json": {"id": 1, "type": "story", "title": "One", "time": 0},
        "https://hacker-news.firebaseio.com/v0/item/2.json": {"id": 2, "type": "comment", "time": 0},
        "https://hacker-news.firebaseio.com/v0/item/4.json": {"id": 4, "type": "story", "title": "Four", "time": 0},
    }
    fetched = []

    def fake_get_json(url):
        fetched.append(url)
        return responses[url]

    monkeypatch.setattr("daily_brief.hn_client._get_json", fake_get_json)

    stories = fetch_hot_stories(limit_ids=2)

    assert [story.hn_item_id for story in stories] == ["1", "4"]
    assert fetched == [
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        "https://hacker-news.firebaseio.com/v0/beststories.json",
        "https://hacker-news.firebaseio.com/v0/item/1.json",
        "https://hacker-news.firebaseio.com/v0/item/2.json",
        "https://hacker-news.firebaseio.com/v0/item/4.json",
    ]
