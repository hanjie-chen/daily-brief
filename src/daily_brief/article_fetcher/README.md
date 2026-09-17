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
   nonempty body text. Empty bodies still raise `empty_content` and follow the
   existing policy-controlled Jina recovery path, even when metadata is present.
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
4. Retry a direct network timeout only when the policy permits it.
5. For eligible failures, try Jina Reader and then, for eligible browser
   challenges only, a validated Wayback capture. Wayback index and replay
   requests prefer identity encoding, but also accept gzip. Both the downloaded
   payload and the decompressed payload must fit the existing response byte
   limit; corrupt gzip and unsupported encodings are rejected. Decoded replay
   content still passes the normal challenge, source-identity, and extraction checks.
6. Return `ArticleFetchResult` with transport, extractor, attempt count,
   retrieved URL, fallback reason, material origin, and optional bounded
   `source_evidence`. The latter preserves fetched title, author, and explicit
   source relations from article headers, Reader introductions, or YouTube
   descriptions; it is not itself an acceptance decision. It survives direct,
   Jina, and Wayback routing and never substitutes for article/caption text.

Classification uses a stricter policy than final summary retrieval. Recovery
paths must not run when the active policy disables them.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade and supported imports |
| `contracts.py` | Shared policies, results, errors, limits, and logger |
| `fetch.py` | URL routing and direct-request retry |
| `recovery.py` | Jina and Wayback fallback orchestration |
| `responses.py` | Response decoding and HTML/PDF extraction dispatch |
| `wayback.py` | Capture discovery, identity checks, and replay retrieval |
| `jina.py` | Jina Reader request and envelope validation |
| `github.py` | Repository README and exact blob retrieval |
| `extract.py` | HTML body extraction and semantic-table preservation |
| `http_safety.py` | Public-address validation and pinned connections |
| `challenges.py` | Browser-challenge and network-failure detection |

## Dependency Direction

Keep dependencies directed from orchestration toward lower-level modules:

```text
__init__ -> fetch -> recovery -> jina / wayback
                  -> github / responses
lower-level modules -> contracts / http_safety / challenges / extract
```

Lower-level modules must not import `fetch.py` or the package facade. Shared
types and limits belong in `contracts.py`; this keeps the package free of
circular imports.

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

Subsystem tests live in `tests/test_article_fetcher.py`. Tests must use injected
openers and resolvers and must never call live article, Jina, Wayback, GitHub, or
model-provider services.

During development, run the relevant article-fetcher tests. Before completing a
behavior or code change, run the full suite as required by the repository guide:

```sh
pytest -q tests/test_article_fetcher.py
pytest -q
```
