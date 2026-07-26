import io
import json
import logging
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.gemini_backend import (
    DEFAULT_CLASSIFIER_MODEL,
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


def http_error(status: int, message: str, retry_after: str | None = None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        INTERACTIONS_URL,
        status,
        message,
        headers,
        io.BytesIO(json.dumps({"error": {"message": message}}).encode()),
    )


def request_payload(opener: RecordingOpener, index: int = 0):
    request, _ = opener.calls[index]
    return json.loads(request.data.decode("utf-8"))


def test_classifier_uses_pinned_model_structured_output_and_header_key():
    opener = RecordingOpener(
        FakeResponse(interaction({"selected_ids": ["1"]}))
    )
    backend = GeminiBackend(api_key="secret-key", opener=opener)
    items = [candidate("1", "Qwen release"), candidate("2", "SQLite release")]

    assert backend.classify(items) == {"1"}

    request, timeout = opener.calls[0]
    payload = request_payload(opener)
    assert request.full_url == INTERACTIONS_URL
    assert "secret-key" not in request.full_url
    assert request.get_header("X-goog-api-key") == "secret-key"
    assert timeout == 90
    assert payload["model"] == DEFAULT_CLASSIFIER_MODEL
    assert payload["store"] is False
    assert payload["response_format"]["mime_type"] == "application/json"
    selected_schema = payload["response_format"]["schema"]["properties"][
        "selected_ids"
    ]
    assert selected_schema["items"]["enum"] == ["1", "2"]
    assert payload["generation_config"] == {"max_output_tokens": 512}
    assert "temperature" not in json.dumps(payload)
    assert "Qwen release" in payload["input"]
    assert "untrusted" in payload["input"].lower()


def test_classifier_skips_api_for_empty_input():
    opener = RecordingOpener()
    backend = GeminiBackend(api_key="secret-key", opener=opener)

    assert backend.classify([]) == set()
    assert opener.calls == []


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
    assert "input_tokens=100" in caplog.text
    assert "thought_tokens=10" in caplog.text
    assert "secret-key" not in caplog.text


def test_transient_http_errors_retry_with_retry_after_and_backoff():
    delays = []
    opener = RecordingOpener(
        http_error(429, "quota reached", retry_after="2.5"),
        http_error(503, "temporarily unavailable"),
        FakeResponse(interaction({"selected_ids": []})),
    )
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        sleeper=delays.append,
        jitter=lambda start, end: 0.25,
        retry_base_seconds=1,
    )

    assert backend.classify([candidate("1", "Database")]) == set()
    assert len(opener.calls) == 3
    assert delays == [2.5, 2.25]


def test_network_error_retries_only_up_to_configured_limit():
    delays = []
    opener = RecordingOpener(URLError("offline"), URLError("still offline"))
    backend = GeminiBackend(
        api_key="secret-key",
        opener=opener,
        sleeper=delays.append,
        jitter=lambda start, end: 0,
        max_retries=1,
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
        ({"selected_ids": ["unknown"]}, "unknown IDs"),
        ({"selected_ids": ["1", "1"]}, "duplicate IDs"),
        ({"selected_ids": [1]}, "non-string IDs"),
        ({"selected_ids": [], "extra": True}, "invalid object"),
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
        },
        opener=RecordingOpener(),
    )

    assert backend.api_key == "key-from-env"
    assert backend.classifier_model == "gemini-classifier-stable"
    assert backend.summarizer_model == "gemini-summary-stable"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_key": ""},
        {"api_key": "key", "classifier_model": "https://example.com/model"},
        {"api_key": "key", "timeout_seconds": 0},
        {"api_key": "key", "max_retries": -1},
        {"api_key": "key", "retry_base_seconds": -1},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(GeminiConfigurationError):
        GeminiBackend(**kwargs)
