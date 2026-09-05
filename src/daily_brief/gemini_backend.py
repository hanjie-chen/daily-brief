from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Candidate
from .summarizer import SUMMARY_SYSTEM_INSTRUCTION, build_summary_prompt
from .topic_classifier import (
    TOPIC_CLASSIFIER_SYSTEM_INSTRUCTION,
    build_topic_classifier_prompt,
)

LOGGER = logging.getLogger(__name__)

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_CLASSIFIER_MODEL = "gemini-3.5-flash-lite"
DEFAULT_SUMMARIZER_MODEL = "gemini-3.6-flash"
DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS = 6.0
DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS = 20.0
MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
RETRYABLE_HTTP_STATUSES = {408, 429}
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 1000
CLASSIFIER_MAX_OUTPUT_TOKENS = 512
SUMMARY_MAX_OUTPUT_TOKENS = 8192
SUMMARY_THINKING_LEVEL = "high"
SUMMARY_INCOMPLETE_RETRIES = 1
RETRY_DELAY_PATTERN = re.compile(r"^(\d+)(?:\.(\d{1,9}))?s$")
RETRY_MESSAGE_PATTERN = re.compile(
    r"(?:^|\s)Please retry in (\d+(?:\.\d{1,9})?)s(?:[.\s]|$)"
)


class GeminiConfigurationError(ValueError):
    pass


class GeminiAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "provider_error",
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status


class GeminiResponseError(GeminiAPIError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "invalid_response",
        provider_status: str = "",
    ) -> None:
        super().__init__(message, error_code=error_code)
        self.provider_status = provider_status


class GeminiBackend:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        classifier_model: str = DEFAULT_CLASSIFIER_MODEL,
        summarizer_model: str = DEFAULT_SUMMARIZER_MODEL,
        timeout_seconds: int = 90,
        max_retries: int = 3,
        retry_base_seconds: float = 1.0,
        classifier_min_request_interval_seconds: float = (
            DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS
        ),
        summarizer_min_request_interval_seconds: float = (
            DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS
        ),
        min_request_interval_seconds: float | None = None,
        opener: Callable[..., HTTPResponse] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.api_key = api_key.strip()
        self.classifier_model = _validate_model(classifier_model)
        self.summarizer_model = _validate_model(summarizer_model)
        if not self.api_key:
            raise GeminiConfigurationError("GEMINI_API_KEY is not configured")
        if timeout_seconds <= 0:
            raise GeminiConfigurationError("Gemini timeout must be positive")
        if max_retries < 0 or max_retries > 10:
            raise GeminiConfigurationError(
                "Gemini max retries must be between 0 and 10"
            )
        if retry_base_seconds < 0:
            raise GeminiConfigurationError("Gemini retry base must not be negative")
        if min_request_interval_seconds is not None:
            classifier_min_request_interval_seconds = min_request_interval_seconds
            summarizer_min_request_interval_seconds = min_request_interval_seconds
        _validate_request_interval(
            classifier_min_request_interval_seconds,
            "Gemini classifier minimum request interval",
        )
        _validate_request_interval(
            summarizer_min_request_interval_seconds,
            "Gemini summarizer minimum request interval",
        )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.classifier_min_request_interval_seconds = (
            classifier_min_request_interval_seconds
        )
        self.summarizer_min_request_interval_seconds = (
            summarizer_min_request_interval_seconds
        )
        self._model_request_intervals: dict[str, float] = {}
        for model, interval in (
            (self.classifier_model, classifier_min_request_interval_seconds),
            (self.summarizer_model, summarizer_min_request_interval_seconds),
        ):
            self._model_request_intervals[model] = max(
                interval,
                self._model_request_intervals.get(model, 0.0),
            )
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock
        self.jitter = jitter
        self._last_request_started_by_model: dict[str, float] = {}
        self._last_request_attempts_by_model: dict[str, int] = {}
        self._last_response_status_by_model: dict[str, str] = {}
        self._last_response_usage_by_model: dict[str, dict[str, int | None]] = {}

    @property
    def last_summary_attempts(self) -> int:
        return self._last_request_attempts_by_model.get(self.summarizer_model, 0)

    @property
    def last_summary_provider_status(self) -> str:
        return self._last_response_status_by_model.get(self.summarizer_model, "")

    @property
    def last_summary_usage(self) -> dict[str, int | None]:
        return dict(self._last_response_usage_by_model.get(self.summarizer_model, {}))

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, **kwargs
    ) -> GeminiBackend:
        environment = os.environ if env is None else env
        shared_interval = environment.get(
            "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS"
        )
        if "classifier_min_request_interval_seconds" in kwargs:
            classifier_interval = kwargs.pop(
                "classifier_min_request_interval_seconds"
            )
        else:
            classifier_interval = _environment_request_interval(
                environment,
                "DAILY_BRIEF_GEMINI_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS",
                shared_interval,
                DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS,
            )
        if "summarizer_min_request_interval_seconds" in kwargs:
            summarizer_interval = kwargs.pop(
                "summarizer_min_request_interval_seconds"
            )
        else:
            summarizer_interval = _environment_request_interval(
                environment,
                "DAILY_BRIEF_GEMINI_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS",
                shared_interval,
                DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS,
            )
        return cls(
            api_key=environment.get("GEMINI_API_KEY", ""),
            classifier_model=environment.get(
                "DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL
            ),
            summarizer_model=environment.get(
                "DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL", DEFAULT_SUMMARIZER_MODEL
            ),
            classifier_min_request_interval_seconds=classifier_interval,
            summarizer_min_request_interval_seconds=summarizer_interval,
            **kwargs,
        )

    def classify(self, candidates: list[Candidate]) -> dict[str, str]:
        if not candidates:
            return {}
        self._reset_request_diagnostics(self.classifier_model)
        allowed_ids = [candidate.story.hn_item_id for candidate in candidates]
        output = self._interact(
            task="classify",
            model=self.classifier_model,
            system_instruction=TOPIC_CLASSIFIER_SYSTEM_INSTRUCTION,
            prompt=build_topic_classifier_prompt(
                candidates,
                (
                    "Return one JSON object with a decisions array. "
                    "Do not include Markdown or explanations."
                ),
            ),
            schema={
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "description": "One topic decision for every supplied item.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "enum": allowed_ids},
                                "label": {
                                    "type": "string",
                                    "enum": [
                                        "ai",
                                        "core_non_ai",
                                        "outside",
                                        "uncertain",
                                    ],
                                },
                            },
                            "required": ["id", "label"],
                            "additionalProperties": False,
                        },
                        "minItems": len(allowed_ids),
                        "maxItems": len(allowed_ids),
                    }
                },
                "required": ["decisions"],
                "additionalProperties": False,
            },
            max_output_tokens=CLASSIFIER_MAX_OUTPUT_TOKENS,
        )
        if set(output) != {"decisions"} or not isinstance(output["decisions"], list):
            raise GeminiResponseError("Gemini classifier returned an invalid object")
        decisions = output["decisions"]
        if not all(
            isinstance(item, dict)
            and set(item) == {"id", "label"}
            and isinstance(item["id"], str)
            and isinstance(item["label"], str)
            for item in decisions
        ):
            raise GeminiResponseError("Gemini classifier returned invalid decisions")
        decision_ids = [item["id"] for item in decisions]
        if len(set(decision_ids)) != len(decision_ids):
            raise GeminiResponseError("Gemini classifier returned duplicate IDs")
        unknown_ids = set(decision_ids) - set(allowed_ids)
        if unknown_ids:
            raise GeminiResponseError("Gemini classifier returned unknown IDs")
        if set(decision_ids) != set(allowed_ids):
            raise GeminiResponseError("Gemini classifier omitted item IDs")
        allowed_labels = {"ai", "core_non_ai", "outside", "uncertain"}
        if any(item["label"] not in allowed_labels for item in decisions):
            raise GeminiResponseError("Gemini classifier returned unknown labels")
        return {item["id"]: item["label"] for item in decisions}

    def summarize(self, candidate: Candidate) -> str:
        self._reset_request_diagnostics(self.summarizer_model)
        output = self._interact(
            task="summarize",
            model=self.summarizer_model,
            system_instruction=SUMMARY_SYSTEM_INSTRUCTION,
            prompt=build_summary_prompt(candidate),
            schema={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A concise 1-2 sentence Chinese summary.",
                    }
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
            max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            thinking_level=SUMMARY_THINKING_LEVEL,
            incomplete_retries=SUMMARY_INCOMPLETE_RETRIES,
        )
        if set(output) != {"summary"} or not isinstance(output["summary"], str):
            raise GeminiResponseError("Gemini summarizer returned an invalid object")
        summary = output["summary"].strip()
        if not summary:
            raise GeminiResponseError("Gemini summarizer returned an empty summary")
        if len(summary) > MAX_SUMMARY_CHARS:
            raise GeminiResponseError("Gemini summarizer returned an oversized summary")
        return summary

    def _interact(
        self,
        *,
        task: str,
        model: str,
        system_instruction: str,
        prompt: str,
        schema: dict,
        max_output_tokens: int,
        thinking_level: str | None = None,
        incomplete_retries: int = 0,
    ) -> dict:
        generation_config = {"max_output_tokens": max_output_tokens}
        if thinking_level is not None:
            generation_config["thinking_level"] = thinking_level
        payload = {
            "model": model,
            "input": prompt,
            "system_instruction": system_instruction,
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
            "generation_config": generation_config,
            "store": False,
        }
        for incomplete_retry in range(incomplete_retries + 1):
            self._last_response_status_by_model[model] = ""
            self._last_response_usage_by_model[model] = {}
            response = self._post_json(payload, model=model)
            provider_status = _provider_status(response)
            usage = _normalized_usage(response.get("usage"))
            self._last_response_status_by_model[model] = provider_status
            self._last_response_usage_by_model[model] = usage
            if provider_status == "completed":
                break
            will_retry = (
                provider_status == "incomplete"
                and incomplete_retry < incomplete_retries
            )
            _log_non_completed_interaction(
                task,
                model,
                provider_status,
                response.get("errors"),
                usage,
                attempts=self._last_request_attempts_by_model.get(model, 0),
                will_retry=will_retry,
            )
            if will_retry:
                continue
            raise GeminiResponseError(
                f"Gemini interaction ended with status {provider_status}",
                provider_status=provider_status,
            )
        text = _extract_output_text(response)
        try:
            output = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise GeminiResponseError(
                "Gemini returned invalid structured JSON"
            ) from exc
        if not isinstance(output, dict):
            raise GeminiResponseError("Gemini structured output must be an object")
        _log_usage(task, model, response.get("usage"))
        return output

    def _post_json(self, payload: dict, *, model: str) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            self._wait_for_request_slot(model)
            self._last_request_attempts_by_model[model] = (
                self._last_request_attempts_by_model.get(model, 0) + 1
            )
            request = Request(
                INTERACTIONS_URL,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "daily-brief/0.1",
                    "x-goog-api-key": self.api_key,
                },
                method="POST",
            )
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    response_body = response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as exc:
                error_body = exc.read(MAX_RESPONSE_BYTES + 1)
                if _is_retryable_status(exc.code) and attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt, exc.headers, error_body))
                    continue
                raise GeminiAPIError(
                    _http_error_message(exc.code, error_body),
                    error_code=_http_error_code(exc.code),
                    http_status=exc.code,
                ) from exc
            except TimeoutError as exc:
                if attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt, None))
                    continue
                raise GeminiAPIError(
                    f"Gemini API request failed after {attempt + 1} attempts",
                    error_code="timeout",
                ) from exc
            except URLError as exc:
                if attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt, None))
                    continue
                raise GeminiAPIError(
                    f"Gemini API request failed after {attempt + 1} attempts",
                    error_code="network_error",
                ) from exc

            if len(response_body) > MAX_RESPONSE_BYTES:
                raise GeminiResponseError("Gemini response exceeded the size limit")
            try:
                decoded = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GeminiResponseError("Gemini API returned invalid JSON") from exc
            if not isinstance(decoded, dict):
                raise GeminiResponseError("Gemini API response must be an object")
            return decoded
        raise AssertionError("Gemini retry loop ended unexpectedly")

    def _reset_request_diagnostics(self, model: str) -> None:
        self._last_request_attempts_by_model[model] = 0
        self._last_response_status_by_model[model] = ""
        self._last_response_usage_by_model[model] = {}

    def _wait_for_request_slot(self, model: str) -> None:
        now = self.clock()
        last_request_started = self._last_request_started_by_model.get(model)
        if last_request_started is not None:
            remaining = self._model_request_intervals[model] - (
                now - last_request_started
            )
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self._last_request_started_by_model[model] = now

    def _retry_delay(
        self, attempt: int, headers, error_body: bytes | None = None
    ) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        provider_delay = _retry_delay_from_error(error_body)
        if provider_delay is not None:
            return min(max(provider_delay, 0.0), 60.0)
        base_delay = self.retry_base_seconds * (2**attempt)
        return min(base_delay + self.jitter(0.0, self.retry_base_seconds), 60.0)


def _validate_model(model: str) -> str:
    normalized = model.strip()
    if not MODEL_PATTERN.fullmatch(normalized):
        raise GeminiConfigurationError("Gemini model ID is invalid")
    return normalized


def _validate_request_interval(value: float, label: str) -> None:
    if not math.isfinite(value) or value < 0:
        raise GeminiConfigurationError(f"{label} must not be negative")


def _environment_request_interval(
    environment: Mapping[str, str],
    name: str,
    shared_value: str | None,
    default: float,
) -> float:
    fallback = shared_value if shared_value is not None else str(default)
    value = environment.get(name, fallback)
    try:
        return float(value)
    except ValueError as exc:
        source_name = (
            name
            if name in environment
            else "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS"
        )
        raise GeminiConfigurationError(f"{source_name} must be numeric") from exc


def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


def _http_error_code(status: int) -> str:
    if status == 429:
        return "quota_exceeded"
    if status == 408:
        return "timeout"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return f"http_{status}"


def _http_error_message(status: int, body: bytes) -> str:
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = " ".join(error["message"].split())[:500]
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    suffix = f": {message}" if message else ""
    return f"Gemini API HTTP {status}{suffix}"


def _retry_delay_from_error(body: bytes | None) -> float | None:
    if not body or len(body) > MAX_RESPONSE_BYTES:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            detail_type = detail.get("@type")
            retry_delay = detail.get("retryDelay")
            if (
                not isinstance(detail_type, str)
                or not detail_type.endswith("google.rpc.RetryInfo")
                or not isinstance(retry_delay, str)
            ):
                continue
            match = RETRY_DELAY_PATTERN.fullmatch(retry_delay)
            if match is None:
                continue
            fraction = match.group(2) or ""
            return float(match.group(1)) + (
                int(fraction) / (10 ** len(fraction)) if fraction else 0.0
            )
    message = error.get("message") if isinstance(error, dict) else None
    if isinstance(message, str):
        match = RETRY_MESSAGE_PATTERN.search(message)
        if match is not None:
            return float(match.group(1))
    return None


def _extract_output_text(response: dict) -> str:
    text_parts = []
    steps = response.get("steps")
    if not isinstance(steps, list):
        raise GeminiResponseError("Gemini response does not contain output steps")
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                text_parts.append(part["text"])
    output_text = "".join(text_parts).strip()
    if not output_text:
        raise GeminiResponseError("Gemini response does not contain output text")
    return output_text


def _provider_status(response: dict) -> str:
    status = response.get("status")
    if not isinstance(status, str) or not status.strip():
        return "unknown"
    return " ".join(status.split())[:128]


def _normalized_usage(usage) -> dict[str, int | None]:
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": _usage_integer(usage.get("total_input_tokens")),
        "output_tokens": _usage_integer(usage.get("total_output_tokens")),
        "thought_tokens": _usage_integer(usage.get("total_thought_tokens")),
        "total_tokens": _usage_integer(usage.get("total_tokens")),
    }


def _usage_integer(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _interaction_error_codes(errors) -> str:
    if not isinstance(errors, list):
        return "none"
    codes = []
    for error in errors[:5]:
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code.strip():
            codes.append(" ".join(code.split())[:128])
    return ",".join(codes) if codes else "none"


def _log_non_completed_interaction(
    task: str,
    model: str,
    provider_status: str,
    errors,
    usage: dict[str, int | None],
    *,
    attempts: int,
    will_retry: bool,
) -> None:
    LOGGER.warning(
        "component=gemini_api task=%s model=%s status=%s attempts=%d "
        "retry=%s error_codes=%s input_tokens=%s output_tokens=%s "
        "thought_tokens=%s total_tokens=%s",
        task,
        model,
        provider_status,
        attempts,
        str(will_retry).lower(),
        _interaction_error_codes(errors),
        usage.get("input_tokens", "unknown"),
        usage.get("output_tokens", "unknown"),
        usage.get("thought_tokens", "unknown"),
        usage.get("total_tokens", "unknown"),
    )


def _log_usage(task: str, model: str, usage) -> None:
    if not isinstance(usage, dict):
        return
    LOGGER.info(
        "component=gemini_api task=%s model=%s input_tokens=%s output_tokens=%s "
        "thought_tokens=%s total_tokens=%s",
        task,
        model,
        usage.get("total_input_tokens", "unknown"),
        usage.get("total_output_tokens", "unknown"),
        usage.get("total_thought_tokens", "unknown"),
        usage.get("total_tokens", "unknown"),
    )
