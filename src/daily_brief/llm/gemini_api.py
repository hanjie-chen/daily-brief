"""Gemini Interactions API wire details: errors, HTTP error interpretation,
retry delays, response text and usage extraction, and interaction logging."""

from __future__ import annotations

import json
import logging
import re

LOGGER = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 256 * 1024
RETRYABLE_HTTP_STATUSES = {408, 429}
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


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_HTTP_STATUSES or 500 <= status <= 599


def http_error_code(status: int) -> str:
    if status == 429:
        return "quota_exceeded"
    if status == 408:
        return "timeout"
    if 500 <= status <= 599:
        return "provider_unavailable"
    return f"http_{status}"


def is_daily_quota_error(body: bytes) -> bool:
    """Only explicit daily limits justify switching models without waiting.

    An unqualified 429 can be RPM/TPM or another quota; never guess that it
    means the daily request budget is exhausted.
    """
    if len(body) > MAX_RESPONSE_BYTES:
        return False
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return False
    details = error.get("details", [])
    if isinstance(details, list):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            violations = detail.get("violations", [])
            if not isinstance(violations, list):
                continue
            for violation in violations:
                if not isinstance(violation, dict):
                    continue
                for field in ("quotaId", "quotaMetric"):
                    value = violation.get(field)
                    if isinstance(value, str) and "perday" in re.sub(
                        r"[^a-z]", "", value.lower()
                    ):
                        return True
    message = error.get("message", "")
    return isinstance(message, str) and bool(re.search(
        r"\b(?:per[ _-]day|daily (?:request )?quota|requests? per day)\b",
        message, re.IGNORECASE,
    ))


def http_error_message(status: int, body: bytes) -> str:
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


def retry_delay_from_error(body: bytes | None) -> float | None:
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


def extract_output_text(response: dict) -> str:
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


def response_status(response: dict) -> str:
    status = response.get("status")
    if not isinstance(status, str) or not status.strip():
        return "unknown"
    return " ".join(status.split())[:128]


def normalized_usage(usage) -> dict[str, int | None]:
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


def log_non_completed_interaction(
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


def log_usage(task: str, model: str, usage) -> None:
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
