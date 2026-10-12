"""In-process recovery for a daily run; no probes or persistent checkpoints."""

from __future__ import annotations

import errno
import logging
import socket
import ssl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError

LOGGER = logging.getLogger(__name__)
WAIT_SECONDS = 600
MAX_PAUSES = 6
_ACTIVE: ContextVar[NetworkRecovery | None] = ContextVar("network_recovery", default=None)
_SUSPENDED: ContextVar[bool] = ContextVar("network_recovery_suspended", default=False)
_FAILURES: ContextVar[tuple[NetworkFailures, ...]] = ContextVar("network_failures", default=())


def is_transient_network_error(error: BaseException) -> bool:
    """Use structured errors and explicit causes, never log provider messages."""
    seen: set[int] = set()
    current: BaseException | None = error
    earlier_transport_failure = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        reason = getattr(current, "fallback_reason", "")
        earlier_transport_failure |= reason in {"request_failed", "network_timeout"} or _transient_http_code(reason)
        if isinstance(current, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
            return True
        if isinstance(current, ssl.SSLError):
            return earlier_transport_failure
        if isinstance(current, socket.gaierror):
            return earlier_transport_failure or current.errno == socket.EAI_AGAIN
        if isinstance(current, HTTPError):
            return earlier_transport_failure or current.code == 408 or 500 <= current.code <= 599
        if isinstance(current, URLError):
            if isinstance(current.reason, BaseException):
                current = current.reason
                continue
            return True
        # Prefer a typed cause over a wrapper's generic transport code.
        if current.__cause__ is not None:
            current = current.__cause__
            continue
        code = getattr(current, "error_code", "")
        if code == "retrieval_budget_exhausted" and current.__context__ is not None:
            current = current.__context__
            continue
        if code in {"network_error", "network_timeout", "request_failed", "timeout", "provider_unavailable"}:
            return True
        if _transient_http_code(code):
            return True
        status = getattr(current, "http_status", None)
        if isinstance(status, int):
            return earlier_transport_failure or status == 408 or 500 <= status <= 599
        if isinstance(current, (TimeoutError, ConnectionError, IncompleteRead, RemoteDisconnected)):
            return True
        if isinstance(current, OSError):
            return earlier_transport_failure or current.errno in {
                errno.ENETDOWN, errno.ENETUNREACH, errno.ENETRESET,
                errno.ECONNABORTED, errno.ECONNRESET, errno.ECONNREFUSED,
                errno.EHOSTUNREACH, errno.ETIMEDOUT, errno.EPIPE,
            }
        return earlier_transport_failure
    return earlier_transport_failure


def _transient_http_code(code) -> bool:
    if not isinstance(code, str) or not code.startswith("http_") or not code[5:].isdigit():
        return False
    status = int(code[5:])
    return status == 408 or 500 <= status <= 599


def is_local_network_error(error: BaseException) -> bool:
    """Material hosts are optional; only explicit local resolver/route faults pause.

    A timeout, refused connection or HTTP 5xx can belong to one remote host.
    Generic error codes and fallback reasons cannot establish a local fault.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (HTTPError, ssl.SSLError)):
            return False
        if isinstance(current, socket.gaierror):
            return current.errno == socket.EAI_AGAIN
        if isinstance(current, OSError) and current.errno in {errno.ENETDOWN, errno.ENETUNREACH}:
            return True
        if isinstance(current, URLError) and isinstance(current.reason, BaseException):
            current = current.reason
        elif current.__cause__ is not None:
            current = current.__cause__
        elif getattr(current, "error_code", "") == "retrieval_budget_exhausted":
            current = current.__context__
        else:
            return False
    return False


class NetworkRecovery:
    """One shared allowance across collection, materials, models and publishing."""

    def __init__(self, *, sleeper: Callable[[float], None] | None = None,
                 max_pauses: int = MAX_PAUSES) -> None:
        if not isinstance(max_pauses, int) or max_pauses < 0:
            raise ValueError("max_pauses must be a nonnegative integer")
        self.sleeper = sleeper if sleeper is not None else time.sleep
        self.max_pauses = max_pauses
        self.pauses = 0
        self.exhausted = False

    def __enter__(self) -> NetworkRecovery:
        if _ACTIVE.get() is not None:
            raise RuntimeError("network recovery scopes must not reset an active run")
        self._token = _ACTIVE.set(self)
        return self

    def __exit__(self, *exc) -> None:
        _ACTIVE.reset(self._token)

    def wait(self, operation: str) -> bool:
        if self.pauses >= self.max_pauses:
            self.exhausted = True
            LOGGER.error("component=network_recovery status=exhausted operation=%s pauses=%d", operation, self.pauses)
            return False
        self.pauses += 1
        LOGGER.warning("component=network_recovery status=waiting operation=%s pause=%d/%d wait_seconds=%d", operation, self.pauses, self.max_pauses, WAIT_SECONDS)
        self.sleeper(WAIT_SECONDS)
        LOGGER.info("component=network_recovery status=resuming operation=%s pause=%d", operation, self.pauses)
        return True


def wait_for_network(operation: str) -> bool:
    active = _ACTIVE.get()
    return active is not None and not _SUSPENDED.get() and active.wait(operation)


def network_recovery_exhausted() -> bool:
    active = _ACTIVE.get()
    return active is not None and active.exhausted


@contextmanager
def suspend_network_recovery() -> Iterator[None]:
    """Try optional sources first, recording faults without delaying fallbacks."""
    token = _SUSPENDED.set(True)
    try:
        yield
    finally:
        _SUSPENDED.reset(token)


class NetworkFailures:
    """Observe faults eligible for a shared pause, even across material fallbacks."""

    def __init__(self) -> None:
        self.transient = False

    def __enter__(self) -> NetworkFailures:
        self._token = _FAILURES.set((*_FAILURES.get(), self))
        return self

    def __exit__(self, *exc) -> None:
        _FAILURES.reset(self._token)


def observe_network_failure(error: BaseException, *, critical: bool = False) -> None:
    eligible = is_transient_network_error(error) if critical else is_local_network_error(error)
    if eligible:
        for failures in _FAILURES.get():
            failures.transient = True


def recoverable_network(operation: str):
    """Retry only this failed operation, after its normal short retries finish."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            while True:
                try:
                    return function(*args, **kwargs)
                except Exception as exc:
                    if not is_transient_network_error(exc):
                        raise
                    observe_network_failure(exc, critical=True)
                    if not wait_for_network(operation):
                        raise
        return wrapped
    return decorate
