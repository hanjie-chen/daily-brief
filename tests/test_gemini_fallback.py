import io
import json
from datetime import UTC, datetime
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.gemini_backend import (
    GeminiAPIError,
    GeminiBackend,
    GeminiResponseError,
    INTERACTIONS_URL,
)
from daily_brief.models import Candidate, Story
from daily_brief.summarizer import InsufficientSummaryMaterial


class FakeResponse:
    def __init__(self, output):
        self.body = json.dumps({
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{
                "type": "text", "text": json.dumps(output, ensure_ascii=False),
            }]}],
        }, ensure_ascii=False).encode()

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class RecordingOpener:
    def __init__(self, *outcomes, clock=None):
        self.outcomes = list(outcomes)
        self.calls = []
        self.clock = clock

    def __call__(self, request, timeout):
        self.calls.append((json.loads(request.data.decode()), self.clock() if self.clock else None))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now
        self.delays = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class FakeWallClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def item():
    return Candidate(story=Story(
        source="test", hn_item_id="1", title="A release", source_url="https://example.com/a",
        hn_discussion_url="https://news.ycombinator.com/item?id=1",
        created_at="2026-09-22T00:00:00Z", points=1, comments=1,
        fetched_text="The release has one grounded fact.",
    ))


def sufficient(text="摘要。"):
    return FakeResponse({"status": "sufficient", "summary": text, "reason": ""})


def insufficient():
    return FakeResponse({"status": "insufficient", "summary": "", "reason": "材料不足。"})


def http_error(status, message, *, details=None):
    headers = Message()
    body = {"error": {"message": message}}
    if details is not None:
        body["error"]["details"] = details
    return HTTPError(INTERACTIONS_URL, status, message, headers, io.BytesIO(json.dumps(body).encode()))


def daily_quota_error():
    return http_error(429, "Resource exhausted", details=[{
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests_per_day",
        }],
    }])


def models(opener, **kwargs):
    request_interval = kwargs.pop("min_request_interval_seconds", 0)
    return GeminiBackend(
        api_key="test-key", opener=opener,
        summarizer_model="gemini-3.6-flash",
        summarizer_fallback_models=("gemini-3.7-flash", "gemini-3.8-flash"),
        min_request_interval_seconds=request_interval,
        jitter=lambda _start, _end: 0,
        **kwargs,
    )


def called_models(opener):
    return [payload["model"] for payload, _started_at in opener.calls]


def test_summary_uses_configured_fallback_order_after_daily_quota():
    opener = RecordingOpener(daily_quota_error(), daily_quota_error(), sufficient("来自 3.8。"))
    backend = models(opener, max_retries=0)

    assert backend.summarize(item()) == "来自 3.8。"
    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]
    assert backend.last_summary_model == "gemini-3.8-flash"
    assert backend.last_summary_attempts == 3


def test_daily_quota_skips_model_until_los_angeles_midnight_then_resets():
    # 06:59 UTC is 23:59 on the prior day in Los Angeles during PDT.
    wall_clock = FakeWallClock(datetime(2026, 9, 22, 6, 59, tzinfo=UTC).timestamp())
    opener = RecordingOpener(
        daily_quota_error(), sufficient("3.7 first"), sufficient("3.7 second"), sufficient("3.6 reset"),
    )
    backend = models(opener, max_retries=0, wall_clock=wall_clock)

    assert backend.summarize(item()) == "3.7 first"
    assert backend.summarize(item()) == "3.7 second"
    wall_clock.now += 120
    assert backend.summarize(item()) == "3.6 reset"

    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.7-flash", "gemini-3.6-flash"]


def test_minute_quota_retries_same_model_and_does_not_switch_on_success():
    opener = RecordingOpener(http_error(429, "Minute quota reached"), sufficient("retry worked"))
    backend = models(opener, max_retries=1, retry_base_seconds=0)

    assert backend.summarize(item()) == "retry worked"
    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.6-flash"]
    assert backend.last_summary_model == "gemini-3.6-flash"
    assert backend.last_summary_attempts == 2


def test_unknown_429_exhaustion_does_not_switch_models():
    opener = RecordingOpener(http_error(429, "Resource exhausted"), http_error(429, "Resource exhausted"))
    backend = models(opener, max_retries=1, retry_base_seconds=0)

    with pytest.raises(GeminiAPIError):
        backend.summarize(item())

    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.6-flash"]
    assert backend.last_summary_model == "gemini-3.6-flash"
    assert backend.last_summary_attempts == 2


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_errors_never_switch_models(status):
    opener = RecordingOpener(http_error(status, "permission denied"))
    backend = models(opener, max_retries=0)

    with pytest.raises(GeminiAPIError):
        backend.summarize(item())

    assert called_models(opener) == ["gemini-3.6-flash"]


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), URLError("offline")])
def test_network_failures_switch_to_the_next_model_after_bounded_retries(failure):
    opener = RecordingOpener(failure, sufficient("3.7 works"))
    backend = models(opener, max_retries=0)

    assert backend.summarize(item()) == "3.7 works"
    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.7-flash"]


def test_insufficient_material_is_a_completed_decision_and_never_switches():
    opener = RecordingOpener(insufficient())
    backend = models(opener)

    with pytest.raises(InsufficientSummaryMaterial):
        backend.summarize(item())

    assert called_models(opener) == ["gemini-3.6-flash"]


def test_invalid_summary_response_never_switches_models():
    opener = RecordingOpener(FakeResponse({"status": "sufficient", "summary": "", "reason": ""}))
    backend = models(opener)

    with pytest.raises(GeminiResponseError):
        backend.summarize(item())

    assert called_models(opener) == ["gemini-3.6-flash"]


def test_503_retries_are_bounded_before_falling_back():
    opener = RecordingOpener(
        http_error(503, "temporarily unavailable"), http_error(503, "temporarily unavailable"), sufficient("3.7 works"),
    )
    backend = models(opener, max_retries=1, retry_base_seconds=0)

    assert backend.summarize(item()) == "3.7 works"
    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.6-flash", "gemini-3.7-flash"]
    assert backend.last_summary_model == "gemini-3.7-flash"
    assert backend.last_summary_attempts == 3


def test_summary_attempts_and_model_report_the_actual_last_attempt_when_all_fail():
    opener = RecordingOpener(
        daily_quota_error(), daily_quota_error(), http_error(503, "temporarily unavailable"),
    )
    backend = models(opener, max_retries=0)

    with pytest.raises(GeminiAPIError):
        backend.summarize(item())

    assert backend.last_summary_model == "gemini-3.8-flash"
    assert backend.last_summary_attempts == 3


def test_all_daily_exhausted_models_are_cached_without_new_requests():
    opener = RecordingOpener(daily_quota_error(), daily_quota_error(), daily_quota_error())
    backend = models(opener, max_retries=0)

    with pytest.raises(GeminiAPIError) as first:
        backend.summarize(item())
    with pytest.raises(GeminiAPIError, match="All summary models"):
        backend.summarize(item())

    assert first.value.error_code == "daily_quota_exceeded"
    assert called_models(opener) == ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash"]
    assert backend.last_summary_model == ""
    assert backend.last_summary_attempts == 0


def test_summary_pacing_is_shared_across_retries_and_model_switches():
    clock = FakeClock()
    opener = RecordingOpener(
        http_error(503, "temporarily unavailable"), http_error(503, "temporarily unavailable"), sufficient("3.7 works"),
        clock=clock,
    )
    backend = models(
        opener, max_retries=1, retry_base_seconds=0,
        clock=clock, sleeper=clock.sleep,
        min_request_interval_seconds=None,
        summarizer_min_request_interval_seconds=20,
    )

    assert backend.summarize(item()) == "3.7 works"
    assert [started_at for _payload, started_at in opener.calls] == [0, 20, 40]
    assert clock.delays == [0, 20, 20]


def test_cli_records_the_actual_fallback_model_and_total_attempts():
    from daily_brief.cli import _generate_candidate_summary

    opener = RecordingOpener(daily_quota_error(), daily_quota_error(), sufficient("来自 3.8。"))
    backend = models(opener, max_retries=0)
    candidate = item()

    assert _generate_candidate_summary(candidate, backend) is False
    assert candidate.summary_generation.model == "gemini-3.8-flash"
    assert candidate.summary_generation.attempts == 3


def test_cli_records_actual_model_for_insufficient_fallback_decision():
    from daily_brief.cli import _generate_candidate_summary

    opener = RecordingOpener(daily_quota_error(), insufficient())
    backend = models(opener, max_retries=0)
    candidate = item()

    assert _generate_candidate_summary(candidate, backend) is True
    assert candidate.summary_generation.model == "gemini-3.7-flash"
    assert candidate.summary_generation.attempts == 2


def test_environment_defaults_fallbacks_but_explicit_choice_can_disable_or_replace_them():
    default = GeminiBackend.from_environment({"GEMINI_API_KEY": "key"}, opener=RecordingOpener())
    disabled = GeminiBackend.from_environment({
        "GEMINI_API_KEY": "key", "DAILY_BRIEF_GEMINI_SUMMARIZER_FALLBACK_MODELS": "",
    }, opener=RecordingOpener())
    custom_primary = GeminiBackend.from_environment({
        "GEMINI_API_KEY": "key", "DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL": "another-model",
    }, opener=RecordingOpener())
    custom_fallbacks = GeminiBackend.from_environment({
        "GEMINI_API_KEY": "key", "DAILY_BRIEF_GEMINI_SUMMARIZER_FALLBACK_MODELS": " 3.7 , 3.8 ",
    }, opener=RecordingOpener())

    assert default.summarizer_fallback_models == ("gemini-3.7-flash", "gemini-3.8-flash")
    assert disabled.summarizer_fallback_models == ()
    assert custom_primary.summarizer_fallback_models == ()
    assert custom_fallbacks.summarizer_fallback_models == ("3.7", "3.8")


def test_evaluate_model_explicitly_disables_summary_fallbacks(monkeypatch, tmp_path):
    from daily_brief import cli

    captured = {}

    class Factory:
        @classmethod
        def from_environment(cls, **kwargs):
            captured.update(kwargs)
            return type("Backend", (), {"name": "fake"})()

    monkeypatch.setattr(cli, "GeminiBackend", Factory)
    monkeypatch.setattr(
        cli, "run_model_evaluation",
        lambda *_args: type("Result", (), {"failures": 0, "output_path": tmp_path / "out.json"})(),
    )

    assert cli.main(["evaluate-model", "--date", "2026-09-22", "--data-dir", str(tmp_path)]) == 0
    assert captured == {"summarizer_fallback_models": ()}
