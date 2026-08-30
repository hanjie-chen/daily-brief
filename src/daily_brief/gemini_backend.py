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
DEFAULT_SUMMARIZER_MODEL = "gemini-3.5-flash-lite"
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 6.0
MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
RETRYABLE_HTTP_STATUSES = {408, 429}
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 1000
CLASSIFIER_MAX_OUTPUT_TOKENS = 512
SUMMARY_MAX_OUTPUT_TOKENS = 2048
RETRY_DELAY_PATTERN = re.compile(r"^(\d+)(?:\.(\d{1,9}))?s$")
RETRY_MESSAGE_PATTERN = re.compile(
    r"(?:^|\s)Please retry in (\d+(?:\.\d{1,9})?)s(?:[.\s]|$)"
)


class GeminiConfigurationError(ValueError):
    pass


class GeminiAPIError(RuntimeError):
    pass


class GeminiResponseError(GeminiAPIError):
    pass


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
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
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
        if (
            not math.isfinite(min_request_interval_seconds)
            or min_request_interval_seconds < 0
        ):
            raise GeminiConfigurationError(
                "Gemini minimum request interval must not be negative"
            )
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.min_request_interval_seconds = min_request_interval_seconds
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock
        self.jitter = jitter
        self._last_request_started: float | None = None

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None, **kwargs
    ) -> GeminiBackend:
        environment = os.environ if env is None else env
        if "min_request_interval_seconds" in kwargs:
            min_request_interval_seconds = kwargs.pop(
                "min_request_interval_seconds"
            )
        else:
            interval_value = environment.get(
                "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS",
                str(DEFAULT_MIN_REQUEST_INTERVAL_SECONDS),
            )
            try:
                min_request_interval_seconds = float(interval_value)
            except ValueError as exc:
                raise GeminiConfigurationError(
                    "DAILY_BRIEF_GEMINI_MIN_REQUEST_INTERVAL_SECONDS must be numeric"
                ) from exc
        return cls(
            api_key=environment.get("GEMINI_API_KEY", ""),
            classifier_model=environment.get(
                "DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL
            ),
            summarizer_model=environment.get(
                "DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL", DEFAULT_SUMMARIZER_MODEL
            ),
            min_request_interval_seconds=min_request_interval_seconds,
            **kwargs,
        )

    def classify(self, candidates: list[Candidate]) -> dict[str, str]:
        if not candidates:
            return {}
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
    ) -> dict:
        response = self._post_json(
            {
                "model": model,
                "input": prompt,
                "system_instruction": system_instruction,
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
                "generation_config": {"max_output_tokens": max_output_tokens},
                "store": False,
            }
        )
        if response.get("status") != "completed":
            raise GeminiResponseError("Gemini interaction did not complete")
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

    def _post_json(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(self.max_retries + 1):
            self._wait_for_request_slot()
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
                raise GeminiAPIError(_http_error_message(exc.code, error_body)) from exc
            except (TimeoutError, URLError) as exc:
                if attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt, None))
                    continue
                raise GeminiAPIError(
                    f"Gemini API request failed after {attempt + 1} attempts"
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

    def _wait_for_request_slot(self) -> None:
        now = self.clock()
        if self._last_request_started is not None:
            remaining = self.min_request_interval_seconds - (
                now - self._last_request_started
            )
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self._last_request_started = now

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


def _is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


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
