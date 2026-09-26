# Recovery

This package finds replacement material for a selected story whose original page
is blocked by a browser challenge. It searches for other pages, fetches them,
and accepts one only after deterministic validation. The package-level facade is
the import used by the rest of the application:

```python
from daily_brief.recovery import attempt_same_article_recovery
```

Deciding when recovery runs, and what happens after it fails, stays in
`generation/material.py`. Fetching each page, including its safety checks and
Jina/Wayback fallbacks, stays in [`article_fetcher/`](../article_fetcher/README.md).
The reader-level flow and the audit fields to check when debugging are described
in [the architecture guide](../../../docs/architecture.md) (Chinese).

## When Recovery Runs

Recovery runs only while preparing a selected story's summary, never during
classification, and only after `article_fetcher` has exhausted its own
fallbacks. `generation/material.py` tries at most one route of each kind, in
this order, and stops at the first accepted page:

1. Same article: the original failure's fallback reason is an origin block
   (`models.is_origin_block_reason(...)`).
2. Reuters syndicated copy: the original URL is on Reuters, the failure came
   from Jina or Wayback, and the fallback reason is `datadome_challenge`.
3. Alternate reporting: the original URL is not on Reuters and the fallback
   reason is an origin block.

Routes 2 and 3 are mutually exclusive. When every route fails, the story keeps
its original retrieval failure and `generation/` moves on to the HN discussion.

## Routes

Each route has a finder that turns a story into search results, a validator that
decides whether a fetched page is acceptable, and an attempt function in
`search_recovery.py` that connects them. Search results only nominate URLs:
their titles and snippets are never evidence.

| Route | Search | Pages considered | Accepts |
| --- | --- | --- | --- |
| Same article | HN title, any domain except HN | `MAX_SAME_ARTICLE_CANDIDATES` results, at most `MAX_SAME_ARTICLE_FETCHES` fetched | The first page that passes validation |
| Syndicated copy | Terms from the Reuters URL slug and title, exact match, `SYNDICATED_HOST_ALLOWLIST` only | `MAX_SYNDICATED_CANDIDATES` | The first page that passes validation |
| Alternate reporting | Distinctive slug or title terms plus "Reuters", `ALTERNATE_REPORTING_HOST_ALLOWLIST` only | `MAX_ALTERNATE_REPORTING_CANDIDATES` | The longest verified body, if the verified pages agree |

### Same Article

`same_article.validate_same_article(...)` looks only at the fetched page:

- Its own title, read from the page's `source_evidence`, must match the HN title
  word for word, optionally after removing a trailing site name.
- It must declare an explicit `crosspost` or `republication` relation to the
  original URL (a YouTube video must declare `narration` instead). A canonical
  link or a bare link is not enough. URLs are compared after normalization by
  `same_source_url(...)`, which also knows LessWrong post IDs.
- Its body must reach `MIN_SAME_ARTICLE_BODY_CHARS`, contain no teaser or
  summary/aggregator phrases, and have several substantive paragraphs
  (transcripts: several substantive sentences).

The route is intentionally conservative: it rejects short copies and
attribution formats other than these explicit relations.

Before validation, `search_recovery.py` also skips HN pages, the original URL,
duplicates, pages that redirect back to the original, and fetch results that
cannot carry source evidence. Every result gets its own entry in the audit.

### Reuters Syndicated Copy

`syndicated_copy.validate_syndicated_copy(...)` requires a body of at least
`MIN_SYNDICATED_BODY_CHARS` without teaser phrases, a Reuters marker near the
top, a date near the one in the Reuters URL, most of the distinctive words from
the URL slug, every amount (million, billion, trillion) from the slug, and up
to two capitalized names shared by the title and slug.

### Alternate Reporting

`alternate_reporting.validate_alternate_reporting(...)` requires a body of at
least `MIN_ALTERNATE_REPORTING_BODY_CHARS` without teaser phrases, a Reuters
marker near the top, a Reuters "Reporting by" footer near the end, a reporting
date near the source date (from the source URL, or the HN submission time), most
of the distinctive words from the title and slug, and every amount (million,
billion, trillion) or percentage from the title and slug.

Yahoo Finance results are validated first; Reuters results are tried only when
no Yahoo result passes. If two verified pages disagree on the date or share too
few event words (`validations_conflict(...)`), the route fails with status
`conflict` rather than choosing one. Summaries built on this material receive
`ALTERNATE_REPORTING_SUMMARY_PREFIX` in `generation/summaries.py`, because the
page reports the same event rather than the same article.

## Audit Records

Each route writes its own record into the private candidate audit under
`article_retrieval`: `same_article_recovery`, `syndicated_recovery`, and
`alternate_reporting_recovery`. On success, `material_origin` names the route,
`retrieved_url` names the accepted page, and `origin_failure` keeps the original
failure.

| `status` | Meaning |
| --- | --- |
| `not_attempted` | The route's trigger did not apply |
| `not_configured` | Same article only: `TAVILY_API_KEY` is missing; no request was made |
| `finder_failed` | Search failed or returned malformed results; `error_code` says why (the other routes also use it for a missing key, with `error_code` `not_configured`) |
| `not_found` | Search returned no results (syndicated copy and alternate reporting) |
| `exhausted` | Results were found but none was accepted |
| `conflict` | Alternate reporting only: verified pages disagreed |
| `success` | A page was accepted |

The same-article record keeps the search `query` and lists every result in
`candidates` with its own
`status` (`accepted`, `rejected`, or `fetch_failed`), `reason`, and evidence.
The other two routes list rejection reasons in `rejection_reasons`.

## Invariants

- Identity evidence remains untrusted publisher testimony, not independent
  authorship authentication.
- Fail closed. Missing configuration, provider errors, malformed results,
  unclear identity, and conflicts all leave the story without recovered
  material; none of them fails the run.
- No recursion. Candidate pages are fetched through the same summary-policy
  article client as the original, and `article_fetcher` never imports this
  package, so fetching a candidate cannot start another search.
- Everything is bounded: search results, fetches, response sizes
  (`TAVILY_MAX_RESPONSE_BYTES`), and request time (`TAVILY_TIMEOUT_SECONDS`).
- Candidate URLs are normalized and, for the syndicated-copy and
  alternate-reporting routes, restricted to an allowlist before fetching and
  again after redirects. `article_fetcher` still enforces public-address checks
  on every request.
- The API key is sent only in the Tavily `Authorization` header. Audit records
  and logs carry error codes, never exception text.
- Finders are injectable through `generation.run_generate(...)`; without an
  injected finder, each route builds its Tavily finder from the environment.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade and supported imports |
| `search_recovery.py` | Attempt functions: search, filter, fetch, validate, and audit for each route |
| `same_article.py` | Same-article finder, URL normalization and comparison, and validation |
| `syndicated_copy.py` | Reuters syndicated-copy finder, allowlist, and validation |
| `alternate_reporting.py` | Alternate-reporting finder, allowlist, validation, and conflict check |
| `fetched_material.py` | Normalized fetched material shared with `generation/material.py` |

## Dependency Direction

```text
__init__        -> search_recovery, same_article, syndicated_copy, alternate_reporting, fetched_material
search_recovery -> same_article, syndicated_copy, alternate_reporting, fetched_material
all modules     -> models; search_recovery, same_article, fetched_material also
                   -> article_fetcher (result and error types only)
```

This package never imports `generation/`. The finder and validator modules do
not import each other.

## Tests

- `tests/test_same_article.py`, `tests/test_syndicated_copy.py`, and
  `tests/test_alternate_reporting.py`: each finder's request and parsing, and
  each validator.
- `tests/test_same_article_pipeline.py`: same-article attempts, budgets, and the
  hand-off to the other routes and to discussion fallback.
- `tests/test_cli.py`: end-to-end Reuters and alternate-reporting recovery
  through `run_generate(...)`.

Tests inject finders and fake HTTP responses and never call Tavily.
