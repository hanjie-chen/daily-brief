# Article Fetcher

This package retrieves bounded, public article material and reports how that
material was obtained. Its package-level facade preserves the import path used
by the rest of the application:

```python
from daily_brief.article_fetcher import fetch_article
```

The package is responsible for transport, extraction, recovery, and retrieval
provenance. Decisions about candidate selection, Reuters-specific recovery, and
summary generation remain outside this package.

## Retrieval Flow

`fetch_article(...)` applies the selected `ArticleFetchPolicy` and follows this
bounded flow:

1. Validate that the requested URL resolves to public HTTP(S) addresses.
2. Route supported YouTube and GitHub URLs to their specialized transports;
   GitHub PDF blobs retain the same PDF extraction policy.
3. Otherwise perform the direct request, validate redirects and the final URL,
   and extract bounded HTML, text, or PDF content. HTML extraction includes
   separately labeled publisher metadata (title, description, og:description)
   and body text. Metadata is bounded and deduplicated, and only supplements
   nonempty body text. Empty HTML extraction still raises `empty_content` and
   follows the policy-controlled Jina recovery path, even when metadata is present.
   Recognizable LessWrong post markup is normalized before extraction: the
   article's `commentOnSelection` wrapper must not cause it to be discarded as
   a discussion comment. A linkpost introduction is retained as page text;
   its outbound link is not automatically followed. Comment extraction remains
   disabled.
   Nonempty extraction is a retrieval result, not a guarantee of sufficient evidence. No scripts or browser
   interactions are executed. Public PDFs use Adobe
   PDF-to-Markdown first when configured, with one bounded hard timeout across
   classification and summary retrieval, duration logging, and an explicit,
   logged local `pypdf` fallback.
4. When the policy permits it, retry transient direct failures once: network
   timeouts or interrupted connections, HTTP 408, and HTTP 5xx. Ordinary HTTP
   429 also permits one direct retry. Numeric and HTTP-date `Retry-After` values
   are honored for a direct retry, up to a 15-second wait. A longer wait, or one
   that cannot fit the remaining deadline, skips the direct retry: HTTP 408/5xx
   may still use Reader, while ordinary HTTP 429 stops. Missing or invalid values
   use the normal one-second delay. Only an actual direct retry waits.
   Recognized challenges use recovery immediately, including HTTP 5xx. Ordinary rate limits do
   not switch to Jina or search. A recognized Vercel verification response with
   HTTP 429 and no `Retry-After` follows the challenge recovery path.
   Summary retrieval also permits one narrowly scoped URL correction: when the
   original URL (not a redirected destination) returns HTTP 404 and its path
   ends in a literal backslash, remove exactly that final character and make
   one direct request. Preserve the query and fragment, validate the corrected
   URL again, and use the same response/extraction limits. Percent-encoded
   backslashes are not changed. A failed correction ends retrieval without
   timeout retries, Jina, Wayback, or search recovery. Classification disables
   URL correction. The original story URL remains unchanged; success records
   the actual `retrieved_url`, total attempts, and the private fallback reason
   `url_trailing_backslash`. Failure retains that reason and the final error.
   Logs record the correction; public provenance maps the new reason to the
   existing `unknown` value without expanding the public schema.
5. For eligible failures, try Jina Reader and then a validated Wayback capture.
   Eligibility includes ordinary HTTP 400–599 responses except 400, 401, 407,
   422, 429, and 451; HTTP 404/410 skip Reader and go directly to Wayback.
   Recognized verification responses preserve their challenge diagnostic reason,
   including supported DataDome HTTP 401 responses. Missing challenge-vendor
   markers do not prevent recovery. HTML extraction with no body, network
   timeouts or interrupted connections after retries, and missing TLS issuer
   information also qualify. Other certificate verification failures remain
   terminal. Every eligible Reader failure can proceed to Wayback when enabled,
   except unsafe URLs or an exhausted retrieval budget. Wayback index and replay
   requests prefer identity encoding, but also accept gzip. Both the downloaded
   payload and the decompressed payload must fit the existing response byte
   limit; corrupt gzip and unsupported encodings are rejected. Decoded replay
   content still passes the normal challenge, source-identity, and extraction checks.
   Reader always tries anonymously first. Only Reader HTTP 401/429 triggers one
   authenticated retry when `JINA_API_KEY` is configured. Origin-status errors,
   challenges, malformed responses, timeouts, and other failures do not trigger
   this retry. Each request retains the existing size limit and its timeout is
   clipped to the remaining retrieval deadline;
   attempt counts include both requests, including when Wayback follows.
   Reader validates its successful JSON envelope separately from the reported
   origin status. Origin 2xx and 301/302/303/307/308 proceed to the existing
   public-URL, nonempty-content, challenge-page and size checks: rendered article
   content can accompany a redirect status. Other statuses (including 304),
   missing values and noninteger values are rejected. Acceptance does not prove
   article completeness or change the requested URL/query parameters.
   Per-request logs distinguish anonymous/API-key attempts and record outer HTTP
   status, envelope code/status, origin status and content length, without body
   text or credentials. Status errors include bounded numeric status values;
   malformed fields are represented by markers, never echoed. The key uses an
   unredirected Authorization header and is never logged.
6. Return `ArticleFetchResult` with transport, extractor, attempt count,
   retrieved URL, fallback reason, material origin, and optional bounded
   `source_evidence`. The latter preserves fetched title, author, publication
   date, and explicit
   source relations from article headers, Reader introductions, or YouTube
   descriptions; it is not itself an acceptance decision. It survives direct,
   Jina, and Wayback routing and never substitutes for article/caption text.
   Publication dates are bounded to 64 characters and come only from explicit
   page publication metadata, article JSON-LD, or article/header time elements;
   modification dates and unrelated page dates are excluded. Reader/YouTube
   metadata can supply a publication date explicitly, but prose and search
   results are not used to infer it. Reader prefers `publishedTime`, falling back
   to `metadata["article:published_time"]` when the former is missing, nonstring,
   blank or over the 64-character bound. Modification timestamps are not used.

Complete extracted text has a 2 MiB hard ceiling, independent of downstream
model evidence budgets. HTML and PDF download ceilings remain 4 and 20 MiB.
Exceeding a hard ceiling still fails; this package does not truncate or select
title-dependent excerpts. Classification and summarization select bounded views
after retrieval, preserving full material for recovery validation.

Classification uses a stricter policy than final summary retrieval. Recovery
paths must not run when the active policy disables them.

## Shared Retrieval Budget

Within a scheduled `daily-brief run`, opener and bounded-response-read failures
are also observed by the run's network recovery scope before a fallback can
replace the final error. Transient DNS, route, connection and HTTP failures
are recorded as possible recovery signals, not proof of a shared outage.
An optional-source failure can request a shared pause only when no usable
material remains and a separate HN HTTPS check also fails transiently.
Observation does not sleep or alter retrieval routing:
generation first tries the existing material alternatives, then decides whether
to pause. Each resumed material attempt gets a new retrieval budget after the
long pause, while successful material is retained.

`budget.py` gives each selected item's source-material preparation a shared
120-second cooperative deadline and a maximum of 24 opener calls. Direct
requests, retries, Reader, Wayback discovery/replay, Tavily searches, and fetched
recovery candidates consume the same scope; nested fetches do not reset it.
Standalone `fetch_article(...)` calls start a scope when none exists. Socket
timeouts are clipped to the remaining time. Checks decide whether to start new
requests or recovery stages; completed responses and extracted material still
undergo normal validation and are retained even when they arrive after the deadline.
Exhaustion stops further recovery with `retrieval_budget_exhausted` when no usable
material was obtained; existing HN materials remain available to generation.

This is not a hard wall-clock cutoff: CPU extraction, response reads, injected
blocking functions, or subprocess work can run past the deadline before the next
check. PDF and YouTube subprocesses retain their own time and resource limits.
In particular, Adobe conversion keeps its 300-second limit in both classification
and summary retrieval; a successful conversion taking over 120 seconds is accepted.
Requests are counted at the top-level opener invocation; redirects inside urllib
are bounded separately and do not consume additional shared request slots. The
budget does not replace per-response, per-route, or extraction limits.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade and supported imports |
| `contracts.py` | Shared policies, results, errors, limits, and logger |
| `fetch.py` | URL routing and direct-request retry |
| `budget.py` | Shared cooperative deadline, request counting, bounded waits, and opener wrapping |
| `recovery.py` | Jina and Wayback fallback orchestration |
| `responses.py` | Response decoding and HTML/PDF extraction dispatch |
| `wayback.py` | Capture discovery, identity checks, and replay retrieval |
| `jina.py` | Jina Reader request and envelope validation |
| `github.py` | Repository README and exact blob retrieval |
| `extract.py` | HTML body extraction and semantic-table preservation |
| `http_safety.py` | Public-address validation and pinned connections |
| `challenges.py` | Browser-challenge and network-failure detection |
| `youtube_captions.py` | Bounded YouTube caption retrieval and normalization |
| `source_evidence.py` | Bounded publisher-declared identity signals from page headers and video descriptions |

## Dependency Direction

Keep dependencies directed from orchestration toward lower-level modules:

```text
__init__ -> fetch -> recovery -> jina / wayback
                  -> github / responses / youtube_captions
lower-level modules -> budget / contracts / http_safety / challenges / extract / source_evidence
```

Lower-level modules must not import `fetch.py` or the package facade. Shared
types and limits belong in `contracts.py`; this keeps the package free of
circular imports.

`responses.py` runs PDF extraction in subprocesses from the separate
[`pdf_workers`](../pdf_workers/__init__.py) package. Those workers must not
live in or import this package: `python -m` would load the whole package
before every PDF, adding startup time and memory inside the worker's limits.

## Invariants

- Validate the initial URL, every redirect, and the final response URL.
- Connect only to the exact public addresses returned by the injected resolver.
- Keep downloads, extracted text, PDF work, subprocesses, retries, and recovery
  attempts bounded.
- A specialized transport or fallback must not weaken URL, response, or content
  validation.
- Preserve stable error codes and complete retrieval provenance.
- Treat all retrieved content as untrusted input.

## Tests

Subsystem tests live in `tests/test_article_fetcher.py`, with YouTube captions
and source evidence covered by `tests/test_youtube_captions.py`,
`tests/test_source_evidence.py`, and `tests/test_source_evidence_transport.py`.
`tests/test_retrieval_budget.py` covers shared counts, deadline checks, nested
scopes, bounded waits, and Tavily integration without real sleeping.
Tests must use injected openers and resolvers and must never call live article,
Jina, Wayback, GitHub, or model-provider services.

During development, run the relevant article-fetcher tests. Before completing a
behavior or code change, run the full suite as required by the repository guide:

```sh
pytest -q tests/test_article_fetcher.py
pytest -q
```
