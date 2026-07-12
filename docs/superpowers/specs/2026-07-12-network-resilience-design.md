# Daily Brief Network Resilience Design

## Background

The 2026-07-12 scheduled run reached both Hacker News data sources through the
configured Mihomo proxy but timed out during TLS handshakes. Each source was
attempted once, so a short-lived network-path failure produced an empty brief.
The program preserved a readable failure note, but the cron log did not contain
enough information to reconstruct the failure without inspecting the generated
Markdown and system journal.

## Goals

- Recover automatically from short-lived HTTP and TLS failures.
- Make every retry and final source outcome visible in the cron log.
- Preserve the current partial and empty brief degradation behavior.
- Keep the implementation independent from Mihomo and other host-specific
  network infrastructure.
- Keep runtime dependencies at zero.

## Non-Goals

- Switching Mihomo nodes or modifying proxy configuration.
- Bypassing the proxy or forcing direct connections.
- Backing up, validating, or preserving an older same-day output.
- Adding notifications or external monitoring.
- Retrying summary generation or changing summary behavior.

## Retry Policy

Every JSON HTTP GET made by the Hacker News clients uses the same bounded retry
policy:

- Three total attempts, including the initial request.
- A 20-second timeout for each attempt.
- Wait 10 seconds after the first failure.
- Wait 20 seconds after the second failure.
- Do not wait after the third failure.
- Retry the network operation for any exception currently raised by the HTTP,
  TLS, decoding, or JSON parsing path. The existing clients already treat all of
  these failures as source failures, and the small fixed attempt count prevents
  unbounded retries.

The retry helper accepts an injectable sleep function so tests never wait in
real time. After the final failure it raises an error that retains the final
exception as its cause and identifies the source and number of attempts.

## Logging

Use Python's standard `logging` module. Logs go to stderr so the existing cron
redirection appends them to `logs/daily-brief.log`.

Retry warnings contain:

- source name;
- attempt number and total attempts;
- exception type and message;
- next retry delay.

The final request failure contains the same fields without a retry delay. Full
URLs are not logged, avoiding accidental exposure of future query parameters.

The CLI emits one aggregate completion record per source with its status,
elapsed time, and story count. It does not emit a success record for every HN
item request. A final run record includes selected item counts and output paths.

Human-readable key-value messages are preferred over JSON logs for this local
CLI. Timestamps and severity are supplied by the logging formatter.

## Data Flow And Degradation

The existing orchestration remains intact:

1. Fetch Algolia stories, with request-level retries.
2. Fetch HN official stories, with request-level retries.
3. Record each source's aggregate duration and result.
4. Continue matching, scoring, selection, summarization, and rendering.

If one source exhausts its retries, its section is empty and contains the
existing source-failure note while the other source continues normally. If both
sources fail, the program still writes an empty candidate JSON array and a brief
with both failure notes. This is considered a completed degraded run, so the CLI
continues to return exit status zero.

Summary failures retain the existing per-item fallback behavior.

## Components

### `hn_client.py`

- Owns retry constants and request retry execution.
- Logs retry warnings and the terminal request error.
- Keeps parsing and pagination behavior unchanged.
- Exposes injection points only where needed for deterministic tests.

### `cli.py`

- Configures the CLI logging format.
- Measures each source operation with a monotonic clock.
- Logs one aggregate outcome per source and one final run outcome.
- Continues to put user-facing source failure notes into Markdown.

## Testing

Tests use fake request operations, fake sleep functions, fake clocks, and the
existing fake clients and summarizers. They do not access the network or invoke
`codex exec`.

Coverage includes:

- success on the first attempt with no sleep;
- success on the second attempt after a 10-second delay;
- success on the third attempt after 10- and 20-second delays;
- terminal failure after exactly three attempts with no final sleep;
- retention of the final exception as the raised error's cause;
- retry log fields and aggregate source log fields;
- successful, partially degraded, and fully degraded CLI runs;
- bounded log volume for successful HN item fetching;
- the complete existing test suite.

## Deferred Work

If logs later show that bounded retries are insufficient, proxy-node health and
failover should be handled as a separate host-infrastructure design rather than
embedded into Daily Brief.
