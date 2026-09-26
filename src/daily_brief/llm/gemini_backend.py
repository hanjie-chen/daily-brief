from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, time as datetime_time, timedelta
from http.client import HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ..models import Candidate
from .gemini_api import (
    MAX_RESPONSE_BYTES,
    GeminiAPIError,
    GeminiConfigurationError,
    GeminiResponseError,
    extract_output_text,
    http_error_code,
    http_error_message,
    is_daily_quota_error,
    is_retryable_status,
    log_non_completed_interaction,
    log_usage,
    normalized_usage,
    response_status,
    retry_delay_from_error,
)
from .summarizer import (
    MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS,
    MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS,
    MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS,
    MAX_INSUFFICIENT_REASON_CHARS,
    COMMUNITY_ROUNDUP_SYSTEM_INSTRUCTION,
    SUMMARY_MODE_COMMUNITY_ROUNDUP,
    SUMMARY_SYSTEM_INSTRUCTION,
    InsufficientSummaryMaterial,
    build_summary_prompt,
    has_discussion_source,
    HN_DISCUSSION_SUMMARY_PREFIX,
    route_summary_mode,
    source_summary_prefix,
)
from .topic_classifier import (
    TOPIC_CLASSIFIER_SYSTEM_INSTRUCTION,
    TOPIC_LABELS,
    build_topic_classifier_prompt,
)

LOGGER = logging.getLogger(__name__)

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_CLASSIFIER_MODEL = "gemini-3.5-flash-lite"
DEFAULT_SUMMARIZER_MODEL = "gemini-3.6-flash"
DEFAULT_SUMMARIZER_FALLBACK_MODELS = ("gemini-3.7-flash", "gemini-3.8-flash")
DEFAULT_CLASSIFIER_MIN_REQUEST_INTERVAL_SECONDS = 6.0
DEFAULT_SUMMARIZER_MIN_REQUEST_INTERVAL_SECONDS = 20.0
MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MAX_SUMMARY_CHARS = 1000
CLASSIFIER_MAX_OUTPUT_TOKENS = 512
SUMMARY_MAX_OUTPUT_TOKENS = 8192
SUMMARY_THINKING_LEVEL = "high"
SUMMARY_INCOMPLETE_RETRIES = 1


def _classifier_labels(candidates: list[Candidate]) -> set[str]:
    labels = set(TOPIC_LABELS) - {"community_roundup"}
    if any(
        candidate.story.source_url == candidate.story.hn_discussion_url
        and candidate.content_kind != "community_roundup"
        for candidate in candidates
    ):
        labels.add("community_roundup")
    return labels


def _validate_and_format_community_roundup(output: object) -> str:
    expected_keys = {"status", "introduction", "entries", "reason"}
    if not isinstance(output, dict) or set(output) != expected_keys:
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if not isinstance(output["status"], str) or output["status"] not in {
        "sufficient",
        "insufficient",
    }:
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if not isinstance(output["introduction"], str) or not isinstance(output["reason"], str):
        raise GeminiResponseError("Gemini community roundup returned an invalid object")
    if any("\n" in value or "\r" in value for value in (output["introduction"], output["reason"])):
        raise GeminiResponseError("Gemini community roundup returned a multiline field")

    introduction = output["introduction"].strip()
    reason = output["reason"].strip()
    entries = output["entries"]
    if len(output["reason"]) > MAX_INSUFFICIENT_REASON_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized reason")
    if len(introduction) > MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized introduction")
    if output["status"] == "insufficient":
        if introduction or entries != [] or not reason:
            raise GeminiResponseError("Gemini community roundup returned an inconsistent decision")
        raise InsufficientSummaryMaterial(reason)
    if reason or not introduction or not isinstance(entries, list) or not 2 <= len(entries) <= 3:
        raise GeminiResponseError("Gemini community roundup returned an inconsistent decision")

    formatted_entries = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"name", "description"}:
            raise GeminiResponseError("Gemini community roundup returned an invalid entry")
        name = entry["name"]
        description = entry["description"]
        if not isinstance(name, str) or not isinstance(description, str):
            raise GeminiResponseError("Gemini community roundup returned an invalid entry")
        if "\n" in name or "\r" in name or "\n" in description or "\r" in description:
            raise GeminiResponseError("Gemini community roundup returned a multiline field")
        name = name.strip()
        description = description.strip()
        if not name or not description:
            raise GeminiResponseError("Gemini community roundup returned an empty entry")
        if len(name) > MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS or len(description) > MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS:
            raise GeminiResponseError("Gemini community roundup returned an oversized entry")
        formatted_entries.append(f"- {name}：{description}")

    summary = introduction + "\n\n" + "\n".join(formatted_entries)
    if len(summary) > MAX_SUMMARY_CHARS:
        raise GeminiResponseError("Gemini community roundup returned an oversized summary")
    return summary


class GeminiBackend:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        classifier_model: str = DEFAULT_CLASSIFIER_MODEL,
        summarizer_model: str = DEFAULT_SUMMARIZER_MODEL,
        summarizer_fallback_models: tuple[str, ...] = (),
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
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.api_key = api_key.strip()
        self.classifier_model = _validate_model(classifier_model)
        self.summarizer_model = _validate_model(summarizer_model)
        self._summary_models = tuple(dict.fromkeys((
            self.summarizer_model,
            *(_validate_model(model) for model in summarizer_fallback_models),
        )))
        self.summarizer_fallback_models = self._summary_models[1:]
        self.last_summary_model = self.summarizer_model
        self._summary_attempt_models: list[str] = []
        self._daily_exhausted_until: dict[str, float] = {}
        self._last_summary_request_started: float | None = None
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
            *((model, summarizer_min_request_interval_seconds)
              for model in self.summarizer_fallback_models),
        ):
            self._model_request_intervals[model] = max(
                interval,
                self._model_request_intervals.get(model, 0.0),
            )
        self.opener = opener
        self.sleeper = sleeper
        self.clock = clock
        self.wall_clock = wall_clock
        self.jitter = jitter
        self._last_request_started_by_model: dict[str, float] = {}
        self._last_request_attempts_by_model: dict[str, int] = {}
        self._last_response_status_by_model: dict[str, str] = {}
        self._last_response_usage_by_model: dict[str, dict[str, int | None]] = {}

    @property
    def last_summary_attempts(self) -> int:
        return sum(
            self._last_request_attempts_by_model.get(model, 0)
            for model in self._summary_attempt_models
        )

    @property
    def last_summary_provider_status(self) -> str:
        return self._last_response_status_by_model.get(self.last_summary_model, "")

    @property
    def last_summary_usage(self) -> dict[str, int | None]:
        return dict(self._last_response_usage_by_model.get(self.last_summary_model, {}))

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
        primary_model = environment.get(
            "DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL", DEFAULT_SUMMARIZER_MODEL
        )
        if "summarizer_fallback_models" not in kwargs:
            default_fallbacks = (
                ",".join(DEFAULT_SUMMARIZER_FALLBACK_MODELS)
                if primary_model.strip() == DEFAULT_SUMMARIZER_MODEL else ""
            )
            value = environment.get(
                "DAILY_BRIEF_GEMINI_SUMMARIZER_FALLBACK_MODELS", default_fallbacks
            )
            kwargs["summarizer_fallback_models"] = (
                tuple(value.split(",")) if value.strip() else ()
            )
        return cls(
            api_key=environment.get("GEMINI_API_KEY", ""),
            classifier_model=environment.get(
                "DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL
            ),
            summarizer_model=primary_model,
            classifier_min_request_interval_seconds=classifier_interval,
            summarizer_min_request_interval_seconds=summarizer_interval,
            **kwargs,
        )

    def classify(self, candidates: list[Candidate]) -> dict[str, str]:
        if not candidates:
            return {}
        self._reset_request_diagnostics(self.classifier_model)
        allowed_ids = [candidate.story.hn_item_id for candidate in candidates]
        allowed_labels = _classifier_labels(candidates)
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
                                    "enum": sorted(allowed_labels),
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
        if any(item["label"] not in allowed_labels for item in decisions):
            raise GeminiResponseError("Gemini classifier returned unknown labels")
        candidates_by_id = {candidate.story.hn_item_id: candidate for candidate in candidates}
        if any(
            item["label"] == "community_roundup"
            and (
                candidates_by_id[item["id"]].story.source_url
                != candidates_by_id[item["id"]].story.hn_discussion_url
                or candidates_by_id[item["id"]].content_kind == "community_roundup"
            )
            for item in decisions
        ):
            raise GeminiResponseError("Gemini classifier returned invalid community roundup")
        return {item["id"]: item["label"] for item in decisions}

    def summarize(self, candidate: Candidate) -> str:
        self._summary_attempt_models = []
        self.last_summary_model = ""
        for model in self._summary_models:
            self._reset_request_diagnostics(model)
        last_error = None
        for model in self._summary_models:
            if self._daily_exhausted_until.get(model, 0) > self.wall_clock():
                continue
            self.last_summary_model = model
            self._summary_attempt_models.append(model)
            try:
                return self._summarize_with_model(candidate, model)
            except GeminiAPIError as exc:
                if exc.error_code == "daily_quota_exceeded":
                    now = datetime.fromtimestamp(
                        self.wall_clock(), ZoneInfo("America/Los_Angeles")
                    )
                    reset = datetime.combine(
                        now.date() + timedelta(days=1), datetime_time(), now.tzinfo
                    )
                    self._daily_exhausted_until[model] = reset.timestamp()
                elif exc.error_code not in {"provider_unavailable", "timeout", "network_error"}:
                    raise
                last_error = exc
                LOGGER.warning(
                    "component=gemini_api task=summarize model=%s error_code=%s "
                    "action=summary_model_unavailable", model, exc.error_code,
                )
        if last_error is not None:
            raise last_error
        raise GeminiAPIError(
            "All summary models have exhausted their daily quota",
            error_code="daily_quota_exceeded", http_status=429,
        )

    def _summarize_with_model(self, candidate: Candidate, model: str) -> str:
        combined = has_discussion_source(candidate)
        community_roundup = route_summary_mode(candidate) == SUMMARY_MODE_COMMUNITY_ROUNDUP
        summary_schema = (
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["sufficient", "insufficient"]},
                    "introduction": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_INTRODUCTION_CHARS},
                    "entries": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_ENTRY_NAME_CHARS},
                                "description": {"type": "string", "maxLength": MAX_COMMUNITY_ROUNDUP_ENTRY_DESCRIPTION_CHARS},
                            },
                            "required": ["name", "description"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string", "maxLength": MAX_INSUFFICIENT_REASON_CHARS},
                },
                "required": ["status", "introduction", "entries", "reason"],
                "additionalProperties": False,
            }
            if community_roundup
            else {
                "type": "object",
                "properties": {
                    **({"source_summary": {"type": "string"}} if combined else {}),
                    "status": {
                        "type": "string",
                        "enum": ["sufficient", "insufficient"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "A concise Chinese summary, or empty if insufficient.",
                    },
                    "reason": {
                        "type": "string",
                        "maxLength": MAX_INSUFFICIENT_REASON_CHARS,
                        "description": "Why material is insufficient; empty if sufficient.",
                    },
                },
                "required": ["status", "summary", "reason"] + (["source_summary"] if combined else []),
                "additionalProperties": False,
            }
        )
        output = self._interact(
            task="summarize",
            model=model,
            system_instruction=(
                COMMUNITY_ROUNDUP_SYSTEM_INSTRUCTION
                if community_roundup
                else SUMMARY_SYSTEM_INSTRUCTION
            ),
            prompt=build_summary_prompt(candidate),
            schema=summary_schema,
            max_output_tokens=SUMMARY_MAX_OUTPUT_TOKENS,
            thinking_level=SUMMARY_THINKING_LEVEL,
            incomplete_retries=SUMMARY_INCOMPLETE_RETRIES,
        )
        if community_roundup:
            return _validate_and_format_community_roundup(output)
        if (
            set(output) != ({"status", "summary", "reason"} | ({"source_summary"} if combined else set()))
            or not all(isinstance(value, str) for value in output.values())
            or output["status"] not in {"sufficient", "insufficient"}
        ):
            raise GeminiResponseError("Gemini summarizer returned an invalid object")
        summary = output["summary"].strip()
        source_summary = output.get("source_summary", "").strip()
        reason = output["reason"].strip()
        if len(output["reason"]) > MAX_INSUFFICIENT_REASON_CHARS:
            raise GeminiResponseError("Gemini summarizer returned an oversized reason")
        if output["status"] == "insufficient":
            if summary or source_summary or not reason:
                raise GeminiResponseError("Gemini summarizer returned an inconsistent decision")
            raise InsufficientSummaryMaterial(reason)
        if reason:
            raise GeminiResponseError("Gemini summarizer returned an inconsistent decision")
        if not summary and not source_summary:
            raise GeminiResponseError("Gemini summarizer returned an empty summary")
        if combined:
            parts = []
            if source_summary:
                parts.append(source_summary_prefix(candidate) + source_summary)
            if summary:
                parts.append(HN_DISCUSSION_SUMMARY_PREFIX + summary)
            summary = " ".join(parts)
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
            provider_status = response_status(response)
            usage = normalized_usage(response.get("usage"))
            self._last_response_status_by_model[model] = provider_status
            self._last_response_usage_by_model[model] = usage
            if provider_status == "completed":
                break
            will_retry = (
                provider_status == "incomplete"
                and incomplete_retry < incomplete_retries
            )
            log_non_completed_interaction(
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
        text = extract_output_text(response)
        try:
            output = json.loads(text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise GeminiResponseError(
                "Gemini returned invalid structured JSON"
            ) from exc
        if not isinstance(output, dict):
            raise GeminiResponseError("Gemini structured output must be an object")
        log_usage(task, model, response.get("usage"))
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
                if exc.code == 429 and is_daily_quota_error(error_body):
                    raise GeminiAPIError(
                        http_error_message(exc.code, error_body),
                        error_code="daily_quota_exceeded", http_status=429,
                    ) from exc
                if is_retryable_status(exc.code) and attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt, exc.headers, error_body))
                    continue
                raise GeminiAPIError(
                    http_error_message(exc.code, error_body),
                    error_code=http_error_code(exc.code),
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
        remaining = 0.0
        if last_request_started is not None:
            remaining = self._model_request_intervals[model] - (
                now - last_request_started
            )
        if model in self._summary_models and self._last_summary_request_started is not None:
            remaining = max(
                remaining,
                self.summarizer_min_request_interval_seconds
                - (now - self._last_summary_request_started),
            )
        if remaining > 0:
            self.sleeper(remaining)
            now = self.clock()
        self._last_request_started_by_model[model] = now
        if model in self._summary_models:
            self._last_summary_request_started = now

    def _retry_delay(
        self, attempt: int, headers, error_body: bytes | None = None
    ) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 0.0), 60.0)
            except ValueError:
                pass
        provider_delay = retry_delay_from_error(error_body)
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
