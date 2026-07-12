# Daily Brief Network Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hacker News data fetching survive short-lived network failures and leave enough cron-log evidence to diagnose every degraded run.

**Architecture:** Add bounded request-level retries to the existing standard-library HTTP helper, with injectable sleeping for deterministic tests. Add source-level timing and outcome logging in the CLI while preserving the current Markdown degradation behavior and zero runtime dependencies.

**Tech Stack:** Python 3.12 standard library (`logging`, `time`, `urllib.request`, `json`), pytest.

---

## File Structure

- Modify `src/daily_brief/hn_client.py`: own retry policy, retry execution, safe request logging, and terminal request errors.
- Modify `src/daily_brief/cli.py`: configure logging, measure source operations, and log aggregate run outcomes.
- Modify `tests/test_hn_client.py`: verify retry counts, delays, exception chaining, and retry logs without real sleeping or network access.
- Modify `tests/test_cli.py`: verify source-level success/failure logs and degraded output behavior.
- Modify `README.md`: document retry behavior and the cron log fields operators should expect.

### Task 1: Bounded HTTP Retries

**Files:**
- Modify: `src/daily_brief/hn_client.py`
- Test: `tests/test_hn_client.py`

- [ ] **Step 1: Write failing retry tests**

Add response and opener fakes plus tests that call `_get_json` directly:

```python
import logging

import pytest

from daily_brief.hn_client import RequestFailedError, _get_json


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


def test_get_json_retries_with_configured_backoff(caplog):
    outcomes = [TimeoutError("first"), TimeoutError("second"), FakeResponse(b'{"ok": true}')]
    sleeps = []

    def opener(request, timeout):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with caplog.at_level(logging.WARNING, logger="daily_brief.hn_client"):
        result = _get_json(
            "https://hn.algolia.com/test",
            opener=opener,
            sleep=sleeps.append,
        )

    assert result == {"ok": True}
    assert sleeps == [10, 20]
    assert "source=algolia attempt=1/3" in caplog.text
    assert "retry_in=10s" in caplog.text
    assert "source=algolia attempt=2/3" in caplog.text
    assert "retry_in=20s" in caplog.text


def test_get_json_raises_after_exactly_three_attempts(caplog):
    attempts = []
    sleeps = []

    def opener(request, timeout):
        attempts.append(timeout)
        raise TimeoutError(f"failure {len(attempts)}")

    with caplog.at_level(logging.ERROR, logger="daily_brief.hn_client"):
        with pytest.raises(RequestFailedError, match="algolia request failed after 3 attempts") as raised:
            _get_json(
                "https://hn.algolia.com/test",
                opener=opener,
                sleep=sleeps.append,
            )

    assert attempts == [20, 20, 20]
    assert sleeps == [10, 20]
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert str(raised.value.__cause__) == "failure 3"
    assert "source=algolia attempt=3/3 status=failed" in caplog.text
```

Also add a first-attempt-success test asserting that `sleep` is never called.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
pytest tests/test_hn_client.py -q
```

Expected: collection fails because `RequestFailedError` is not defined or `_get_json` does not accept injected `opener` and `sleep` arguments.

- [ ] **Step 3: Implement the retry policy**

Add these definitions to `hn_client.py` and replace the single-attempt `_get_json`:

```python
import logging
import time
from collections.abc import Callable


LOGGER = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 20
RETRY_DELAYS_SECONDS = (10, 20)
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)


class RequestFailedError(RuntimeError):
    pass


def _source_name(url: str) -> str:
    return "algolia" if url.startswith(ALGOLIA_URL) else "hn_official"


def _get_json(
    url: str,
    *,
    opener=urlopen,
    sleep: Callable[[float], None] = time.sleep,
):
    source = _source_name(url)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = Request(url, headers={"User-Agent": "daily-brief/0.1"})
            with opener(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            if attempt == MAX_ATTEMPTS:
                LOGGER.error(
                    "source=%s attempt=%d/%d status=failed error=%s message=%s",
                    source,
                    attempt,
                    MAX_ATTEMPTS,
                    type(exc).__name__,
                    exc,
                )
                raise RequestFailedError(
                    f"{source} request failed after {MAX_ATTEMPTS} attempts: {exc}"
                ) from exc

            delay = RETRY_DELAYS_SECONDS[attempt - 1]
            LOGGER.warning(
                "source=%s attempt=%d/%d error=%s message=%s retry_in=%ss",
                source,
                attempt,
                MAX_ATTEMPTS,
                type(exc).__name__,
                exc,
                delay,
            )
            sleep(delay)
```

Do not log successful individual requests; `fetch_hot_stories` can make hundreds of them.

- [ ] **Step 4: Run client tests**

Run:

```bash
pytest tests/test_hn_client.py -q
```

Expected: all client tests pass.

- [ ] **Step 5: Commit bounded retries**

```bash
git add src/daily_brief/hn_client.py tests/test_hn_client.py
git commit -m "Add bounded retries for HN requests"
```

### Task 2: Source-Level Timing And Outcome Logs

**Files:**
- Modify: `src/daily_brief/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing aggregate logging tests**

Add tests using `caplog` and a deterministic clock:

```python
import logging


def test_run_generate_logs_source_success_and_completion(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        cli,
        "fetch_algolia_stories",
        lambda window: [story("1", "AI coding agent with Claude", points=40, comments=8)],
    )
    monkeypatch.setattr(cli, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 12.5, 20.0, 23.0]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief.cli"):
        run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
            clock=clock,
        )

    assert "source=algolia status=success stories=1 duration=2.500s" in caplog.text
    assert "source=hn_official status=success stories=0 duration=3.000s" in caplog.text
    assert "status=completed ai_items=1 hot_items=0" in caplog.text


def test_run_generate_logs_terminal_source_failure(tmp_path, monkeypatch, caplog):
    def raise_algolia_error(window):
        raise RuntimeError("algolia unavailable")

    monkeypatch.setattr(cli, "fetch_algolia_stories", raise_algolia_error)
    monkeypatch.setattr(cli, "fetch_hot_stories", lambda: [])
    clock = iter([10.0, 100.0, 200.0, 201.0]).__next__

    with caplog.at_level(logging.INFO, logger="daily_brief.cli"):
        result = run_generate(
            output_dir=tmp_path / "briefs",
            data_dir=tmp_path / "data",
            date_label="2026-07-08",
            summarizer=FakeSummarizer(),
            clock=clock,
        )

    assert "source=algolia status=failed duration=90.000s error=RuntimeError" in caplog.text
    assert "source=hn_official status=success stories=0 duration=1.000s" in caplog.text
    assert "AI data source failed" in result.brief_path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the focused CLI tests and verify they fail**

Run:

```bash
pytest tests/test_cli.py -q
```

Expected: the new tests fail because `run_generate` has no `clock` argument and no aggregate logs.

- [ ] **Step 3: Implement source operation logging**

Add `logging`, `time`, and `Callable` imports, define `LOGGER`, and add this helper:

```python
LOGGER = logging.getLogger(__name__)


def _fetch_source(
    source: str,
    fetch: Callable[[], list[Story]],
    failure_prefix: str,
    clock: Callable[[], float],
) -> tuple[list[Story], str]:
    started = clock()
    try:
        stories = fetch()
    except Exception as exc:
        duration = clock() - started
        LOGGER.error(
            "source=%s status=failed duration=%.3fs error=%s message=%s",
            source,
            duration,
            type(exc).__name__,
            exc,
        )
        return [], f"{failure_prefix} ({exc})."

    duration = clock() - started
    LOGGER.info(
        "source=%s status=success stories=%d duration=%.3fs",
        source,
        len(stories),
        duration,
    )
    return stories, ""
```

Add `clock: Callable[[], float] = time.monotonic` to `run_generate`. Replace each production fetch `try/except` with `_fetch_source`, using a lambda for the Algolia window argument. Keep injected `algolia_stories` and `hot_stories` paths unchanged because they are test/local inputs rather than network source operations.

After both files are written, log the final result:

```python
LOGGER.info(
    "status=completed ai_items=%d hot_items=%d brief=%s data=%s",
    len(ai_items),
    len(selected_hot_items),
    output_path,
    data_path,
)
```

Configure logging once in `main` before generation:

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
```

- [ ] **Step 4: Run CLI and full tests**

Run:

```bash
pytest tests/test_cli.py -q
pytest -q
```

Expected: all tests pass, including existing partial-source degradation tests.

- [ ] **Step 5: Commit aggregate logging**

```bash
git add src/daily_brief/cli.py tests/test_cli.py
git commit -m "Log HN source outcomes"
```

### Task 3: Operator Documentation And Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document retry and log behavior**

Add a Chinese subsection under `定时运行` explaining:

```markdown
### 网络失败与重试

每个 Hacker News HTTP 请求最多尝试 3 次。第一次失败后等待 10 秒，第二次失败后等待 20 秒；第三次仍失败时，该数据源降级为空，并在简报和 `logs/daily-brief.log` 中记录原因。一个数据源失败不会阻止另一个数据源继续生成；两个数据源都失败时仍会生成带失败说明的空简报。

日志会记录每次重试，以及 Algolia、HN official API 的总耗时、story 数量和最终状态。程序不会自动修改 Mihomo 节点或绕过本机代理。
```

- [ ] **Step 2: Run documentation and repository checks**

Run:

```bash
git diff --check
rg -n "最多尝试 3 次|10 秒|20 秒|Mihomo" README.md
pytest -q
```

Expected: no whitespace errors, all four documentation phrases are present, and the complete test suite passes.

- [ ] **Step 3: Run a dry-run CLI smoke test**

Run:

```bash
python -m daily_brief generate --dry-run
```

Expected: exit status 0 with no network access or file writes.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md
git commit -m "Document HN request retries"
```

- [ ] **Step 5: Review final history and worktree state**

Run:

```bash
git status --short --branch
git log --oneline -5
```

Expected: a clean worktree on the implementation branch with the design, retry, logging, and documentation commits visible.
