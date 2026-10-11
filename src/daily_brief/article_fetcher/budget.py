"""Cooperative deadline and transport-request budget shared by one selected item.

Nested retrieval and search calls reuse the current context. Requests count at
an opener's entry, not per urllib redirect hop. The deadline clips transport
socket timeouts and is checked around calls; it cannot preempt CPU work or an
injected blocking function that ignores its timeout.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from .contracts import ArticleFetchError

DEFAULT_RETRIEVAL_TIMEOUT_SECONDS = 120
DEFAULT_RETRIEVAL_MAX_REQUESTS = 24


@dataclass
class RetrievalBudget:
    deadline: float
    max_requests: int
    clock: Callable[[], float]
    requests: int = 0

    def remaining_seconds(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            _exhausted()
        return remaining

    def reserve_request(self) -> float:
        remaining = self.remaining_seconds()
        if self.requests >= self.max_requests:
            _exhausted()
        self.requests += 1
        return remaining


def _exhausted() -> None:
    raise ArticleFetchError(
        "Retrieval budget exhausted", error_code="retrieval_budget_exhausted"
    )


_ACTIVE_BUDGET: ContextVar[RetrievalBudget | None] = ContextVar(
    "daily_brief_retrieval_budget", default=None
)


@contextmanager
def retrieval_budget(
    *,
    timeout_seconds: float = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
    max_requests: int = DEFAULT_RETRIEVAL_MAX_REQUESTS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[RetrievalBudget]:
    """Start a budget, or reuse the outer budget without resetting its limits."""
    active = _ACTIVE_BUDGET.get()
    if active is not None:
        yield active
        return
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if not isinstance(max_requests, int) or max_requests <= 0:
        raise ValueError("max_requests must be a positive integer")
    budget = RetrievalBudget(clock() + timeout_seconds, max_requests, clock)
    token = _ACTIVE_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE_BUDGET.reset(token)


def remaining_seconds() -> float | None:
    """Return the remaining deadline, raising on expiry; None means no scope."""
    budget = _ACTIVE_BUDGET.get()
    return None if budget is None else budget.remaining_seconds()


def check_budget(*, require_request: bool = False) -> None:
    """Check time, and optionally room for starting another recovery stage."""
    remaining_seconds()
    budget = _ACTIVE_BUDGET.get()
    if require_request and budget is not None and budget.requests >= budget.max_requests:
        _exhausted()


P = ParamSpec("P")
T = TypeVar("T")


def with_retrieval_budget(function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with retrieval_budget():
            return function(*args, **kwargs)

    return wrapped


class _BudgetedOpener:
    def __init__(self, opener: Callable[..., Any]) -> None:
        self.opener = opener

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        budget = _ACTIVE_BUDGET.get()
        if budget is None:
            return self.opener(*args, **kwargs)
        remaining = budget.reserve_request()
        # urllib's positional arguments are url, data, timeout. Most injected
        # openers use a keyword timeout; preserve both calling conventions.
        if len(args) >= 3:
            positional = list(args)
            timeout = positional[2]
            positional[2] = remaining if timeout is None else min(timeout, remaining)
            args = tuple(positional)
        else:
            timeout = kwargs.get("timeout")
            kwargs["timeout"] = remaining if timeout is None else min(timeout, remaining)
        try:
            response = self.opener(*args, **kwargs)
        except Exception:
            check_budget()
            raise
        try:
            check_budget()
        except ArticleFetchError:
            # A late response must not leak its transport resource.
            close = getattr(response, "close", None)
            if close is not None:
                with suppress(Exception):
                    close()
            raise
        return response


def budgeted_opener(opener: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap once, looking up the active item budget at each invocation."""
    return opener if isinstance(opener, _BudgetedOpener) else _BudgetedOpener(opener)


def budgeted_sleep(seconds: float, sleeper: Callable[[float], Any]) -> None:
    """Honor a wait in full or reject it; never shorten a server Retry-After."""
    remaining = remaining_seconds()
    if remaining is not None and seconds >= remaining:
        _exhausted()
    sleeper(seconds)
    check_budget()
