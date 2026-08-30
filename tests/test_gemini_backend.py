import io
import json
import logging
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.gemini_backend import (
    DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS,
    DEFAULT_CLASSIFIER_MODEL,
    DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS,
    DEFAULT_SUMMARIZER_MODEL,
    GeminiAPIError,
    GeminiBackend,
    GeminiConfigurationError,
    GeminiResponseError,
    INTERACTIONS_URL,
    MAX_SUMMARY_CHARS,
)
from daily_brief.models import Candidate, Story


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self, amount=-1):
        return self.body if amount < 0 else self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class RecordingOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
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

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


def candidate(
    item_id: str,
    title: str,
    *,
    story_text: str = "",
    fetched_text: str = "",
) -> Candidate:
    return Candidate(
        story=Story(
            source="test",
            hn_item_id=item_id,
            title=title,
            source_url=f"https://example.com/{item_id}",
            hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
            created_at="2026-07-20T00:00:00Z",
            points=30,
            comments=5,
            story_text=story_text,
            fetched_text=fetched_text,
        )
    )


def interaction(output, *, usage=None, status="completed"):
    payload = {
        "status": status,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": output
                        if isinstance(output, str)
                        else json.dumps(output, ensure_ascii=False),
                    }
                ],
            }
        ],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def http_error(
    status: int,
    message: str,
    retry_after: str | None = None,
    retry_delay: str | None = None,
):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    error = {"message": message}
    if retry_delay is not None:
        error["details"] = [
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": retry_delay,
            }
        ]
    return HTTPError(
        INTERACTIONS_URL,
        status,
        message,
        headers,
        io.BytesIO(json.dumps({"error": error}).encode()),
    )


def request_payload(opener: RecordingOpener, index: int = 0):
    request, _ = opener.calls[index]
    return json.loads(request.data.decode("utf-8"))


def test_classifier_uses_pinned_model_structured_output_and_header_key():
    opener = RecordingOpener(
        FakeResponse(
            interaction(
                {
                    "decisions": [
                        {"id": "1", "label": "ai"},
                        {"id": "2", "label": "core_non_ai"},
                    ]
                }
            )
        )
    )
    backend = GeminiBackend(api_key="secret-key", opener=opener)
    items = [candidate("1", "Qwen release"), candidate("2", "SQLite release")]

    assert backend.classify(items) == {"1": "ai", "2": "core_non_ai"}

    request, timeout = opener.calls[0]
    payload = request_payload(opener)
    assert request.full_url == INTERACTIONS_URL
    assert "secret-key" not in request.full_url
    assert request.get_header("X-goog-api-key") == "secret-key"
    assert timeout == 90
    assert payload["model"] == DEFAULT_CLASSIFIER_MODEL
    assert payload["store"] is False
    assert payload["response_format"]["mime_type"] == "application/json"
    decisions_schema = payload["response_format"]["schema"]["properties"]["decisions"]
    assert decisions_schema["items"]["properties"]["id"]["enum"] == ["1", "2"]
    assert decisions_schema["items"]["properties"]["label"]["enum"] == [
        "ai",
        "core_non_ai",
        "outside",
        "uncertain",
    ]
    assert decisions_schema["minItems"] == 2
    assert payload["generation_config"] == {"max_output_tokens": 512}
    assert "temperature" not in json.dumps(payload)
    assert "Qwen release" in payload["input"]
    assert "untrusted" in payload["input"].lower()


def test_classifier_skips_api_for_empty_input():
    opener = RecordingOpener()
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    assert backend.classify([]) == {}
    assert opener.calls == []


def test_default_models_and_request_intervals_are_role_specific():
    backend = GeminiBackend(api_key="secret-key", opener=RecordingOpener())

    assert backend.classifier_model == "gemini-3.5-flash-lite"
    assert backend.summarizer_model == "gemini-3.6-flash"
    assert (
        backend.classifier_min_request_interval_seconds
        == DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS
    )
    assert (
        backend.summarizer_min_request_interval_seconds
        == DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS
    )


def test_request_intervals_are_independent_for_different_models():
    clock = FakeClock(100.0)
    opener = RecordingOpener(
        FakeResponse(
            interaction({"decisions": [{"id": "1", "label": "core_non_ai"}]})
        ),
        FakeResponse(interaction({"summary": "摘要。"})),
        FakeResponse(
            interaction({"decisions": [{"id": "2", "label": "core_non_ai"}]})
        ),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        clock=clock,
        sleeper=clock.sleep,
        classifier_min_request_interval_seconds=6.0,
        summarizer_min_request_interval_seconds=20.0,
    )

    backend.classify([candidate("1", "Database")])
    clock.now += 1.0
    backend.summarize(candidate("1", "Database", fetched_text="Facts."))
    clock.now += 1.0
    backend.classify([candidate("2", "Compiler")])

    assert clock.delays == [4.0]
    assert len(opener.calls) == 3


def test_same_model_uses_more_conservative_request_interval():
    clock = FakeClock(100.0)
    opener = RecordingOpener(
        FakeResponse(
            interaction({"decisions": [{"id": "1", "label": "core_non_ai"}]})
        ),
        FakeResponse(interaction({"summary": "摘要。"})),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        classifier_model="shared-model",
        summarizer_model="shared-model",
        opener=opener,
        clock=clock,
        sleeper=clock.sleep,
        classifier_min_request_interval_seconds=6.0,
        summarizer_min_request_interval_seconds=20.0,
    )

    backend.classify([candidate("1", "Database")])
    clock.now += 1.0
    backend.summarize(candidate("1", "Database", fetched_text="Facts."))

    assert clock.delays == [19.0]


def test_request_interval_also_covers_backend_retry_attempts():
    clock = FakeClock()
    opener = RecordingOpener(
        URLError("offline"),
        FakeResponse(
            interaction({"decisions": [{"id": "1", "label": "core_non_ai"}]})
        ),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        clock=clock,
        sleeper=clock.sleep,
        jitter=lambda start, end: 0,
        max_retries=1,
        retry_base_seconds=1.0,
        classifier_min_request_interval_seconds=6.0,
    )

    assert backend.classify([candidate("1", "Database")]) == {"1": "core_non_ai"}

    assert clock.delays == [1.0, 5.0]
    assert len(opener.calls) == 2


def test_summarizer_uses_fetched_text_and_logs_usage(caplog):
    opener = RecordingOpener(
        FakeResponse(
            interaction(
                {"summary": " 中文摘要。 "},
                usage={
                    "total_input_tokens": 100,
                    "total_output_tokens": 20,
                    "total_thought_tokens": 10,
                    "total_tokens": 130,
                },
            )
        )
    )
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    with caplog.at_level(logging.INFO, logger="daily_brief.gemini_backend"):
        summary = backend.summarize(
            candidate("1", "AI tool", fetched_text="Grounded article facts.")
        )

    assert summary == "中文摘要。"
    payload = request_payload(opener)
    assert payload["model"] == DEFAULT_SUMMARIZER_MODEL
    assert "Grounded article facts." in payload["input"]
    assert payload["response_format"]["schema"]["required"] == ["summary"]
    assert payload["generation_config"] == {"max_output_tokens": 2048}
    assert "input_tokens=100" in caplog.text
    assert "thought_tokens=10" in caplog.text
    assert "secret-key" not in caplog.text


def test_transient_http_errors_retry_with_retry_after_and_backoff():
    delays = []
    opener = RecordingOpener(
        http_error(429, "quota reached", retry_after="2.5"),
        http_error(503, "temporarily unavailable"),
        FakeResponse(interaction({"decisions": [{"id": "1", "label": "core_non_ai"}]})),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        sleeper=delays.append,
        jitter=lambda start, end: 0.25,
        retry_base_seconds=1,
        min_request_interval_seconds=0,
    )

    assert backend.classify([candidate("1", "Database")]) == {"1": "core_non_ai"}
    assert len(opener.calls) == 3
    assert delays == [2.5, 2.25]


def test_quota_error_retries_with_bounded_provider_retry_delay():
    delays = []
    opener = RecordingOpener(
        http_error(429, "quota reached. Please retry in 39.106525668s."),
        http_error(429, "quota reached", retry_delay="999s"),
        FakeResponse(interaction({"decisions": [{"id": "1", "label": "core_non_ai"}]})),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        sleeper=delays.append,
        min_request_interval_seconds=0,
    )

    assert backend.classify([candidate("1", "Database")]) == {"1": "core_non_ai"}
    assert len(opener.calls) == 3
    assert delays == [39.106525668, 60.0]


def test_network_error_retries_only_up_to_configured_limit():
    delays = []
    opener = RecordingOpener(URLError("offline"), URLError("still offline"))
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        sleeper=delays.append,
        jitter=lambda start, end: 0,
        max_retries=1,
        min_request_interval_seconds=0,
    )

    with pytest.raises(GeminiAPIError, match="after 2 attempts"):
        backend.classify([candidate("1", "Qwen")])

    assert len(opener.calls) == 2
    assert delays == [1]


def test_non_retryable_http_error_fails_once_without_exposing_key():
    opener = RecordingOpener(http_error(403, "permission denied"))
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    with pytest.raises(GeminiAPIError) as caught:
        backend.classify([candidate("1", "Qwen")])

    assert "HTTP 403" in str(caught.value)
    assert "permission denied" in str(caught.value)
    assert "secret-key" not in str(caught.value)
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ({"decisions": [{"id": "unknown", "label": "ai"}]}, "unknown IDs"),
        (
            {
                "decisions": [
                    {"id": "1", "label": "ai"},
                    {"id": "1", "label": "outside"},
                ]
            },
            "duplicate IDs",
        ),
        ({"decisions": [{"id": "1", "label": "unsupported"}]}, "unknown labels"),
        ({"decisions": []}, "omitted item IDs"),
        ({"decisions": [{"id": 1, "label": "ai"}]}, "invalid decisions"),
        ({"decisions": [], "extra": True}, "invalid object"),
        ([], "must be an object"),
        ("not JSON", "invalid structured JSON"),
    ],
)
def test_classifier_rejects_invalid_structured_results(output, message):
    opener = RecordingOpener(FakeResponse(interaction(output)))
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    with pytest.raises(GeminiResponseError, match=message):
        backend.classify([candidate("1", "Qwen")])


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ({"summary": ""}, "empty summary"),
        ({"summary": "x" * (MAX_SUMMARY_CHARS + 1)}, "oversized summary"),
        ({"summary": 42}, "invalid object"),
        ({"summary": "valid", "extra": True}, "invalid object"),
    ],
)
def test_summarizer_rejects_invalid_structured_results(output, message):
    opener = RecordingOpener(FakeResponse(interaction(output)))
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    with pytest.raises(GeminiResponseError, match=message):
        backend.summarize(candidate("1", "AI tool"))


def test_incomplete_interaction_is_rejected():
    opener = RecordingOpener(
        FakeResponse(interaction({"summary": "摘要"}, status="failed"))
    )
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    with pytest.raises(GeminiResponseError, match="did not complete"):
        backend.summarize(candidate("1", "AI tool"))


def test_from_environment_uses_explicit_model_overrides():
    backend = GeminiBackend.from_environment(
        {
            "GEMINI_API_KEY": "key-from-env",
            "DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL": "gemini-classifier-stable",
            "DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL": "gemini-summary-stable",
            "DAILY_BRIEF_GEMINI_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS": "6.5",
            "DAILY_BRIEF_GEMINI_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS": "20.5",
        },
        opener=RecordingOpener(),
    )

    assert backend.api_key == "key-from-env"
    assert backend.classifier_model == "gemini-classifier-stable"
    assert backend.summarizer_model == "gemini-summary-stable"
    assert backend.classifier_min_request_interval_seconds == 6.5
    assert backend.summarizer_min_request_interval_seconds == 20.5


def test_from_environment_uses_legacy_shared_interval_as_fallback():
    backend = GeminiBackend.from_environment(
        {
            "GEMINI_API_KEY": "key-from-env",
            "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS": "12.5",
        },
        opener=RecordingOpener(),
    )

    assert backend.classifier_min_request_interval_seconds == 12.5
    assert backend.summarizer_min_request_interval_seconds == 12.5


@pytest.mark.parametrize(
    "name",
    [
        "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS",
        "DAILY_BRIEF_GEMINI_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS",
        "DAILY_BRIEF_GEMINI_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS",
    ],
)
def test_from_environment_rejects_non_numeric_request_interval(name):
    with pytest.raises(GeminiConfigurationError, match="must be numeric"):
        GeminiBackend.from_environment(
            {
                "GEMINI_API_KEY": "key-from-env",
                name: "fast",
            }
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": ""},
        {"api_key": "key", "classifier_model": "https://example.com/model"},
        {"api_key": "key", "timeout_seconds": 0},
        {"api_key": "key", "max_retries": -1},
        {"api_key": "key", "retry_base_seconds": -1},
        {"api_key": "key", "classifier_min_request_interval_seconds": -1},
        {"api_key": "key", "summarizer_min_request_interval_seconds": -1},
        {"api_key": "key", "min_request_interval_seconds": -1},
        {"api_key": "key", "min_request_interval_seconds": float("inf")},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(GeminiConfigurationError):
        GeminiBackend(**kwargs)
