from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
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
MAX_DISCUSSION_COMMENTS = 24
MAX_DISCUSSION_ITEM_REQUESTS = 40
MAX_DISCUSSION_DEPTH = 3
MAX_DISCUSSION_CHARS = 16_000
MAX_COMMENT_CHARS = 2_000
MAX_DISCUSSION_FAILED_ITEMS = 3
DISCUSSION_REQUEST_TIMEOUT_SECONDS = 8


class RequestFailedError(RuntimeError):
    pass


class HNDiscussionFetchError(RuntimeError):
    def __init__(self, message: str, *, error_code: str):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class HNDiscussionResult:
    text: str
    comments: int
    chars: int
    requested_items: int
    failed_items: int


class _HNTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset({"blockquote", "br", "div", "li", "p", "pre"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line).strip()


def fetch_hn_discussion(
    item_id: str,
    *,
    max_comments: int = MAX_DISCUSSION_COMMENTS,
    max_item_requests: int = MAX_DISCUSSION_ITEM_REQUESTS,
    max_depth: int = MAX_DISCUSSION_DEPTH,
    max_chars: int = MAX_DISCUSSION_CHARS,
    max_comment_chars: int = MAX_COMMENT_CHARS,
    item_fetcher: Callable[[str], object] | None = None,
) -> HNDiscussionResult:
    """Fetch a bounded breadth-first sample from an HN discussion."""
    if not item_id.isdigit() or int(item_id) <= 0:
        raise HNDiscussionFetchError(
            "invalid Hacker News item ID",
            error_code="invalid_item_id",
        )
    if min(
        max_comments,
        max_item_requests,
        max_depth + 1,
        max_chars,
        max_comment_chars,
    ) <= 0:
        raise ValueError("discussion fetch bounds must be positive")

    fetch_item = item_fetcher or _get_discussion_item
    requested_items = 1
    try:
        root = fetch_item(HN_ITEM_URL.format(item_id=item_id))
    except Exception as exc:
        raise HNDiscussionFetchError(
            f"Hacker News story request failed: {exc}",
            error_code="story_request_failed",
        ) from exc
    if (
        not isinstance(root, dict)
        or root.get("type") != "story"
        or str(root.get("id") or "") != item_id
    ):
        raise HNDiscussionFetchError(
            "Hacker News item is not the requested story",
            error_code="invalid_story",
        )

    queue = deque((kid, 0) for kid in _valid_kids(root.get("kids")))
    samples: list[str] = []
    selected_chars = 0
    failed_items = 0
    while (
        queue
        and len(samples) < max_comments
        and requested_items < max_item_requests
        and selected_chars < max_chars
    ):
        comment_id, depth = queue.popleft()
        requested_items += 1
        try:
            comment = fetch_item(HN_ITEM_URL.format(item_id=comment_id))
        except Exception:
            failed_items += 1
            if failed_items >= MAX_DISCUSSION_FAILED_ITEMS:
                break
            continue
        if not isinstance(comment, dict) or comment.get("type") != "comment":
            failed_items += 1
            continue
        if depth < max_depth:
            queue.extend(
                (kid, depth + 1) for kid in _valid_kids(comment.get("kids"))
            )
        if comment.get("dead") or comment.get("deleted"):
            continue
        text = _hn_html_to_text(comment.get("text"))[:max_comment_chars].strip()
        if not text:
            continue
        author = " ".join(str(comment.get("by") or "unknown").split())[:80]
        header = f"[评论 {len(samples) + 1}；层级 {depth}；作者 {author}]\n"
        separator_chars = 2 if samples else 0
        remaining = max_chars - selected_chars - separator_chars
        sample = (header + text)[:remaining].rstrip()
        if len(sample) <= len(header.rstrip()):
            break
        samples.append(sample)
        selected_chars += len(sample) + separator_chars

    discussion_text = "\n\n".join(samples)
    return HNDiscussionResult(
        text=discussion_text,
        comments=len(samples),
        chars=len(discussion_text),
        requested_items=requested_items,
        failed_items=failed_items,
    )


def _valid_kids(value) -> list[int]:
    if not isinstance(value, list):
        return []
    return [kid for kid in value if isinstance(kid, int) and kid > 0]


def _hn_html_to_text(value) -> str:
    if not isinstance(value, str) or not value:
        return ""
    parser = _HNTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return ""
    return parser.text()


def _get_discussion_item(url: str):
    request = Request(url, headers={"User-Agent": "daily-brief/0.1"})
    with urlopen(request, timeout=DISCUSSION_REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


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
    successful_lists = 0
    last_error: RequestFailedError | None = None
    for url in (HN_TOPSTORIES_URL, HN_BESTSTORIES_URL):
        try:
            item_ids.extend(_get_json(url)[:limit_ids])
            successful_lists += 1
        except RequestFailedError as exc:
            last_error = exc
            LOGGER.error(
                "source=hn_official endpoint=%s status=skipped error=%s message=%s",
                urlparse(url).path.rsplit("/", 1)[-1],
                type(exc).__name__,
                exc,
            )

    if successful_lists == 0:
        raise RequestFailedError("hn_official story lists failed") from last_error

    seen: set[int] = set()
    stories: list[Story] = []
    for item_id in item_ids:
        if item_id in seen:
            continue
        seen.add(item_id)

        try:
            item = _get_json(HN_ITEM_URL.format(item_id=item_id))
        except RequestFailedError as exc:
            LOGGER.error(
                "source=hn_official item_id=%s status=skipped error=%s message=%s",
                item_id,
                type(exc).__name__,
                exc,
            )
            continue
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
