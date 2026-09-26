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
- `candidates/keyword_evaluation.py` provides the corpus collection and replay utility used
  to evaluate production keyword matching. It can be run through
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

## Model Evaluation

Model comparison is intentionally separate from generation. A generation run can
capture the exact classifier and summarizer inputs, and `evaluate-model` can
replay that immutable input without fetching sources, rendering a brief, or
modifying recommendation and publishing state. Capture schema 5 preserves immutable
classification inputs before and after roundup comment retrieval, content kind,
and preselection summary inputs (including rejected roundups). It also preserves
source and HN-discussion inputs when both are attempted for an ordinary item;
schema 3 and 4 captures remain readable. Replay records insufficient material separately from
provider failures and does not retrieve fallback material.

## Summary Sufficiency

The existing summary call returns a validated sufficient/insufficient decision.
Roundups use a separate sufficiency rule requiring two substantive examples and
never count a restatement of the question as a useful overview. Their dedicated
prompt returns a short introduction and two or three structured entries (name and plain-language description), with comments as substantive
evidence and the self-post question only as context. The adapter validates this
route-specific response and formats a plain-text introduction and bullet list;
`output/render.py` preserves those line breaks in Markdown and the existing public
`summary` string. Ordinary summaries retain their existing response schemas and
whitespace normalization. `content_kind` and rejection
details are private audit fields; public schema remains unchanged.
The backend returns summary text or raises `InsufficientSummaryMaterial`; this is
a completed semantic decision, not a provider error. No character minimum is used
for source sufficiency. `source_material` in candidate audit preserves the original
assessment and its call diagnostics if discussion fallback replaces the summary.
Retrieval status remains independent. Public schema stays compatible: an available
discussion overview after source insufficiency has `ok` and a fixed attribution;
when no summary is available, `summary_failed` carries the material-insufficient
reader message. Each item can proceed from source to discussion only once.
When discussion fallback has existing source text, context strategy
`source_and_hn_comments` preserves both separately labeled inputs. The backend
requires separate `source_summary` and comment `summary` fields and applies source
labels in code; either can be empty, but a sufficient result needs at least one.
Pure discussion fallback keeps its existing schema and attribution. A page
introduction explaining a work's type and theme can suffice without its full
interaction or implementation details. Both source-only and combined prompts use
the same shared sufficiency rule: insufficient means no useful grounded
introduction can be written. Metadata-derived statements always require explicit
website self-description attribution. Combined output distinguishes web content
from HN self-post text; source prefixes are defined in one shared location.

## Bounded Evidence Selection

Retrieval retains complete extracted text up to a 2 MiB hard ceiling; HTML/PDF
response limits remain 4/20 MiB. Oversized responses or extraction still fail,
never silently truncate. Source identity and republication validators continue to
inspect full retrieved material before any model-input selection.

`llm/evidence_selection.py` supplies deterministic title/URL-fragment-aware excerpts
at the model boundary: 6,000 characters for classification and 24,000 for ordinary
source summaries. Full `Story.fetched_text` is never replaced by excerpts, including
when classification material is reused for a selected item. Capture/replay accepts
full fetched text up to the same UTF-8 byte ceiling. Short classifier inputs keep
legacy whitespace normalization; long inputs retain block boundaries.

Long text is divided into bounded blocks. Rare matching title/anchor terms rank
blocks; adjacent blocks retain local qualifications and headings when extraction
preserved them. Publisher metadata keeps its attribution wrapper. No lexical match
uses bounded distributed sampling, explicitly distinguished from relevance matching.
All excerpts carry omission notices; matching never establishes factual truth.
Summary audit records source length, selected length (including markers), strategy,
and half-open character ranges. Ordinary ranges refer to the stripped extracted
source; `research_chars` ranges refer to the assembled research sections, whose
names remain in the audit. Research section selection precedes this budget; each chosen section receives a
separate share with head/tail coverage so a title match cannot displace results or
limitations. The
source-plus-comment fallback budgets the source again without restoring full text.
Comments keep their separate existing bounded sampling limits.

Shared sufficiency instructions apply to short and long sources: only facts relevant
to the current item count, the HN title is not evidence, and a concrete short
announcement can suffice. Insufficient excerpts retain existing discussion fallback
and source/comment attribution; absence from excerpts is not absence from the page.
Selection is lexical, not semantic, and makes no additional provider calls.
All summary routes share an event-scope instruction: summarize the item rather
than the entire source page, retain relevant conditions and limitations, and omit
other updates that only share a product or keywords. Source evidence overrides
unsupported title claims. This is a generation instruction, not a post-generation
quality gate; evidence selection and provider-call counts are unchanged.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Package metadata |
| `__main__.py` | `python -m daily_brief` entry point |
| `cli.py` | Command-line parsing and command dispatch |
| [`generation/`](generation/README.md) | Generation pipeline behind `daily-brief generate` |
| `config.py` | Timezone, topic vocabulary, quotas, thresholds, and scoring limits |
| `models.py` | Shared story, candidate, retrieval, and model-diagnostic structures |
| `time_window.py` | Daily collection window |
| `candidates/__init__.py` | Stable facade for rule-based candidate collection, scoring, history, and selection |
| `candidates/hn_client.py` | Algolia collection, hot stories, and bounded HN discussion sampling (the latter used by summary fallback) |
| `candidates/keywords.py` | Keyword and URL-token matching |
| `candidates/keyword_evaluation.py` | Keyword corpus collection and deterministic replay |
| `candidates/scoring.py` | Candidate scoring and recommendation explanations |
| `candidates/selection.py` | Deduplication and final section selection |
| `candidates/history.py` | Recent recommendation history |
| `llm/__init__.py` | Stable facade for model backends, summary helpers, and model evaluation |
| `llm/model_backend.py` | Provider-neutral classification and summarization contracts |
| `llm/gemini_backend.py` | Gemini adapter, pacing, structured output, and bounded retry |
| `llm/topic_classifier.py` | Article-evidence topic classification |
| `llm/summarizer.py` | Grounded prompts, route selection, evidence selection, and normalization |
| `llm/evidence_selection.py` | Bounded title-aware source excerpts with explicit omissions |
| `llm/model_evaluation.py` | Versioned model-input capture and side-effect-free replay |
| [`article_fetcher/`](article_fetcher/README.md) | Stable facade for bounded public article retrieval |
| `article_fetcher/contracts.py` | Shared retrieval policies, results, errors, and limits |
| `article_fetcher/fetch.py` | Transport routing and direct-request retry |
| `article_fetcher/recovery.py` | Jina and Wayback fallback orchestration |
| `article_fetcher/responses.py` | Response decoding plus HTML/PDF extraction dispatch |
| `article_fetcher/wayback.py` | Internet Archive capture lookup and replay validation |
| `article_fetcher/jina.py` | Jina Reader transport and response validation |
| `article_fetcher/github.py` | GitHub README and exact blob retrieval |
| `article_fetcher/extract.py` | HTML body and semantic-table extraction |
| `article_fetcher/http_safety.py` | Public-address validation and pinned connections |
| `article_fetcher/challenges.py` | Browser-challenge and network-failure detection |
| `article_fetcher/youtube_captions.py` | Bounded YouTube caption retrieval and normalization |
| `article_fetcher/source_evidence.py` | Bounded publisher-declared identity signals from page headers and video descriptions |
| `recovery/__init__.py` | Stable facade for search-based recovery after the original source is blocked |
| `recovery/search_recovery.py` | Same-article, Reuters syndicated-copy, and alternate-reporting recovery attempts |
| `recovery/same_article.py` | Bounded discovery and conservative validation of same-article copies |
| `recovery/syndicated_copy.py` | Discovery and validation of Reuters syndicated copies |
| `recovery/alternate_reporting.py` | Discovery and validation of Reuters reporting on the same event |
| `recovery/fetched_material.py` | Normalized fetched material shared by direct retrieval and recovery |
| `pdf_workers/__init__.py` | Import-free package for PDF workers run as `python -m` subprocesses |
| `pdf_workers/adobe_pdf_extractor.py` | Resource-bounded Adobe PDF-to-Markdown worker |
| `pdf_workers/pdf_extractor.py` | Resource-bounded local PDF text worker |
| `output/__init__.py` | Stable facade for rendering, public payload validation, and publishing |
| `output/render.py` | Markdown, public JSON, and private candidate-audit serialization |
| `output/public_schema.py` | Public payload contract shared by generation and publishing |
| `output/publisher.py` | Website delivery, retry, and local success state |

## Core Invariants

Production `GeminiBackend.from_environment()` enables ordered summary fallback
from `gemini-3.6-flash` to `gemini-3.7-flash` and `gemini-3.8-flash`. A custom
primary has no implicit fallback; an explicit comma-separated
`DAILY_BRIEF_GEMINI_SUMMARIZER_FALLBACK_MODELS` overrides the list, and an empty
value disables it. Direct construction remains single-model by default, keeping
model evaluation isolated. The `evaluate-model` CLI explicitly disables fallback,
even when the environment enables it. Classification has no model fallback.

Only explicit daily-quota 429 errors skip retries and disable a summary model
until the next America/Los_Angeles midnight. This is backend-instance memory,
not a persistent quota counter. Unknown and minute-limit 429 errors retain
bounded same-model retries and do not switch models. Exhausted transient-error
retries (5xx, timeout, network failure) permit the next model; material
insufficiency, invalid output and authentication errors do not. Each summary
starts from the configured primary, skipping models whose daily quota is known
to be exhausted. All summary models share a request-start interval in addition
to existing per-model pacing, including retries and model switches.

`summarizer_model` remains the configured primary; `last_summary_model` identifies
the last attempted model. Private summary audit reads it after the call, on both
success and failure. Attempts cover the whole chain, while provider status and
token usage describe the final attempted model's last response. Fallback logs
record model and error code without credentials. Public output is unchanged.

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
