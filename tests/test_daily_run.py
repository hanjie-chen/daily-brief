import errno
import socket
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from daily_brief import daily_run
from daily_brief.cli import main
from daily_brief.network_recovery import NetworkRecovery, recoverable_network
from daily_brief.output import PublishResult


@pytest.mark.parametrize("error", [
    URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
    URLError(socket.gaierror(socket.EAI_NODATA, "No address associated with hostname")),
    URLError(socket.gaierror(socket.EAI_NONAME, "name unavailable")),
    URLError(socket.gaierror(socket.EAI_FAIL, "resolver failure")),
    URLError(OSError(errno.ENETUNREACH, "network unreachable")),
    TimeoutError(),
])
def test_scheduled_source_outage_waits_six_times_then_exits_without_publishing(
    monkeypatch, tmp_path, error,
):
    from daily_brief.candidates import hn_client
    from daily_brief.generation import SourceCollectionError

    attempts, short_waits, waits = [], [], []
    recovery = NetworkRecovery(sleeper=waits.append)
    monkeypatch.setattr(daily_run, "NetworkRecovery", lambda: recovery)
    monkeypatch.setattr(daily_run, "publish_brief", lambda **kw: pytest.fail("must not publish"))
    get_json = hn_client._get_json

    def opener(request, timeout):
        attempts.append(request.full_url)
        raise error

    monkeypatch.setattr(
        hn_client, "_get_json",
        lambda url: get_json(url, opener=opener, sleep=short_waits.append),
    )
    with pytest.raises(SourceCollectionError, match="algolia"):
        daily_run.run_daily(
            output_dir=tmp_path / "briefs", data_dir=tmp_path / "data",
            model_backend=object(),
        )

    assert waits == [600] * 6
    assert short_waits == [10, 20] * 7
    assert len(attempts) == 21
    assert len(set(attempts)) == 1
    assert recovery.exhausted
    assert list(tmp_path.iterdir()) == []


def test_daily_run_shares_allowance_and_does_not_regenerate_for_publish(monkeypatch):
    waits, calls = [], []
    recovery = NetworkRecovery(sleeper=waits.append, max_pauses=2)
    monkeypatch.setattr(daily_run, "NetworkRecovery", lambda: recovery)
    monkeypatch.setattr(daily_run, "daily_window", lambda: SimpleNamespace(date_label="2026-10-12"))
    outcomes = iter([TimeoutError(), "content", URLError("offline"), "published"])

    @recoverable_network("request")
    def request():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    backend = object()

    def generate(**kwargs):
        calls.append("generate")
        assert kwargs["model_backend"] is backend
        assert kwargs["date_label"] == "2026-10-12"
        assert request() == "content"
        # The scheduled date must not change while publication waits.
        monkeypatch.setattr(daily_run, "daily_window", lambda: pytest.fail("date already fixed"))

    def publish(**kwargs):
        calls.append("publish")
        assert kwargs["date_label"] == "2026-10-12"
        assert request() == "published"
        return PublishResult(1, 0)

    monkeypatch.setattr(daily_run, "run_generate", generate)
    monkeypatch.setattr(daily_run, "publish_brief", publish)
    assert daily_run.run_daily(output_dir="briefs", data_dir="data", model_backend=backend).published == 1
    assert calls == ["generate", "publish"]
    assert waits == [600, 600]


def test_exhausted_item_failure_does_not_publish_partial_brief(monkeypatch):
    recovery = NetworkRecovery(sleeper=lambda _: pytest.fail("no allowance"), max_pauses=0)
    monkeypatch.setattr(daily_run, "NetworkRecovery", lambda: recovery)

    @recoverable_network("model")
    def fail():
        raise TimeoutError()

    def generate(**kwargs):
        # Existing per-item degradation catches errors and returns normally.
        try:
            fail()
        except TimeoutError:
            pass

    monkeypatch.setattr(daily_run, "run_generate", generate)
    monkeypatch.setattr(daily_run, "publish_brief", lambda **kw: pytest.fail("must not publish"))
    with pytest.raises(daily_run.DailyRunError, match="publication skipped"):
        daily_run.run_daily(output_dir="briefs", data_dir="data", model_backend=object())


def test_cli_run_dispatch_and_failure_status(monkeypatch):
    from daily_brief import cli
    monkeypatch.setattr(cli, "_model_backend", lambda: "backend")
    calls = []
    monkeypatch.setattr(cli, "run_daily", lambda **kw: calls.append(kw) or PublishResult(1, 0))
    assert main(["run"]) == 0
    assert calls[0]["model_backend"] == "backend"
    monkeypatch.setattr(cli, "run_daily", lambda **kw: (_ for _ in ()).throw(daily_run.DailyRunError("exhausted")))
    assert main(["run"]) == 1


@pytest.mark.parametrize("args", [["--date", "2026-10-11"], ["--force"], ["--item", "123"]])
def test_run_rejects_options_that_could_republish_or_change_scope(args):
    with pytest.raises(SystemExit):
        main(["run", *args])
