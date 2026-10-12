import errno
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from daily_brief.network_recovery import (
    NetworkFailures, NetworkRecovery, is_transient_network_error, is_local_network_error,
    observe_network_failure, recoverable_network, suspend_network_recovery,
)


@pytest.mark.parametrize("error", [
    TimeoutError(), ConnectionResetError(), OSError(errno.ENETUNREACH, "offline"),
    ssl.SSLEOFError(), URLError(ssl.SSLZeroReturnError()),
    URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure")),
    HTTPError("https://example.com", 503, "unavailable", {}, None),
    HTTPError("https://example.com", 408, "timeout", {}, None),
])
def test_transient_network_failures(error):
    assert is_transient_network_error(error)


@pytest.mark.parametrize("error", [
    TimeoutError(), ConnectionRefusedError(), ConnectionResetError(),
    URLError("offline"), ssl.SSLEOFError(),
    HTTPError("https://article.example", 503, "unavailable", {}, None),
    HTTPError("https://article.example", 408, "timeout", {}, None),
])
def test_same_error_has_different_scope_for_optional_material_and_critical_service(error):
    with NetworkFailures() as material_failure:
        observe_network_failure(error)
    with NetworkFailures() as service_failure:
        observe_network_failure(error, critical=True)
    assert not material_failure.transient
    assert service_failure.transient


@pytest.mark.parametrize("cause", [
    socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"),
    OSError(errno.ENETDOWN, "interface down"),
    OSError(errno.ENETUNREACH, "route unavailable"),
])
def test_local_network_signals_survive_wrappers(cause):
    from daily_brief.article_fetcher import ArticleFetchError
    error = ArticleFetchError("URL validation failed", error_code="unsafe_url")
    error.__cause__ = URLError(cause)
    assert is_local_network_error(error)
    with NetworkFailures() as failures:
        observe_network_failure(error)
    assert failures.transient


def test_generic_fallback_reason_does_not_establish_local_outage():
    from daily_brief.article_fetcher import ArticleFetchError
    error = ArticleFetchError("archive failed", error_code="wayback_no_capture", fallback_reason="request_failed")
    assert not is_local_network_error(error)
    with NetworkFailures() as failures:
        observe_network_failure(error)
    assert not failures.transient


def test_origin_failure_survives_a_definitively_unavailable_fallback():
    from daily_brief.article_fetcher import ArticleFetchError
    try:
        raise HTTPError("https://archive.example", 404, "no archive", {}, None)
    except HTTPError as exc:
        failure = ArticleFetchError("archive failed", error_code="wayback_http_404", fallback_reason="network_timeout")
        failure.__cause__ = exc
    assert is_transient_network_error(failure)


def test_retrieval_deadline_cannot_hide_the_network_failure_that_used_it_up():
    from daily_brief.article_fetcher import ArticleFetchError
    try:
        try:
            raise TimeoutError()
        except TimeoutError:
            raise ArticleFetchError("budget", error_code="retrieval_budget_exhausted")
    except ArticleFetchError as exc:
        assert is_transient_network_error(exc)


@pytest.mark.parametrize("error", [
    ValueError("invalid response"), FileNotFoundError(), PermissionError(),
    URLError(ssl.SSLCertVerificationError("certificate")),
    URLError(socket.gaierror(socket.EAI_NONAME, "missing name")),
    HTTPError("https://example.com", 401, "unauthorized", {}, None),
    HTTPError("https://example.com", 404, "missing", {}, None),
    HTTPError("https://example.com", 429, "quota", {}, None),
])
def test_non_network_or_permanent_failures_do_not_wait(error):
    waits = []

    @recoverable_network("test")
    def operation():
        raise error

    with NetworkRecovery(sleeper=waits.append):
        with pytest.raises(type(error)):
            operation()
    assert waits == []


def test_shared_allowance_preserves_completed_operations_and_does_not_leak(caplog):
    calls = []
    waits = []
    outcomes = iter(["first", TimeoutError("secret-provider-message"), "second",
                     ConnectionResetError(), "third", TimeoutError()])

    @recoverable_network("model")
    def operation(label):
        calls.append(label)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with NetworkRecovery(sleeper=waits.append, max_pauses=2) as recovery:
        assert operation(1) == "first"
        assert operation(2) == "second"
        assert operation(3) == "third"
        assert not recovery.exhausted
        with pytest.raises(TimeoutError):
            operation(4)
        assert recovery.exhausted
    assert calls == [1, 2, 2, 3, 3, 4]
    assert waits == [600, 600]
    assert "secret-provider-message" not in caplog.text
    # A standalone command retains its own short-retry behavior after scope exit.
    @recoverable_network("outside")
    def fail():
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        fail()
    assert waits == [600, 600]


def test_optional_source_faults_are_observed_without_waiting_or_exhausting():
    @recoverable_network("source")
    def fail():
        raise TimeoutError()

    with NetworkRecovery(sleeper=lambda _: pytest.fail("must try fallback"), max_pauses=0) as recovery:
        with NetworkFailures() as outer, suspend_network_recovery():
            with NetworkFailures() as inner:
                with pytest.raises(TimeoutError):
                    fail()
            assert inner.transient
        assert outer.transient
        assert not recovery.exhausted


def test_nested_scope_cannot_reset_allowance():
    with NetworkRecovery(sleeper=lambda _: None, max_pauses=0):
        with pytest.raises(RuntimeError):
            with NetworkRecovery():
                pass


def test_interrupted_wait_propagates_and_cleans_scope():
    def interrupt(_):
        raise KeyboardInterrupt()

    @recoverable_network("source")
    def fail():
        raise TimeoutError()

    with pytest.raises(KeyboardInterrupt):
        with NetworkRecovery(sleeper=interrupt):
            fail()
    with pytest.raises(TimeoutError):
        fail()
