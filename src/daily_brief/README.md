# Daily Brief Python Package

This directory contains the Daily Brief application logic. This guide describes
the package's entry points, data flow, module boundaries, and cross-cutting
invariants. User-visible behavior belongs in the [root README](../../README.md),
product intent belongs in [the product document](../../docs/product.md), and
function-level behavior belongs in the code and tests.

## Entry Points

- `__main__.py` runs the primary CLI through `python -m daily_brief`.
- `cli.py` provides the `daily-brief` commands for generation, publishing, and
  model evaluation. Start here for changes to pipeline order or cross-module
  fallback behavior.
- `keyword_evaluation.py` provides the corpus collection and replay utility used
  to evaluate production keyword matching. It can be run through
  `scripts/evaluate_keywords.py`.

## Generation Flow

Production generation spans CLI setup and `cli.run_generate(...)`:

1. `time_window.py` calculates the daily collection window in the
   Asia/Singapore timezone.
2. `cli.main()` constructs the production model backend before calling
   `run_generate(...)`, so configuration errors fail before external collection.
3. `hn_client.py` collects recent Algolia stories and hot stories from the
   official Hacker News API. Both sources are required; failure of either source
   aborts the run before date-scoped artifacts are written or replaced.
4. `selection.py` deduplicates candidates, while `history.py` excludes recently
   recommended stories. `keywords.py` and `scoring.py` establish the initial core
   candidates and ranking order.
5. A bounded set of remaining candidates is fetched under the classification
   retrieval policy and classified by article evidence as AI, other core
   computing, outside the core scope, or uncertain. Retrieval and classifier
   failures fail closed for that candidate.
6. Confirmed core candidates join one ranked pool. Confirmed outside candidates
   must also satisfy the exploration eligibility rules and are ranked separately.
7. Selected external stories are retrieved under the fuller summary policy.
   Material fetched during classification is reused. Specialized GitHub, YouTube,
   HTML, and PDF paths remain behind the same bounded retrieval interface;
   recovery material is accepted only after deterministic validation.
8. If every external-source retrieval and recovery path fails for a selected
   story, `cli.py` asks `hn_client.py` for a bounded HN discussion sample as the
   final fallback.
   `summarizer.py` selects the generic, memorial, research, or HN-discussion route
   from available evidence. An external-source retrieval failure never becomes a
   title- or model-knowledge-based article summary.
9. `render.py` writes the readable Markdown, validated public JSON, and private
   candidate audit. `history.py` then records selected item IDs. An empty brief
   writes a `.no-content` marker instead of public JSON.
10. Publishing is a separate, explicitly targeted operation. `publisher.py`
    validates the public payload, sends it to the website, and records successful
    content hashes for idempotent retries.

Model comparison is intentionally separate from generation. A generation run can
capture the exact classifier and summarizer inputs, and `evaluate-model` can
replay that immutable input without fetching sources, rendering a brief, or
modifying recommendation and publishing state.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Package metadata |
| `__main__.py` | `python -m daily_brief` entry point |
| `cli.py` | Primary CLI and end-to-end orchestration |
| `config.py` | Timezone, topic vocabulary, quotas, thresholds, and scoring limits |
| `models.py` | Shared story, candidate, retrieval, and model-diagnostic structures |
| `time_window.py` | Daily collection window |
| `hn_client.py` | Algolia collection, hot stories, and bounded HN discussion sampling |
| `keywords.py` | Keyword and URL-token matching |
| `keyword_evaluation.py` | Keyword corpus collection and deterministic replay |
| `scoring.py` | Candidate scoring and recommendation explanations |
| `selection.py` | Deduplication and final section selection |
| `history.py` | Recent recommendation history |
| `model_backend.py` | Provider-neutral classification and summarization contracts |
| `gemini_backend.py` | Gemini adapter, pacing, structured output, and bounded retry |
| `model_evaluation.py` | Versioned model-input capture and side-effect-free replay |
| `topic_classifier.py` | Article-evidence topic classification |
| `article_fetcher.py` | Bounded public article retrieval, extraction, and provenance |
| `syndicated_copy.py` | Discovery and validation of Reuters syndicated copies |
| `alternate_reporting.py` | Discovery and validation of Reuters reporting on the same event |
| `youtube_captions.py` | Bounded YouTube caption retrieval and normalization |
| `adobe_pdf_extractor.py` | Resource-bounded Adobe PDF-to-Markdown worker |
| `pdf_extractor.py` | Resource-bounded local PDF text worker |
| `summarizer.py` | Grounded prompts, route selection, evidence selection, and normalization |
| `render.py` | Markdown, public JSON, and private candidate-audit serialization |
| `public_schema.py` | Public payload contract shared by generation and publishing |
| `publisher.py` | Website delivery, retry, and local success state |

## Core Invariants

- Hacker News fields, article content, captions, URLs, and source metadata are
  untrusted input. They remain behind an explicit prompt boundary and cannot
  supply instructions to the model.
- Article retrieval accepts only validated public HTTP(S) destinations and
  preserves address validation across redirects and final responses. All network,
  subprocess, document, and extracted-text work remains bounded. Specialized
  transports and recovery paths must not weaken these controls.
- Classification uses a stricter retrieval policy than selected-item
  summarization. A recovery path is eligible only for its documented failure
  conditions, cannot recurse, and must validate both source identity and usable
  material before model input is created.
- Summaries are grounded in retrieved article text, Hacker News self-post text, or
  an explicitly labeled bounded discussion sample. Failed external retrieval never
  produces a title-only article paraphrase. Product-level summary requirements are
  defined in [the product document](../../docs/product.md).
- Candidate collection is run-scoped: either required source failing aborts the
  run. After collection succeeds, classifier, article-retrieval, and summarizer
  failures are item-scoped and retain distinct reader-facing and audit states.
- Public JSON retains the compatibility section keys `ai` and `non_ai_hot` even
  though the reader-facing core section is 技术精选 (`Tech picks`). Changing a
  display label is not a schema migration.
- Public JSON and private audit data have different trust and compatibility
  boundaries. Public output uses the strict schema in `public_schema.py` and never
  exposes raw provider diagnostics, recovery URLs, or private evaluation material.
- Public JSON replacement and no-content marker writes are atomic. A no-content
  marker cannot hide an existing invalid public payload, and publishing never
  scans or catches up old dates implicitly.
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

- Core-topic recognition: `config.py` -> `keywords.py` -> `topic_classifier.py`
  -> `cli.py`.
- Ranking or quotas: `config.py` -> `scoring.py` -> `selection.py` -> `cli.py`.
- Article material: the relevant transport or extractor -> `article_fetcher.py`
  -> `summarizer.py` -> `cli.py`.
- Summary quality: `summarizer.py` -> the model adapter -> relevant orchestration
  and rendering tests.
- Generated or published data: `render.py` -> `public_schema.py` -> `publisher.py`
  -> `cli.py`.

Keep external calls injectable, update tests at the boundary whose behavior
changes, and update the root README or product document when a change affects
their documented responsibilities. Follow the repository [contributor
instructions](../../AGENTS.md) for verification requirements.
