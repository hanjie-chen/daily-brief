"""Run-level regressions for optional article failures versus shared outages."""

import errno
import json
import socket
from collections import Counter
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

import pytest

from daily_brief import daily_run
from daily_brief.article_fetcher import fetch_article
from daily_brief.candidates import HNDiscussionResult
from daily_brief.generation import material, pipeline
from daily_brief.network_recovery import NetworkRecovery, recoverable_network
from daily_brief.output import PublishResult
from fakes import FakeModelBackend, story


class ArticleResponse:
    def __init__(self, url):
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, amount):
        return b"<html><body><article>Concrete article facts about computer systems.</article></body></html>"[:amount]


@pytest.fixture
def scheduled_run(monkeypatch, tmp_path):
    elapsed = [0]
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        elapsed[0] += seconds

    checks = []
    outage_error = [None]

    def check_connection():
        checks.append(elapsed[0])
        if outage_error[0] is not None and elapsed[0] < 900:
            raise outage_error[0]()

    recovery = NetworkRecovery(sleeper=sleep, connectivity_check=check_connection)
    monkeypatch.setattr(daily_run, "NetworkRecovery", lambda: recovery)
    monkeypatch.setattr(daily_run, "daily_window", lambda: SimpleNamespace(date_label="2026-10-12"))
    stories = [
        story(str(i), f"Interesting discovery number {i}", points=500 - i, comments=80,
              url=f"https://site{i}.example/article")
        for i in range(1, 6)
    ]
    monkeypatch.setattr(pipeline, "fetch_algolia_stories", lambda window: stories)
    monkeypatch.setattr(pipeline, "fetch_hot_stories", lambda: [])
    monkeypatch.setattr(material, "fetch_hn_discussion", lambda item_id: HNDiscussionResult(
        text="", comments=0, chars=0, requested_items=1, failed_items=0,
    ))
    backend = FakeModelBackend()
    backend.default_label = "core_non_ai"
    published = []

    def publish(**kwargs):
        payload = json.loads((kwargs["brief_dir"] / "2026-10-12.json").read_text())
        published.append(payload)
        return PublishResult(1, 0)

    monkeypatch.setattr(daily_run, "publish_brief", publish)
    request_counts = Counter()

    def install_transport(*, article_error=None, local_error=None, isolated_dns_error=None,
                          outage_stage="resolver"):
        outage_error[0] = local_error

        def resolver(host, port, type):
            if host == "site2.example" and isolated_dns_error is not None:
                raise isolated_dns_error()
            if local_error is not None and elapsed[0] < 900 and host != "site1.example" and outage_stage == "resolver":
                raise local_error()
            return [(socket.AF_INET, type, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

        def opener(request, **kwargs):
            host = urlparse(request.full_url).hostname
            request_counts[host] += 1
            if local_error is not None and elapsed[0] < 900 and host != "site1.example" and outage_stage == "transport":
                raise local_error()
            if host == "site2.example" and article_error is not None:
                raise article_error(request.full_url)
            return ArticleResponse(request.full_url)

        monkeypatch.setattr(material, "fetch_article", lambda url, **kwargs: fetch_article(
            url, opener=opener, resolver=resolver, sleeper=lambda _: None, **kwargs,
        ))

    def run():
        return daily_run.run_daily(output_dir=tmp_path / "briefs", data_dir=tmp_path / "data", model_backend=backend)

    return SimpleNamespace(run=run, install_transport=install_transport, elapsed=elapsed,
                           waits=waits, published=published, backend=backend,
                           counts=request_counts, recovery=recovery, publish=publish,
                           data_dir=tmp_path / "data", checks=checks)


@pytest.mark.parametrize("article_error", [
    lambda url: HTTPError(url, 503, "unavailable", {}, None),
    lambda url: HTTPError(url, 502, "bad gateway", {}, None),
    lambda url: URLError(ConnectionRefusedError(errno.ECONNREFUSED, "refused")),
    lambda url: TimeoutError("article timed out"),
], ids=["503", "502", "connection-refused", "timeout"])
def test_one_unavailable_article_does_not_delay_or_prevent_publication(scheduled_run, article_error):
    run = scheduled_run
    run.install_transport(article_error=article_error)
    assert run.run().published == 1
    assert run.waits == []
    assert run.checks == [0]
    assert not run.recovery.exhausted
    items = run.published[0]["sections"]["ai"]["items"]
    assert {item["hn_item_id"] for item in items} == {"1", "3", "4", "5"}
    audit = json.loads((run.data_dir / "2026-10-12-hn-candidates.json").read_text())
    failed = next(item for item in audit if item["hn_item_id"] == "2")
    assert failed["rejection_reason"] == "topic_unknown"
    assert run.counts["site1.example"] == 1


@pytest.mark.parametrize("isolated_dns_error", [
    lambda: socket.gaierror(socket.EAI_AGAIN, "one domain SERVFAIL"),
    lambda: OSError(errno.ENETUNREACH, "one target has no usable route"),
], ids=["one-domain-servfail", "one-target-route"])
def test_one_unresolvable_article_does_not_delay_or_prevent_publication(scheduled_run, isolated_dns_error):
    run = scheduled_run
    run.install_transport(isolated_dns_error=isolated_dns_error)
    assert run.run().published == 1
    assert run.waits == []
    assert run.checks == [0]
    assert not run.recovery.exhausted
    items = run.published[0]["sections"]["ai"]["items"]
    assert {item["hn_item_id"] for item in items} == {"1", "3", "4", "5"}
    audit = json.loads((run.data_dir / "2026-10-12-hn-candidates.json").read_text())
    failed = next(item for item in audit if item["hn_item_id"] == "2")
    assert failed["rejection_reason"] == "topic_unknown"
    assert run.counts["site1.example"] == 1


@pytest.mark.parametrize("local_error", [
    lambda: socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"),
    lambda: OSError(errno.ENETDOWN, "network down"),
    lambda: OSError(errno.ENETUNREACH, "network unreachable"),
], ids=["temporary-dns", "network-down", "network-unreachable"])
def test_local_outage_resumes_generation_after_twenty_minutes(scheduled_run, local_error):
    run = scheduled_run
    run.install_transport(local_error=local_error)
    assert run.run().published == 1
    assert run.waits == [600, 600]
    assert run.elapsed == [1200]
    assert run.checks == [0, 600]
    assert not run.recovery.exhausted
    assert len(run.published[0]["sections"]["ai"]["items"]) == 5
    # Work completed before the outage and after recovery is each performed once.
    assert set(run.counts.values()) == {1}
    assert len(run.backend.titles) == len(set(run.backend.titles)) == 5


def test_transport_outage_resumes_without_repeating_successful_work(scheduled_run):
    run = scheduled_run
    run.install_transport(local_error=lambda: TimeoutError("transport unavailable"), outage_stage="transport")
    assert run.run().published == 1
    assert run.waits == [600, 600]
    assert run.checks == [0, 600]
    assert not run.recovery.exhausted
    assert len(run.published[0]["sections"]["ai"]["items"]) == 5
    assert run.counts["site1.example"] == 1
    assert run.counts["site3.example"] == run.counts["site4.example"] == run.counts["site5.example"] == 1
    assert len(run.backend.titles) == len(set(run.backend.titles)) == 5


@pytest.mark.parametrize("failed_stage", ["model", "publish"])
def test_critical_service_503_waits_and_resumes_without_regenerating(scheduled_run, monkeypatch, failed_stage):
    run = scheduled_run
    run.install_transport()
    attempts = []
    original = run.backend.summarize if failed_stage == "model" else run.publish

    @recoverable_network(failed_stage)
    def request(*args, **kwargs):
        attempts.append(failed_stage)
        if len(attempts) == 1:
            raise HTTPError("https://dependency.example/api", 503, "unavailable", {}, None)
        return original(*args, **kwargs)

    if failed_stage == "model":
        monkeypatch.setattr(run.backend, "summarize", request)
    else:
        monkeypatch.setattr(daily_run, "publish_brief", request)
    assert run.run().published == 1
    assert run.waits == [600]
    assert run.checks == []
    assert len(run.published) == 1
    assert set(run.counts.values()) == {1}
    assert len(run.backend.titles) == len(set(run.backend.titles)) == 5
