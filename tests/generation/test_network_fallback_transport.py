"""Observe real fallback transport faults before later failures hide them."""

from email.message import Message
from functools import partial
from io import BytesIO
import errno
from urllib.error import HTTPError

import pytest

from daily_brief.article_fetcher import fetch_article
from daily_brief.models import Candidate
from daily_brief.network_recovery import NetworkRecovery
from daily_brief.generation.summaries import summarize_selected_candidates
from daily_brief.candidates import HNDiscussionResult
from daily_brief.time_window import daily_window
from fakes import FakeSummarizer, story


class Response:
    def __init__(self, url, payload, content_type, *, read_timeout=False):
        self.url = url
        self.payload = payload
        self.read_timeout = read_timeout
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def geturl(self):
        return self.url

    def read(self, size):
        if self.read_timeout:
            raise OSError(errno.ENETDOWN, "Local network unavailable")
        return self.payload[:size]


@pytest.mark.parametrize("failure_stage", ["open", "read"])
def test_jina_network_failure_survives_missing_archive_and_resumes_current_item(
    monkeypatch, failure_stage,
):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    first = Candidate(story("1", "Rust release"))
    second = Candidate(story("2", "Python release"))
    requests, waits = [], []

    def opener(request, timeout):
        url = request.full_url
        requests.append(url)
        if url == second.story.source_url and requests.count(url) == 1:
            raise HTTPError(url, 403, "Forbidden", {}, BytesIO())
        if url.startswith("https://r.jina.ai/"):
            if failure_stage == "open":
                raise OSError(errno.ENETDOWN, "Local network unavailable")
            return Response(url, b"", "application/json", read_timeout=True)
        if url.startswith("https://web.archive.org/cdx/search/cdx?"):
            return Response(url, b"[]", "application/json")
        return Response(url, b"Recovered source facts.", "text/plain")

    def resolver(host, port, type):
        return [(2, type, 6, "", ("93.184.216.34", port))]

    summarizer = FakeSummarizer()
    fetcher = partial(fetch_article, opener=opener, resolver=resolver)
    with NetworkRecovery(sleeper=waits.append, max_pauses=1) as recovery:
        summarize_selected_candidates(
            [first, second], [], summarizer, fetcher,
            lambda _: HNDiscussionResult("", 0, 0, 1, 0),
            None, None, daily_window(),
        )

    assert waits == [600]
    assert not recovery.exhausted
    assert requests[0] == first.story.source_url
    assert requests[1:3] == [second.story.source_url, f"https://r.jina.ai/{second.story.source_url}"]
    assert requests[3].startswith("https://web.archive.org/cdx/search/cdx?")
    assert requests[4:] == [second.story.source_url]
    assert summarizer.titles == [first.story.title, second.story.title]
    assert all(candidate.summary_status == "success" for candidate in (first, second))
