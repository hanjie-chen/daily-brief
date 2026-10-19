import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from daily_brief.candidates import HNStoryFetchError as FacadeHNStoryFetchError
from daily_brief.candidates import fetch_hn_story as facade_fetch_hn_story
from daily_brief.candidates.hn_client import (
    HN_BESTSTORIES_URL,
    HNDiscussionFetchError,
    HNStoryFetchError,
    HN_ITEM_URL,
    HN_TOPSTORIES_URL,
    RequestFailedError,
    _get_json,
    fetch_algolia_stories,
    fetch_hn_discussion,
    fetch_hn_story,
    fetch_hot_stories,
    parse_algolia_hit,
    parse_hn_item,
)
from daily_brief.time_window import TimeWindow


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def test_get_json_succeeds_without_sleeping():
    sleeps = []

    result = _get_json(
        "https://hn.algolia.com/test",
        opener=lambda request, timeout: FakeResponse(b'{"ok": true}'),
        sleep=sleeps.append,
    )

    assert result == {"ok": True}
    assert sleeps == []


def test_get_json_retries_with_configured_backoff(caplog):
    outcomes = [TimeoutError("first"), TimeoutError("second"), FakeResponse(b'{"ok": true}')]
    sleeps = []

    def opener(request, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with caplog.at_level(logging.WARNING, logger="daily_brief.candidates.hn_client"):
        result = _get_json(
            "https://hn.algolia.com/test",
            opener=opener,
            sleep=sleeps.append,
        )

    assert result == {"ok": True}
    assert sleeps == [10, 20]
    assert "source=algolia attempt=1/3" in caplog.text
    assert "retry_in=10s" in caplog.text
    assert "source=algolia attempt=2/3" in caplog.text
    assert "retry_in=20s" in caplog.text


def test_get_json_raises_after_exactly_three_attempts(caplog):
    attempts = []
    sleeps = []

    def opener(request, timeout):
        attempts.append(timeout)
        raise TimeoutError(f"failure {len(attempts)}")

    with caplog.at_level(logging.ERROR, logger="daily_brief.candidates.hn_client"):
        with pytest.raises(RequestFailedError, match="algolia request failed after 3 attempts") as raised:
            _get_json(
                "https://hn.algolia.com/test",
                opener=opener,
                sleep=sleeps.append,
            )

    assert attempts == [20, 20, 20]
    assert sleeps == [10, 20]
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert str(raised.value.__cause__) == "failure 3"
    assert "source=algolia attempt=3/3 status=failed" in caplog.text


def test_scheduled_collection_recovers_current_page_without_reloading_previous(monkeypatch):
    from datetime import datetime
    from daily_brief.candidates import hn_client
    from daily_brief.network_recovery import NetworkRecovery
    from daily_brief.time_window import daily_window
    from urllib.parse import parse_qs, urlparse

    pages, waits, short_waits = [], [], []
    get_json = hn_client._get_json

    def opener(request, timeout):
        page = int(parse_qs(urlparse(request.full_url).query)["page"][0])
        pages.append(page)
        if page == 1 and pages.count(1) <= 3:
            raise TimeoutError()
        return FakeResponse(json.dumps({"hits": [], "nbPages": 2}).encode())

    monkeypatch.setattr(hn_client, "_get_json", lambda url: get_json(url, opener=opener, sleep=short_waits.append))
    with NetworkRecovery(sleeper=waits.append, max_pauses=1):
        assert hn_client.fetch_algolia_stories(daily_window(datetime(2026, 10, 12, 8))) == []
    assert pages == [0, 1, 1, 1, 1]
    assert short_waits == [10, 20]
    assert waits == [600]


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


def test_fetch_hn_story_fetches_one_valid_story_without_collection():
    requested = []

    def fetch(url):
        requested.append(url)
        return {
            "id": 456,
            "type": "story",
            "title": "Show HN: A small project",
            "text": "The current <i>submission</i> text.",
            "url": "https://example.com/project",
            "time": 0,
        }

    story = fetch_hn_story("456", item_fetcher=fetch)

    assert requested == [HN_ITEM_URL.format(item_id="456")]
    assert story.hn_item_id == "456"
    assert story.title == "Show HN: A small project"
    assert story.story_text == "The current <i>submission</i> text."


def test_fetch_hn_story_is_available_from_candidates_facade():
    assert facade_fetch_hn_story is fetch_hn_story
    assert FacadeHNStoryFetchError is HNStoryFetchError


@pytest.mark.parametrize("item_id", ["", "0", "-1", "123x", "１２３"])
def test_fetch_hn_story_rejects_invalid_item_id_without_request(item_id):
    with pytest.raises(HNStoryFetchError) as raised:
        fetch_hn_story(item_id, item_fetcher=lambda url: pytest.fail("unexpected request"))

    assert raised.value.error_code == "invalid_item_id"


def test_fetch_hn_story_wraps_single_request_failure():
    with pytest.raises(HNStoryFetchError) as raised:
        fetch_hn_story(
            "456",
            item_fetcher=lambda url: (_ for _ in ()).throw(TimeoutError("offline")),
        )

    assert raised.value.error_code == "story_request_failed"
    assert isinstance(raised.value.__cause__, TimeoutError)


@pytest.mark.parametrize(
    ("item", "error_code"),
    [
        ({"id": 456, "type": "comment"}, "invalid_story"),
        ({"id": 457, "type": "story"}, "invalid_story"),
        ({"id": 456, "type": "story", "dead": True}, "unavailable_story"),
        ({"id": 456, "type": "story", "deleted": True}, "unavailable_story"),
        ({"id": 456, "type": "story", "title": 4}, "invalid_story_fields"),
        ({"id": 456, "type": "story", "title": " ", "text": ""}, "invalid_story_fields"),
        ({"id": 456, "type": "story", "title": "Valid", "text": 4}, "invalid_story_fields"),
        ({"id": 456, "type": "story", "title": "Valid", "url": 4}, "invalid_story_fields"),
        ({"id": 456, "type": "story", "title": "Valid", "url": "   "}, "invalid_story_fields"),
    ],
)
def test_fetch_hn_story_rejects_invalid_or_unavailable_payloads(item, error_code):
    with pytest.raises(HNStoryFetchError) as raised:
        fetch_hn_story("456", item_fetcher=lambda url: item)

    assert raised.value.error_code == error_code


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

    monkeypatch.setattr("daily_brief.candidates.hn_client._get_json", fake_get_json)
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

    monkeypatch.setattr("daily_brief.candidates.hn_client._get_json", fake_get_json)

    stories = fetch_hot_stories(limit_ids=2)

    assert [story.hn_item_id for story in stories] == ["1", "4"]
    assert fetched == [
        "https://hacker-news.firebaseio.com/v0/topstories.json",
        "https://hacker-news.firebaseio.com/v0/beststories.json",
        "https://hacker-news.firebaseio.com/v0/item/1.json",
        "https://hacker-news.firebaseio.com/v0/item/2.json",
        "https://hacker-news.firebaseio.com/v0/item/4.json",
    ]


@pytest.mark.parametrize("failed_list_url", [HN_TOPSTORIES_URL, HN_BESTSTORIES_URL])
def test_fetch_hot_stories_keeps_successful_list_when_other_list_fails(monkeypatch, failed_list_url):
    successful_list_url = HN_BESTSTORIES_URL if failed_list_url == HN_TOPSTORIES_URL else HN_TOPSTORIES_URL

    def fake_get_json(url):
        if url == failed_list_url:
            raise RequestFailedError("list unavailable")
        if url == successful_list_url:
            return [1]
        return {"id": 1, "type": "story", "title": "kept", "time": 0}

    monkeypatch.setattr("daily_brief.candidates.hn_client._get_json", fake_get_json)

    stories = fetch_hot_stories()

    assert [story.hn_item_id for story in stories] == ["1"]


def test_fetch_hot_stories_skips_failed_item_and_keeps_later_items(monkeypatch, caplog):
    def fake_get_json(url):
        if url == HN_TOPSTORIES_URL:
            return [1, 2]
        if url == HN_BESTSTORIES_URL:
            return []
        if url == HN_ITEM_URL.format(item_id=1):
            raise RequestFailedError("item unavailable")
        return {"id": 2, "type": "story", "title": "kept", "time": 0}

    monkeypatch.setattr("daily_brief.candidates.hn_client._get_json", fake_get_json)

    with caplog.at_level(logging.ERROR, logger="daily_brief.candidates.hn_client"):
        stories = fetch_hot_stories()

    assert [story.hn_item_id for story in stories] == ["2"]
    assert "item_id=1 status=skipped" in caplog.text


def test_fetch_hot_stories_raises_when_both_lists_fail(monkeypatch):
    def fake_get_json(url):
        raise RequestFailedError(f"unavailable: {url}")

    monkeypatch.setattr("daily_brief.candidates.hn_client._get_json", fake_get_json)

    with pytest.raises(RequestFailedError, match="story lists failed"):
        fetch_hot_stories()


def test_fetch_hn_discussion_collects_bounded_breadth_first_plain_text():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2, 3]},
        "2": {
            "id": 2,
            "type": "comment",
            "by": "alice",
            "text": "First <i>view</i>.<p>Second line &amp; detail.",
            "kids": [4],
        },
        "3": {
            "id": 3,
            "type": "comment",
            "by": "bob",
            "text": "Another viewpoint.",
        },
        "4": {
            "id": 4,
            "type": "comment",
            "by": "carol",
            "text": "A nested reply.",
        },
    }
    requested = []

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        requested.append(item_id)
        return responses[item_id]

    result = fetch_hn_discussion("1", item_fetcher=fetch)

    assert requested == ["1", "2", "3", "4"]
    assert result.comments == 3
    assert result.requested_items == 4
    assert result.failed_items == 0
    assert result.top_level_comments == 2
    assert result.reply_comments == 1
    assert result.stop_reason == "queue_exhausted"
    assert "[评论 1；HN 评论 ID 2；层级 0；作者 alice]" in result.text
    assert "First view.\nSecond line & detail." in result.text
    assert result.text.index("作者 bob") < result.text.index("作者 carol")
    assert "<i>" not in result.text


def test_fetch_hn_discussion_labels_replies_with_the_included_parent():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2]},
        "2": {
            "id": 2,
            "type": "comment",
            "by": "alice",
            "text": "The parent explains the trade-off.",
            "kids": [3],
        },
        "3": {
            "id": 3,
            "type": "comment",
            "by": "bob",
            "text": "The reply adds an implementation detail.",
        },
    }

    def fetch(url):
        return responses[url.rsplit("/", 1)[-1].split(".", 1)[0]]

    result = fetch_hn_discussion("1", item_fetcher=fetch)

    assert "The parent explains the trade-off." in result.text
    assert (
        "[评论 2；HN 评论 ID 3；层级 1；回复 HN 评论 ID 2；作者 bob]"
        in result.text
    )
    assert "The reply adds an implementation detail." in result.text


@pytest.mark.parametrize(
    ("parent", "max_comment_chars"),
    [
        ({"deleted": True, "kids": [3]}, 2_000),
        ({"text": "Parent is longer than the per-comment budget.", "kids": [3]}, 10),
    ],
)
def test_fetch_hn_discussion_skips_replies_without_complete_parent_context(
    parent,
    max_comment_chars,
):
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2]},
        "2": {"id": 2, "type": "comment", **parent},
        "3": {"id": 3, "type": "comment", "text": "Orphan reply."},
    }
    requested = []

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        requested.append(item_id)
        return responses[item_id]

    result = fetch_hn_discussion(
        "1", max_comment_chars=max_comment_chars, item_fetcher=fetch
    )

    assert requested == ["1", "2"]
    assert "Orphan reply." not in result.text


def test_fetch_hn_discussion_covers_later_top_level_answers_before_long_replies():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2, 3, 4]},
        "2": {"id": 2, "type": "comment", "text": "First answer.", "kids": [5]},
        "3": {"id": 3, "type": "comment", "text": "Second answer."},
        "4": {"id": 4, "type": "comment", "text": "Third answer."},
        "5": {"id": 5, "type": "comment", "text": "Reply one.", "kids": [6]},
        "6": {"id": 6, "type": "comment", "text": "Reply two.", "kids": [7]},
        "7": {"id": 7, "type": "comment", "text": "Reply three."},
    }
    requested = []

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        requested.append(item_id)
        return responses[item_id]

    result = fetch_hn_discussion(
        "1", max_item_requests=4, item_fetcher=fetch
    )

    assert requested == ["1", "2", "3", "4"]
    assert result.comments == 3
    assert "First answer." in result.text
    assert "Second answer." in result.text
    assert "Third answer." in result.text
    assert "Reply one." not in result.text


def test_fetch_hn_discussion_skips_reply_to_deleted_comment():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2, 3]},
        "2": {"id": 2, "type": "comment", "deleted": True, "kids": [4]},
        "3": {"id": 3, "type": "comment", "text": "Visible top-level answer."},
        "4": {"id": 4, "type": "comment", "text": "Reply to deleted answer."},
    }
    requested = []

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        requested.append(item_id)
        return responses[item_id]

    result = fetch_hn_discussion(
        "1", max_item_requests=4, item_fetcher=fetch
    )

    assert requested == ["1", "2", "3"]
    assert result.comments == 1
    assert "Visible top-level answer." in result.text
    assert "Reply to deleted answer." not in result.text


def test_fetch_hn_discussion_skips_bad_items_and_obeys_request_bound():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2, 3, 4]},
        "2": {"id": 2, "type": "comment", "deleted": True, "kids": [5]},
        "3": RuntimeError("unavailable"),
        "4": {"id": 4, "type": "comment", "text": "not reached"},
        "5": {"id": 5, "type": "comment", "text": "also not reached"},
    }

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        value = responses[item_id]
        if isinstance(value, Exception):
            raise value
        return value

    result = fetch_hn_discussion(
        "1",
        max_item_requests=3,
        item_fetcher=fetch,
    )

    assert result.text == ""
    assert result.comments == 0
    assert result.requested_items == 3
    assert result.failed_items == 1


@pytest.mark.parametrize("item_id", ["", "abc", "0", "-1"])
def test_fetch_hn_discussion_rejects_invalid_item_id(item_id):
    with pytest.raises(HNDiscussionFetchError) as raised:
        fetch_hn_discussion(item_id, item_fetcher=lambda url: {})

    assert raised.value.error_code == "invalid_item_id"


def test_fetch_hn_discussion_requires_matching_story_root():
    with pytest.raises(HNDiscussionFetchError) as raised:
        fetch_hn_discussion(
            "1",
            item_fetcher=lambda url: {"id": 2, "type": "comment"},
        )

    assert raised.value.error_code == "invalid_story"


def test_fetch_hn_discussion_caps_complete_material():
    def fetch(url):
        if url == HN_ITEM_URL.format(item_id=1):
            return {"id": 1, "type": "story", "kids": [2]}
        return {"id": 2, "type": "comment", "by": "alice", "text": "x" * 500}

    result = fetch_hn_discussion("1", max_chars=100, item_fetcher=fetch)

    assert result.comments == 1
    assert result.chars == 100
    assert len(result.text) == 100
    assert result.stop_reason == "char_limit"


@pytest.mark.parametrize(("top_count", "expected_top", "expected_replies"), [
    (28, 28, 11),
    (45, 39, 0),
])
def test_discussion_uses_request_budget_beyond_24_comments(
    top_count, expected_top, expected_replies,
):
    top_ids = list(range(2, 2 + top_count))
    responses = {"1": {"id": 1, "type": "story", "kids": top_ids}}
    for index, item_id in enumerate(top_ids):
        responses[str(item_id)] = {
            "id": item_id, "type": "comment",
            "text": (
                "Security notice: stored provider keys may be exposed."
                if index == 25 else "A short independent observation."
            ),
            "kids": [100 + index],
        }
        responses[str(100 + index)] = {
            "id": 100 + index, "type": "comment", "text": "A reply with context.",
        }
    requested = []

    def fetch(url):
        item_id = url.rsplit("/", 1)[-1].split(".", 1)[0]
        requested.append(int(item_id))
        return responses[item_id]

    result = fetch_hn_discussion("1", item_fetcher=fetch)

    assert result.comments == 39
    assert result.requested_items == len(requested) == 40
    assert result.top_level_comments == expected_top
    assert result.reply_comments == expected_replies
    assert result.stop_reason == "request_limit"
    assert "stored provider keys may be exposed" in result.text
    assert result.chars == len(result.text) < 16_000
    assert requested == [1, *top_ids[:expected_top], *range(100, 100 + expected_replies)]


@pytest.mark.parametrize("bad_item", [None, {"type": "story"}, RuntimeError("offline")])
def test_discussion_stops_at_failure_limit_and_retains_partial_material(bad_item):
    requested = []

    def fetch(url):
        item_id = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        requested.append(item_id)
        if item_id == 1:
            return {"id": 1, "type": "story", "kids": [2, 3, 4, 5, 6]}
        if item_id == 2:
            return {"id": 2, "type": "comment", "text": "Useful material."}
        if isinstance(bad_item, Exception):
            raise bad_item
        return bad_item

    result = fetch_hn_discussion("1", max_item_requests=5, item_fetcher=fetch)

    assert requested == [1, 2, 3, 4, 5]
    assert result.failed_items == 3
    assert result.comments == result.top_level_comments == 1
    assert result.reply_comments == 0
    assert "Useful material." in result.text
    assert result.stop_reason == "failure_limit"


def test_discussion_deleted_and_empty_comments_still_spend_request_budget():
    responses = {
        "1": {"id": 1, "type": "story", "kids": [2, 3, 4]},
        "2": {"id": 2, "type": "comment", "deleted": True},
        "3": {"id": 3, "type": "comment", "text": " "},
        "4": {"id": 4, "type": "comment", "text": "Not reached."},
    }
    result = fetch_hn_discussion(
        "1", max_item_requests=3,
        item_fetcher=lambda url: responses[url.rsplit("/", 1)[-1].split(".", 1)[0]],
    )
    assert result.requested_items == 3
    assert result.comments == result.top_level_comments == result.reply_comments == 0
    assert result.failed_items == 0
    assert result.stop_reason == "request_limit"


@pytest.mark.parametrize("kids", [[], [2]])
def test_discussion_root_only_budget_distinguishes_empty_queue(kids):
    result = fetch_hn_discussion(
        "1", max_item_requests=1,
        item_fetcher=lambda url: {"id": 1, "type": "story", "kids": kids},
    )
    assert result.requested_items == 1
    assert result.comments == 0
    assert result.stop_reason == ("request_limit" if kids else "queue_exhausted")


@pytest.mark.parametrize("max_chars", [1, 39])
def test_discussion_reports_char_limit_even_if_last_item_does_not_fit(max_chars):
    def fetch(url):
        if url == HN_ITEM_URL.format(item_id=1):
            return {"id": 1, "type": "story", "kids": [2]}
        return {"id": 2, "type": "comment", "by": "a", "text": "one two three four"}

    result = fetch_hn_discussion("1", max_chars=max_chars, item_fetcher=fetch)
    assert result.stop_reason == "char_limit"
    assert result.chars <= max_chars
    assert result.requested_items == 2


def test_discussion_preserves_maximum_reply_depth():
    requested = []

    def fetch(url):
        item_id = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        requested.append(item_id)
        return {"id": item_id, "type": "story" if item_id == 1 else "comment",
                "text": "Complete context.", "kids": [item_id + 1]}

    result = fetch_hn_discussion("1", item_fetcher=fetch)
    assert requested == [1, 2, 3, 4, 5]
    assert result.top_level_comments == 1
    assert result.reply_comments == 3
    assert result.stop_reason == "queue_exhausted"


@pytest.mark.parametrize("spare_chars", [0, 1, 2, 3])
def test_discussion_separator_cannot_overrun_remaining_character_budget(spare_chars):
    def fetch(url):
        item_id = int(url.rsplit("/", 1)[-1].split(".", 1)[0])
        if item_id == 1:
            return {"id": 1, "type": "story", "kids": [2, 3]}
        return {"id": item_id, "type": "comment", "by": "a", "text": "Complete."}

    first_only = fetch_hn_discussion("1", max_item_requests=2, item_fetcher=fetch)
    limit = first_only.chars + spare_chars
    result = fetch_hn_discussion("1", max_chars=limit, item_fetcher=fetch)

    assert result.text == first_only.text
    assert result.comments == 1
    assert result.chars <= limit
    assert result.stop_reason == "char_limit"


def test_discussion_exact_limits_with_no_remaining_nodes_are_queue_exhaustion():
    def fetch(url):
        if url == HN_ITEM_URL.format(item_id=1):
            return {"id": 1, "type": "story", "kids": [2]}
        return {"id": 2, "type": "comment", "text": "Complete."}

    complete = fetch_hn_discussion("1", item_fetcher=fetch)
    result = fetch_hn_discussion(
        "1", max_chars=complete.chars, max_item_requests=2, item_fetcher=fetch,
    )
    assert result.text == complete.text
    assert result.stop_reason == "queue_exhausted"
