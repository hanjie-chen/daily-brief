"""Bounded OpenRouter chat-completions adapter sharing existing task contracts."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..models import Candidate
from .gemini_api import GeminiResponseError, MAX_RESPONSE_BYTES
from .gemini_output import (
    classification_schema, classifier_labels, summary_schema,
    validate_classification, validate_material_summary, validate_summary,
    validate_and_format_community_roundup,
)
from .summarizer import (
    COMMUNITY_ROUNDUP_SYSTEM_INSTRUCTION, SUMMARY_MODE_COMMUNITY_ROUNDUP,
    SUMMARY_SYSTEM_INSTRUCTION, build_summary_prompt, has_discussion_source,
    route_summary_mode, uses_material_summary,
)
from .topic_classifier import TOPIC_CLASSIFIER_SYSTEM_INSTRUCTION, build_topic_classifier_prompt

CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CLASSIFIER_MODEL = "qwen/qwen3.8-flash"
DEFAULT_SUMMARIZER_MODEL = "openai/gpt-6-luna"
CLASSIFIER_MAX_OUTPUT_TOKENS = 512
SUMMARY_MAX_OUTPUT_TOKENS = 8192
SUMMARY_REASONING_EFFORT = "medium"
MODEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}/[a-z0-9][a-z0-9._:-]{0,127}$")
LOGGER = logging.getLogger(__name__)


class OpenRouterConfigurationError(ValueError):
    pass


class OpenRouterAPIError(RuntimeError):
    def __init__(self, message, *, error_code="provider_error", http_status=None):
        super().__init__(message)
        self.error_code = error_code
        self.http_status = http_status


class OpenRouterResponseError(OpenRouterAPIError):
    def __init__(self, message, *, provider_status=""):
        super().__init__(message, error_code="invalid_response")
        self.provider_status = provider_status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _number(value, label, *, minimum=0, maximum=None):
    if isinstance(value, bool):
        raise OpenRouterConfigurationError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise OpenRouterConfigurationError(f"{label} must be numeric") from None
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise OpenRouterConfigurationError(f"{label} is outside its permitted range")
    return result


def _model(value):
    if not isinstance(value, str) or not MODEL_PATTERN.fullmatch(value.strip()):
        raise OpenRouterConfigurationError("OpenRouter model ID is invalid")
    return value.strip()


def _usage(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens"), ("total_tokens", "total_tokens")):
        count = value.get(source)
        result[target] = count if type(count) is int and count >= 0 else None
    for target, section, source in (("thought_tokens", "completion_tokens_details", "reasoning_tokens"), ("cached_tokens", "prompt_tokens_details", "cached_tokens"), ("cache_write_tokens", "prompt_tokens_details", "cache_write_tokens")):
        details = value.get(section)
        count = details.get(source) if isinstance(details, dict) else None
        result[target] = count if type(count) is int and count >= 0 else None
    cost = value.get("cost")
    result["cost_usd"] = cost if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0 else None
    return result


def _status_code(code):
    return "quota_exceeded" if code == 429 else "provider_unavailable" if 500 <= code <= 599 else f"http_{code}"


class OpenRouterBackend:
    name = "openrouter"

    def __init__(self, *, api_key, classifier_model=DEFAULT_CLASSIFIER_MODEL,
                 summarizer_model=DEFAULT_SUMMARIZER_MODEL, timeout_seconds=90,
                 max_retries=2, retry_base_seconds=1.0,
                 classifier_min_request_interval_seconds=1.0,
                 summarizer_min_request_interval_seconds=1.0,
                 min_request_interval_seconds=None, max_run_cost_usd=0.25,
                 classifier_input_price_per_million=None, classifier_output_price_per_million=None,
                 summarizer_input_price_per_million=None, summarizer_output_price_per_million=None,
                 opener=None, sleeper=time.sleep, clock=time.monotonic):
        if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY is not configured or invalid")
        self.api_key = api_key.strip()
        self.classifier_model = _model(classifier_model)
        self.summarizer_model = _model(summarizer_model)
        self.timeout_seconds = _number(timeout_seconds, "OpenRouter timeout", minimum=0.001, maximum=300)
        retries = _number(max_retries, "OpenRouter max retries", maximum=5)
        if retries != int(retries):
            raise OpenRouterConfigurationError("OpenRouter max retries must be an integer")
        self.max_retries = int(retries)
        self.retry_base_seconds = _number(retry_base_seconds, "OpenRouter retry base", maximum=60)
        if min_request_interval_seconds is not None:
            classifier_min_request_interval_seconds = summarizer_min_request_interval_seconds = min_request_interval_seconds
        self.classifier_min_request_interval_seconds = _number(classifier_min_request_interval_seconds, "OpenRouter classifier interval", maximum=300)
        self.summarizer_min_request_interval_seconds = _number(summarizer_min_request_interval_seconds, "OpenRouter summary interval", maximum=300)
        self.max_run_cost_usd = _number(max_run_cost_usd, "OpenRouter run budget", minimum=0.000001)
        self._prices = {}
        for role, model, default_model, input_price, output_price, defaults in (
            ("classify", self.classifier_model, DEFAULT_CLASSIFIER_MODEL, classifier_input_price_per_million, classifier_output_price_per_million, (0.15, 0.47)),
            ("summarize", self.summarizer_model, DEFAULT_SUMMARIZER_MODEL, summarizer_input_price_per_million, summarizer_output_price_per_million, (0.10, 0.50)),
        ):
            if model != default_model and (input_price is None or output_price is None):
                raise OpenRouterConfigurationError("Custom OpenRouter models require explicit input and output price caps")
            self._prices[role] = (_number(defaults[0] if input_price is None else input_price, "OpenRouter input price", minimum=0.000001), _number(defaults[1] if output_price is None else output_price, "OpenRouter output price", minimum=0.000001))
        self.opener = opener if opener is not None else build_opener(_NoRedirect()).open
        self.sleeper, self.clock = sleeper, clock
        self.accounted_cost_usd = 0.0
        self.request_records: list[dict] = []
        self._last_started = {}
        self.last_summary_reasoning_effort = None
        self.last_summary_model = self.summarizer_model
        self.last_summary_attempts = 0
        self.last_summary_provider_status = ""
        self.last_summary_usage = {}
        self.last_summary_provider = ""

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None, **kwargs):
        environment = os.environ if env is None else env
        options = {"api_key": environment.get("OPENROUTER_API_KEY", "")}
        for key in ("classifier_model", "summarizer_model", "timeout_seconds", "max_retries", "retry_base_seconds", "min_request_interval_seconds", "classifier_min_request_interval_seconds", "summarizer_min_request_interval_seconds", "max_run_cost_usd", "classifier_input_price_per_million", "classifier_output_price_per_million", "summarizer_input_price_per_million", "summarizer_output_price_per_million"):
            value = environment.get("DAILY_BRIEF_OPENROUTER_" + key.upper())
            if value is not None:
                options[key] = value
        options.update(kwargs)
        return cls(**options)

    def classify(self, candidates: list[Candidate]) -> dict[str, str]:
        if not candidates:
            return {}
        ids = [candidate.story.hn_item_id for candidate in candidates]
        labels = classifier_labels(candidates)
        output = self._interact("classify", self.classifier_model, TOPIC_CLASSIFIER_SYSTEM_INSTRUCTION,
                                build_topic_classifier_prompt(candidates, "Return one JSON object with a decisions array. Do not include Markdown or explanations."), classification_schema(ids, labels), CLASSIFIER_MAX_OUTPUT_TOKENS)
        try:
            return validate_classification(output, candidates, ids, labels)
        except GeminiResponseError:
            raise OpenRouterResponseError("OpenRouter classifier returned invalid decisions") from None

    def summarize(self, candidate: Candidate) -> str:
        self.last_summary_reasoning_effort = None
        self.last_summary_model = self.summarizer_model
        self.last_summary_attempts = 0
        self.last_summary_provider_status = ""
        self.last_summary_usage = {}
        self.last_summary_provider = ""
        materials = uses_material_summary(candidate)
        if materials:
            candidate.summary_sources_used = []
        combined = has_discussion_source(candidate)
        roundup = not materials and route_summary_mode(candidate) == SUMMARY_MODE_COMMUNITY_ROUNDUP
        output = self._interact("summarize", self.summarizer_model,
                                COMMUNITY_ROUNDUP_SYSTEM_INSTRUCTION if roundup else SUMMARY_SYSTEM_INSTRUCTION,
                                build_summary_prompt(candidate), summary_schema(community_roundup=roundup, combined=combined, materials=materials), SUMMARY_MAX_OUTPUT_TOKENS)
        try:
            if roundup:
                return validate_and_format_community_roundup(output)
            if materials:
                return validate_material_summary(output, candidate)
            return validate_summary(output, candidate, combined=combined)
        except GeminiResponseError:
            raise OpenRouterResponseError("OpenRouter summarizer returned an invalid decision") from None

    def _interact(self, task, model, system, prompt, schema, output_limit):
        input_price, output_price = self._prices[task]
        payload = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                   "max_tokens": output_limit, "stream": False,
                   "reasoning": {"enabled": False} if task == "classify" else {"effort": SUMMARY_REASONING_EFFORT, "exclude": True},
                   "response_format": {"type": "json_schema", "json_schema": {"name": "daily_brief_" + task, "strict": True, "schema": schema}},
                   "provider": {"require_parameters": True, "allow_fallbacks": True,
                                "max_price": {"prompt": input_price, "completion": output_price}}}
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        # UTF-8 bytes upper-bound ordinary tokenization; overhead covers chat/schema framing.
        input_bound = len((system + prompt + json.dumps(schema, ensure_ascii=False)).encode("utf-8")) + 4096
        reserve_input_price = max(input_price, 0.125) if model == DEFAULT_SUMMARIZER_MODEL else input_price
        reservation = (input_bound * reserve_input_price + output_limit * output_price) / 1_000_000
        for attempt in range(self.max_retries + 1):
            if self.accounted_cost_usd + reservation > self.max_run_cost_usd:
                raise OpenRouterAPIError("OpenRouter run budget exhausted", error_code="budget_exceeded")
            self.accounted_cost_usd += reservation
            record = {"task": task, "model": model, "attempt": attempt + 1,
                      "reserved_usd": reservation, "cost_usd": None, "status": "pending"}
            self.request_records.append(record)
            if task == "summarize":
                self.last_summary_reasoning_effort = payload["reasoning"]["effort"]
                record["reasoning_effort"] = self.last_summary_reasoning_effort
                self.last_summary_attempts += 1
                self.last_summary_provider_status = ""
                self.last_summary_usage = {}
                self.last_summary_provider = ""
            interval = self.classifier_min_request_interval_seconds if task == "classify" else self.summarizer_min_request_interval_seconds
            now = self.clock()
            remaining = interval - (now - self._last_started.get(model, now - interval))
            if remaining > 0:
                self.sleeper(remaining)
            self._last_started[model] = self.clock()
            request = Request(CHAT_COMPLETIONS_URL, data=body, headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key, "User-Agent": "daily-brief/0.1"}, method="POST")
            try:
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    if hasattr(response, "geturl") and response.geturl() != CHAT_COMPLETIONS_URL:
                        raise OpenRouterResponseError("OpenRouter response redirected")
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as exc:
                code = exc.code
                record.update(status="http_error", http_status=code)
                delay = self._retry_delay(attempt, exc.headers)
                # Do not retain provider-supplied text or the request-bearing exception.
                exc.close()
                if (code == 429 or 500 <= code <= 599) and attempt < self.max_retries:
                    self.sleeper(delay)
                    continue
                raise OpenRouterAPIError(f"OpenRouter API HTTP {code}", error_code=_status_code(code), http_status=code) from None
            except (TimeoutError, URLError, ConnectionError, OSError) as exc:
                code = "timeout" if isinstance(exc, TimeoutError) else "network_error"
                record["status"] = code
                if attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt))
                    continue
                raise OpenRouterAPIError("OpenRouter request failed", error_code=code) from None
            record["status"] = "invalid_response"
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OpenRouterResponseError("OpenRouter response exceeded the size limit")
            try:
                response = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise OpenRouterResponseError("OpenRouter API returned invalid JSON") from None
            if not isinstance(response, dict):
                raise OpenRouterResponseError("OpenRouter API response must be an object")
            usage = _usage(response.get("usage"))
            cost = usage.get("cost_usd")
            if cost is not None:
                self.accounted_cost_usd = max(0.0, self.accounted_cost_usd - reservation + cost)
            record.update(usage)
            provider = response.get("provider")
            if isinstance(provider, str) and re.fullmatch(r"[A-Za-z0-9 ._-]{1,80}", provider) and self.api_key not in provider:
                record["provider"] = provider
            LOGGER.info("component=openrouter task=%s model=%s attempt=%d input_tokens=%s output_tokens=%s cost_usd=%s accounted_usd=%.6f",
                        task, model, attempt + 1, usage.get("input_tokens"), usage.get("output_tokens"), cost, self.accounted_cost_usd)
            if task == "summarize":
                self.last_summary_usage = usage
                provider = response.get("provider")
                self.last_summary_provider = record.get("provider", "")
            error = response.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                if type(code) is int and (code == 429 or 500 <= code <= 599) and attempt < self.max_retries:
                    self.sleeper(self._retry_delay(attempt))
                    continue
                raise OpenRouterAPIError("OpenRouter returned a provider error", error_code=_status_code(code) if type(code) is int else "provider_error", http_status=code if type(code) is int else None)
            choices = response.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise OpenRouterResponseError("OpenRouter returned invalid choices")
            choice = choices[0]
            finish = choice.get("finish_reason")
            status = finish if isinstance(finish, str) and finish in {"stop", "length", "content_filter", "error", "tool_calls"} else "unknown"
            record["status"] = status
            if task == "summarize":
                self.last_summary_provider_status = status
            if status != "stop":
                raise OpenRouterResponseError("OpenRouter completion did not finish", provider_status=status)
            message = choice.get("message")
            if not isinstance(message, dict) or message.get("refusal") or not isinstance(message.get("content"), str):
                raise OpenRouterResponseError("OpenRouter returned no usable completion")
            try:
                output = json.loads(message["content"])
            except ValueError:
                raise OpenRouterResponseError("OpenRouter returned invalid structured JSON") from None
            if not isinstance(output, dict):
                raise OpenRouterResponseError("OpenRouter structured output must be an object")
            return output
        raise AssertionError("OpenRouter retry loop ended unexpectedly")

    def _retry_delay(self, attempt, headers=None):
        value = headers.get("Retry-After") if headers is not None else None
        try:
            delay = float(value)
        except (TypeError, ValueError):
            delay = self.retry_base_seconds * (2 ** attempt)
        if not math.isfinite(delay):
            delay = self.retry_base_seconds * (2 ** attempt)
        return min(max(delay, 0), 60)
