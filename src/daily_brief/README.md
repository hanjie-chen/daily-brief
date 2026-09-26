# Daily Brief Python Package

This directory contains the Daily Brief application logic. This guide describes
the package's entry points, data flow, module boundaries, and cross-cutting
invariants. User-visible behavior belongs in the [root README](../../README.md),
product intent belongs in [the product document](../../docs/product.md), and
function-level behavior belongs in the code and tests.
For a system overview, retrieval/recovery flows, and diagnostic entry
points, see [the architecture guide](../../docs/architecture.md).

## Entry Points

- `__main__.py` runs the primary CLI through `python -m daily_brief`.
- `cli.py` parses the `daily-brief` commands for generation, publishing, and
  model evaluation and dispatches them. It contains no pipeline logic.
- `generation/` implements the generation pipeline. Start at its
  [guide](generation/README.md) for changes to stage order, selection, material
  retrieval, recovery, or summary fallback.
- `candidates/keyword_evaluation.py` provides the corpus collection and replay
  utility used to evaluate production keyword matching. It can be run through
  `scripts/evaluate_keywords.py`.

## Generation Flow

`cli.main()` constructs the production model backend before calling
`generation.run_generate(...)`, so configuration errors fail before external
collection. The [generation guide](generation/README.md) describes each pipeline
stage: candidate collection, classification and selection, material retrieval and
recovery, summaries, and artifact writes.

Publishing is a separate, explicitly targeted operation. `output/publisher.py`
validates the public payload, sends it to the website, and records successful
content hashes for idempotent retries.

## Module Map

Each package with its own guide lists its files there.

| Path | Responsibility |
| --- | --- |
| `__init__.py` | Package metadata |
| `__main__.py` | `python -m daily_brief` entry point |
| `cli.py` | Command-line parsing and command dispatch |
| `config.py` | Timezone, topic vocabulary, quotas, thresholds, and scoring limits |
| `models.py` | Shared story, candidate, retrieval, and model-diagnostic structures |
| `time_window.py` | Daily collection window |
| [`generation/`](generation/README.md) | Generation pipeline behind `daily-brief generate`: stage order, routing, material, and summaries |
| [`candidates/`](candidates/README.md) | Rule-based collection, keyword matching, scoring, history, and section selection |
| [`llm/`](llm/README.md) | Prompts, evidence excerpts, summary sufficiency, the Gemini adapter, and model evaluation |
| [`article_fetcher/`](article_fetcher/README.md) | Bounded public article retrieval, extraction, and Jina/Wayback fallbacks |
| [`recovery/`](recovery/README.md) | Search-based recovery after the original source is blocked |
| `output/__init__.py` | Stable facade for rendering, public payload validation, and publishing |
| `output/render.py` | Markdown, public JSON, and private candidate-audit serialization |
| `output/public_schema.py` | Public payload contract shared by generation and publishing |
| `output/publisher.py` | Website delivery, retry, and local success state |
| `pdf_workers/__init__.py` | Import-free package for PDF workers run as `python -m` subprocesses |
| `pdf_workers/adobe_pdf_extractor.py` | Resource-bounded Adobe PDF-to-Markdown worker |
| `pdf_workers/pdf_extractor.py` | Resource-bounded local PDF text worker |

## Package Dependencies

```text
cli             -> generation, llm, output
generation      -> candidates, llm, recovery, article_fetcher, output
recovery        -> article_fetcher
llm             -> article_fetcher (model evaluation's extracted-text limit only)
article_fetcher -> pdf_workers
every package   -> config / models / time_window as needed
```

Dependencies point one way. Only `cli.py` imports `generation/`, nothing imports
`cli.py`, and `candidates/`, `output/`, and `pdf_workers/` import no other
package. Keep new code on the same side of these arrows; a package that needs
something from a package above it usually means the code belongs in
`generation/`.

## Core Invariants

Summary model fallback and quota handling are described in the
[llm guide](llm/README.md#summary-model-fallback-and-quotas).

- Hacker News fields, article content, captions, URLs, and source metadata are
  untrusted input. They remain behind an explicit prompt boundary and cannot
  supply instructions to the model.
- Article retrieval accepts only validated public HTTP(S) destinations and
  preserves address validation across redirects and final responses. All network,
  subprocess, document, and extracted-text work remains bounded. Specialized
  transports and recovery paths must not weaken these controls. Wayback gzip
  responses are bounded both before and after decompression. LessWrong post
  markup is narrowly normalized so comment filtering retains the article body
  without enabling extraction of discussion comments.
- Classification uses a stricter retrieval policy than selected-item
  summarization. A recovery path is eligible only for its documented failure
  conditions, cannot recurse, and must validate both source identity and usable
  material before model input is created.
  Same-article recovery preserves the original source URL and failure, actual
  recovered URL, `same_article` provenance, query, and per-candidate decisions
  in private audit. Candidate fetches never recursively trigger search. Missing
  or ambiguous evidence fails closed; this first version intentionally rejects
  short copies and unsupported attribution formats. Identity evidence remains
  untrusted publisher testimony, not independent authorship authentication.
- Summaries are grounded in retrieved article text, Hacker News self-post text, or
  an explicitly labeled bounded discussion sample. Failed external retrieval never
  produces a title-only article paraphrase. Product-level summary requirements are
  defined in [the product document](../../docs/product.md).
- Candidate collection is run-scoped: either required source failing aborts the
  run. After collection succeeds, classifier, article-retrieval, and summarizer
  failures are item-scoped and retain distinct reader-facing and audit states.
- Public JSON retains the compatibility section keys `ai` and `non_ai_hot`.
  Local Markdown labels the core section `Tech picks`, while the website displays
  it as 技术精选. Changing either display label is not a schema migration.
- Public JSON and private audit data have different trust and compatibility
  boundaries. Public output uses the strict schema in `output/public_schema.py` and never
  exposes raw provider diagnostics, recovery URLs, or private evaluation material.
- Public JSON replacement and no-content marker writes are atomic. A no-content
  marker cannot hide an existing invalid public payload, and publishing never
  scans or catches up old dates implicitly.
- Jina Reader is anonymous first, with one optional `JINA_API_KEY` retry for
  Reader HTTP 401/429 only. Origin/content failures remain failures; recovery
  attempt totals include the authenticated request. Credentials are not forwarded
  on redirects.
- Credentials come only from process environment variables and must not enter
  artifacts, logs, fixtures, model-evaluation data, or Git. Configuration is
  documented in [`.env.example`](../../.env.example).
- Production and evaluation share provider-neutral model contracts and output
  normalization. Provider-specific behavior stays in its adapter, and production
  model identifiers remain explicit rather than moving aliases.
- Tests are deterministic and do not call live Hacker News, retrieval, search, or
  model-provider services. Real article bodies and captured model inputs remain
  under Git-ignored `data/` paths.

## Common Change Paths

- Core-topic recognition: `config.py` -> `candidates/keywords.py` -> `llm/topic_classifier.py`
  -> `generation/classification.py`.
- Ranking or quotas: `config.py` -> `candidates/scoring.py` -> `candidates/selection.py`
  -> `generation/classification.py`.
- Article material: the relevant transport or extractor -> `article_fetcher/`
  -> `generation/material.py` or `recovery/`
  -> `llm/summarizer.py` -> `generation/summaries.py`.
- Summary quality: `llm/summarizer.py` -> the model adapter -> relevant orchestration
  and rendering tests.
- Generated or published data: `output/render.py` -> `output/public_schema.py`
  -> `output/publisher.py`
  -> `generation/pipeline.py` or `cli.py`.

Keep external calls injectable, update tests at the boundary whose behavior
changes, and update the root README or product document when a change affects
their documented responsibilities. Follow the repository [contributor
instructions](../../AGENTS.md) for verification requirements.
