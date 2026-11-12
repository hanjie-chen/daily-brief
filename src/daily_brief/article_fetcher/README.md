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
2. Route supported YouTube, GitHub and CVE URLs to their specialized transports;
   CVE record pages on exact `cve.org` or `www.cve.org` hosts use the public
   `https://cveawg.mitre.org/api/cve/{ID}` endpoint without credentials in both
   classification and summary retrieval. Recognize `/CVERecord` with an optional
   trailing slash and one valid `id` query value; normalize the CVE ID to uppercase.
   Other pages retain ordinary HTML retrieval. Require a matching `CVE_RECORD`
   with `PUBLISHED` state and a nonempty CNA description. Title-only records
   cannot succeed. Keep the title as publisher metadata and serialize record
   metadata and CNA/ADP containers as JSON body text, preserving version bounds,
   descriptions, metric vectors, weaknesses, dates, references and remedies.
   No extra model call converts these fields. Publication time comes from
   `cveMetadata.datePublished`, never the update time. Preserve the original
   page as `retrieved_url`; private method/extractor are `cve_api`, with the
   public method projected to `direct`.
   CVE requests share the direct retry loop, retrieval budget, public-address
   checks, and download/extraction limits. Restrict every redirect and final URL
   to the exact requested HTTPS API endpoint. Invalid identities, nonpublished
   records, malformed responses and unsafe URLs fail without accepting content.
   Empty descriptions and eligible transport failures recover against the
   original webpage in both classification and summary retrieval.
   Ordinary API 429 retains the bounded wait/retry policy without switching
   channels. No GitHub credentials are sent to CVE Services.

   GitHub PDF blobs retain the same PDF extraction policy. Issue-page URLs use
   the public REST issue API in both classification and summary retrieval.
   Follow official issue migrations, restricting every redirect hop to HTTPS,
   exact `api.github.com`, and a named `/repos/{owner}/{repo}/issues/{number}`
   or numeric `/repositories/{id}/issues/{number}` endpoint. Public-address
   validation still applies at every hop. Validate the final endpoint against
   the response's canonical `url`, `html_url`, and `number`; allow repository
   and issue-number changes established by that trusted chain. For numeric
   repository endpoints, the consistent canonical API/page URLs in GitHub's
   response establish the repository name. Without a redirect, the requested
   identity must still match. Reject off-site intermediaries, HTTP downgrades,
   conflicting identities, PRs, and malformed responses. Preserve the original
   story URL and record the canonical page as `retrieved_url`; a successful
   migration is still direct API acquisition, not fallback.
   Keep the API title as metadata and Markdown `body` as webpage text,
   preserving code blocks. In both classification and summary retrieval, append
   a bounded sample of recent issue comments, so classification material can be
   reused without losing updates. Fetch at most two pages of 30 comments from
   the end using the issue's reported count; retain the latest 30 by ID. A URL's
   `#issuecomment-ID` target is fetched first and retained in addition when it is
   outside the recent sample. Deduplicate by ID, reserve text for the target
   first, then newer comments, and present included comments in ascending ID
   order. The sample is bounded to 24,000 characters including labels, with at
   most 6,000 body characters per comment; the remaining extraction byte budget
   can reduce this further. Truncated comments and incomplete sampling are labeled.
   The sample reports both the issue total and the actual included count after
   character limits, counting a linked target only once.
   Initial body and comments have separate creation/edit timestamps; comments
   include author, URL, and repository association, which does not prove an
   affiliation with the organization being discussed. Both remain `web_body`,
   distinct from HN comments, without another model call or public schema field.
   `created_at` still supplies the page publication date, never `updated_at`.
   Query strings and other fragments do not change which issue is fetched.
   Other GitHub URL types retain their existing routing.
   Comment requests use the validated canonical issue's repository after any
   migration, never arbitrary payload links. Validate each comment's ID, API URL,
   issue URL and page anchor. Comment endpoints reject redirects to a different
   URL before credentials can leave; every request shares public-address,
   response-size and retrieval-budget checks. Comment calls are best effort,
   without retries: a linked-target failure (including timeout, server error,
   missing comment or invalid response) still permits recent sampling. Only
   retrieval-budget exhaustion or API throttling (429, or 403 with zero remaining
   quota or Retry-After) stops both. A recent-page failure stops further paging.
   Issue and linked-comment requests share the throttling predicate in `github.py`.
   All failures retain the initial body and already validated comments. Logs
   record failures and reported/acquired/included counts.
   Comments can supply material for an empty initial body; only when both are
   unusable does the existing empty-content recovery apply.
   The initial issue API request shares the direct retry loop and retrieval budget.
   Empty combined material and eligible transport failures enter the existing Jina/Wayback flow
   against the original issue page, without retrying generic HTML extraction;
   404/410 skip Reader for archives in both stages.
   API quota exhaustion (429, or 403 with `X-RateLimit-Remaining: 0` or
   `Retry-After`) stops API retries and enters webpage recovery immediately
   during classification and summary retrieval. The private cause is
   `github_api_rate_limited`; Reader, Wayback, and eligible search recovery use the original issue page.
   API 403 without rate-limit evidence remains terminal, as do malformed or
   mismatched responses, unsafe URLs, size violations, and existing terminal
   HTTP cases. README/blob transport failure behavior is unchanged.
   Issue and README API requests read optional `GITHUB_TOKEN` from the process
   environment. A nonblank token adds Bearer authentication to the initial
   `https://api.github.com/repos/...` request; absent or blank values keep
   anonymous access. Credentials use an unredirected header; only issue API
   redirects explicitly reattach it after every destination passes the public
   address and issue-endpoint checks. README redirects, raw file downloads,
   ordinary pages, and recovery channels receive no GitHub token. Authentication
   does not change retry or recovery eligibility. Invalid
   header characters fail locally without including the token in diagnostics.
3. Otherwise perform the direct request, validate redirects and the final URL,
   and extract bounded HTML, text, or PDF content. HTML extraction includes
   separately labeled publisher metadata (title, description, og:description)
   and body text. Metadata is bounded and deduplicated, and only supplements
   nonempty body text. Empty HTML extraction still raises `empty_content` and
   follows the policy-controlled Jina recovery path, even when metadata is present.
   After enforcing the extraction size limit, HTML and Reader results share a
   conservative shell check in `content.py`. A body containing only recognized
   JavaScript-enable notices or common navigation/region/language controls is
   rejected as `empty_content`; publisher metadata cannot make it usable. Markdown
   link labels remain visible to the check, while image/link decoration is ignored.
   Repeated site titles may be ignored alongside JS notices, but sentence-like or
   numbered titles and unknown body text are retained. Unknown controls/headings
   are also retained: this is a bounded set of shell patterns, not semantic
   validation or a minimum body length. Accepted content is returned unchanged.
   JS notices include the passive `JavaScript must be enabled` form, optional
   browser/settings wording, and the warning that content or functionality may
   be limited or unavailable. Matching still requires a complete notice; extra
   substantive prose, quotations, and code remain eligible as body material.
   Wayback uses the same HTML check. A Reader shell proceeds to Wayback, and
   exhausted retrieval remains eligible for search recovery in both classification
   and summarization.
   Security-verification notices are matched as complete lines or sentences,
   including the verification heading and explanatory bot-check notices. A bare
   hostname heading is ignored only alongside a recognized security notice.
   Pages containing only these notices/navigation yield `empty_content`, so
   Reader can continue to Wayback and exhausted source retrieval to search.
   Quoted/code lines and other body text are preserved, including articles
   discussing verification prompts.
   Recognizable LessWrong post markup is normalized before extraction: the
   article's `commentOnSelection` wrapper must not cause it to be discarded as
   a discussion comment. A linkpost introduction is retained as page text;
   its outbound link is not automatically followed. Comment extraction remains
   disabled.
   HTML extraction uses the pinned `trafilatura==2.2.0` dependency.
   Nonempty extraction can be checked against the bundled Readability candidate
   from the same normalized HTML. Only disjoint bodies with strictly better
   page-title coverage in Readability trigger replacement: casefolded word
   four-gram sets must overlap by less than 10% of the smaller set, and the
   alternative must match more unique `<title>` words of at least three
   characters. Publisher metadata is excluded from body comparisons. Missing
   title words, bodies shorter than four words, unavailable alternatives, or
   overlapping/no-better candidates retain the original extraction. Full title
   coverage in the original skips the extra extraction. Containment of a shorter
   excerpt does not count as disjointness.
   A selected Readability HTML candidate passes through the same Trafilatura
   cleanup once, with comments disabled and without recursive comparison; raw
   Readability text is never returned. Empty cleanup yields `empty_content` rather
   than restoring the suspect original or metadata. The same recovery conditions
   apply in classification and summarization. This also applies to
   Wayback HTML, not Reader text. There are no new model calls, network requests,
   or public fields. These heuristics do not establish completeness and can
   reject valid material; existing acquisition and summary-source diagnostics
   remain available for investigation.
   Empty inline `span` and custom-element markers (hyphenated tag names) inside
   paragraphs, headings, list items and table cells are unwrapped before
   extraction, preserving the following prose. This avoids losing explanations
   after citation markers that the extractor skips. Only markers with no child
   elements or non-whitespace text are unwrapped; nonempty components and parent
   container attributes remain available to normal content filtering. Markers
   under explicitly hidden ancestors are left intact to avoid exposing their text.
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
   Logs record the correction; public generation info maps the reason to the
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
   Generic Reader request failures retain bounded exception type names in logs
   and private error messages, including the underlying exception type when a
   `URLError` wraps a timeout or DNS error. Exception messages and string reasons
   are omitted, so request URLs and credentials cannot leak through this detail.
   Authenticated retries and subsequent Wayback failures preserve this detail;
   error codes and retry eligibility are unchanged.
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

Classification and summary retrieval share `DEFAULT_FETCH_POLICY`, HTTP and local
PDF timeouts, recovery eligibility, budgets, and date bounds. Recovery paths must
not run when an explicitly supplied policy disables them.

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

`budget.py` gives each candidate's source-material preparation a shared
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
| `github.py` | Repository README, exact blob, and structured issue-body retrieval |
| `cve.py` | CVE record URL recognition, official API validation and structured record text |
| `extract.py` | HTML body extraction and semantic-table preservation |
| `http_safety.py` | Public-address validation and pinned connections |
| `challenges.py` | Browser-challenge and network-failure detection |
| `content.py` | Shared conservative detection of notice/navigation-only page shells |
| `youtube_captions.py` | Bounded YouTube caption retrieval and normalization |
| `source_evidence.py` | Bounded publisher-declared identity signals from page headers and video descriptions |

## Dependency Direction

Keep dependencies directed from orchestration toward lower-level modules:

```text
__init__ -> fetch -> recovery -> jina / wayback
                  -> cve / github / responses / youtube_captions
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

Subsystem tests live in `tests/test_article_fetcher.py`. `tests/test_github_issue.py`
covers API routing, Markdown evidence, identity checks, recovery, and bounds.
`tests/test_cve.py` covers CVE API material, URL/record identity, redirects,
shared classification/summary recovery, budgets and limits using synthetic records.
`tests/test_inline_marker_extraction.py` covers prose after empty inline markers,
text order, paragraph boundaries and preservation of content-filtering structure.
`tests/test_github_issue_redirects.py` exercises repository renames, issue
transfers, and redirect-chain restrictions through urllib's redirect handling;
`tests/test_github_auth.py` covers optional credentials and their isolation.
YouTube captions and source evidence are covered by `tests/test_youtube_captions.py`,
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
