from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from .models import Story
from .time_window import TimeWindow

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_BESTSTORIES_URL = "https://hacker-news.firebaseio.com/v0/beststories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
HN_DISCUSSION_URL = "https://news.ycombinator.com/item?id={item_id}"

LOGGER = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 20
RETRY_DELAYS_SECONDS = (10, 20)
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)


class RequestFailedError(RuntimeError):
    pass


def fetch_algolia_stories(window: TimeWindow, page_size: int = 100) -> list[Story]:
    start_ts = int(window.start.timestamp())
    end_ts = int(window.end.timestamp())
    stories: list[Story] = []
    page = 0

    while True:
        query = urlencode(
            {
                "tags": "story",
                "numericFilters": f"created_at_i>{start_ts},created_at_i<={end_ts}",
                "hitsPerPage": page_size,
                "page": page,
            }
        )
        payload = _get_json(f"{ALGOLIA_URL}?{query}")
        stories.extend(parse_algolia_hit(hit) for hit in payload.get("hits", []))

        if page >= int(payload.get("nbPages") or 0) - 1:
            break
        page += 1

    return stories


def fetch_hot_stories(limit_ids: int = 100) -> list[Story]:
    item_ids = []
    for url in (HN_TOPSTORIES_URL, HN_BESTSTORIES_URL):
        item_ids.extend(_get_json(url)[:limit_ids])

    seen: set[int] = set()
    stories: list[Story] = []
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)

        item = _get_json(HN_ITEM_URL.format(item_id=item_id))
        if item and item.get("type") == "story":
            stories.append(parse_hn_item(item))

    return stories


def parse_algolia_hit(hit: dict) -> Story:
    item_id = str(hit.get("objectID") or "")
    discussion_url = HN_DISCUSSION_URL.format(item_id=item_id)
    return Story(
        source="algolia",
        hn_item_id=item_id,
        title=hit.get("title") or hit.get("story_title") or "",
        source_url=hit.get("url") or discussion_url,
        hn_discussion_url=discussion_url,
        created_at=hit.get("created_at") or "",
        points=int(hit.get("points") or 0),
        comments=int(hit.get("num_comments") or 0),
        story_text=hit.get("story_text") or "",
    )


def parse_hn_item(item: dict) -> Story:
    item_id = str(item.get("id") or "")
    discussion_url = HN_DISCUSSION_URL.format(item_id=item_id)
    created_at = datetime.fromtimestamp(int(item.get("time") or 0), tz=UTC).isoformat()
    return Story(
        source="hn_official",
        hn_item_id=item_id,
        title=item.get("title") or "",
        source_url=item.get("url") or discussion_url,
        hn_discussion_url=discussion_url,
        created_at=created_at,
        points=int(item.get("score") or 0),
        comments=int(item.get("descendants") or 0),
        story_text=item.get("text") or "",
    )


def _source_name(url: str) -> str:
    return "algolia" if urlparse(url).hostname == "hn.algolia.com" else "hn_official"


def _get_json(
    url: str,
    *,
    opener=urlopen,
    sleep: Callable[[float], None] = time.sleep,
):
    source = _source_name(url)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = Request(url, headers={"User-Agent": "daily-brief/0.1"})
            with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            if attempt == MAX_ATTEMPTS:
                LOGGER.error(
                    "source=%s attempt=%d/%d status=failed error=%s message=%s",
                    source,
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                raise RequestFailedError(
                    f"{source} request failed after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc

            delay = RETRY_DELAYS_SECONDS[attempt - 1]
            LOGGER.warning(
                "source=%s attempt=%d/%d error=%s message=%s retry_in=%ss",
                source,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
                exc,
                delay,
            )
            sleep(delay)
