# LLM

This package holds everything that talks to, or prepares input for, a language
model: topic-classification and summary prompts, bounded evidence excerpts, the
provider-neutral backend contract, the Gemini and OpenRouter adapters, and model-input capture
and replay. The package-level facade is the import used by the rest of the
application:

```python
from daily_brief.llm import GeminiBackend, build_summary_context
```

Deciding which candidates are classified or summarized, and falling back to the
HN discussion, stays in [`generation/`](../generation/README.md). Retrieving
article text stays in [`article_fetcher/`](../article_fetcher/README.md), and
writing summaries into the brief stays in `output/`.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade and supported imports |
| `model_backend.py` | Provider-neutral classification and summarization contract |
| `factory.py` | Shared environment-based backend selection for CLI, generation, retry and evaluation |
| `openrouter_backend.py` | Chat-completions adapter, provider price caps, per-run cost reservations, retries and usage records |
| `gemini_backend.py` | Gemini adapter: configuration, pacing, requests and retries, and summary model fallback |
| `gemini_output.py` | Structured-output schemas for classification and summaries, and response validation |
| `gemini_api.py` | Gemini errors, HTTP error and daily-quota interpretation, retry delays, response text and usage extraction, and interaction logs |
| `topic_classifier.py` | Topic-classification prompt and labels |
| `summarizer.py` | Summary routes, prompts, sufficiency rules, source prefixes, and normalization |
| `evidence_selection.py` | Bounded title-aware excerpts with explicit omissions |
| `model_evaluation.py` | Versioned model-input capture and side-effect-free replay |

## Summary Sufficiency

Prompts and sufficiency rules live in `summarizer.py`; `gemini_output.py`
validates each route's structured response.

Each ordinary summary call returns a validated sufficient/insufficient decision
across separately labeled webpage metadata, extracted webpage text, HN submission
text, and a bounded HN comment sample. It returns one integrated `summary`, a
`summary_sources` array, and `reason` alongside `status`. Comments can contribute
to the summary when useful; there is no separate comment note. Available but
unused sources are omitted from the array. Validation rejects unknown, duplicate,
or unavailable sources and inconsistent sufficiency decisions before updating the
audit. Source usage is model-declared, not claim-level verification.

Reader-preference guidance retains useful concrete mechanisms and metric scope even when that takes more space; it does not optimize for the shortest possible text.

The prompt organizes the text around the subject, key facts or mechanisms, and
conditions or limitations. Length is flexible: usually two to four sentences,
shorter for simple material and longer when explanation requires it. The existing
1,000-character validation ceiling remains a defensive bound, not a length target.
The material prompt puts the core explanation and its limits ahead of examples.
It requires availability status (available, preview, planned), the distinction
between an author's expectation and an observed result, and metric definitions
and measurement scope beside the claims they qualify. Unknown details must stay
unknown or be omitted rather than inferred. These are generation instructions,
not semantic checks performed by the response validator.
No code-owned source prefix is added to ordinary material summaries. Claims and
personal experiences that need qualification are attributed naturally within the
relevant sentence; comment claims cannot become article facts or community
consensus. Metadata is publisher context, not independent verification.
When the webpage body is alternate Reuters reporting, the body label and an
`alternate_reporting` prompt module tell the model it is a different article about
the same event, so its claims are not attributed to the linked source; the reader-
facing summary still carries no prefix, and provenance records the origin.
Recommendation questions in ordinary self-posts are context; the summary focuses
on concrete answers and their explanations rather than restating the question.
It starts with the recommendations themselves, retains the substance and direction
of evaluations, and avoids repeating who recommended or mentioned each item.
Useful comment details belong next to the subject they explain, without a closing
comment appendix; attribution remains when needed to qualify a claim or experience.

Roundups retain their separate sufficiency rule, preselection, and structured
introduction plus two or three substantive entries. The adapter formats the
introduction and bullet list without a fixed source prefix; comments remain the
sole substantive evidence and the question only provides context. The pipeline
records `hn_comments` usage. `output/render.py` preserves those line breaks in
Markdown and the existing public `summary` string.

The backend returns text or raises `InsufficientSummaryMaterial`; the latter is
a completed semantic decision, not a provider error. No character minimum is used
for material sufficiency. There is no semantic second call that adds comments:
ordinary selected items fetch their one sample before the call. Empty or failed
sources do not suppress useful remaining material, including comments alone.

Public schema is unchanged. Source details are carried by existing public
provenance enums where expressible and by the private `summary_sources_used`
array; unsupported public combinations remain `unknown`. Fetched comments do not
imply used comments. Historical legacy replay routes keep their older response
contracts and source prefixes; current generation and retry use the integrated
material contract for ordinary items.

## Bounded Evidence Selection

`evidence_selection.py` selects excerpts; `summarizer.py` and
`topic_classifier.py` apply them at the model boundary. Full retrieved text is
kept outside this package, as described under Retrieval Flow in the
[article_fetcher guide](../article_fetcher/README.md#retrieval-flow).

`llm/evidence_selection.py` supplies deterministic title/URL-fragment-aware excerpts
at the model boundary: 6,000 characters for classification; for ordinary summaries,
up to 24,000 characters shared by webpage metadata and webpage text, plus separately
bounded HN post text (6,000) and comments (16,000). Comments keep the sampler’s order and parent context;
oversized replay samples are truncated at the end with an explicit marker. The four inputs retain their
separate boundaries. Full `Story.fetched_text` is never replaced by excerpts,
including when classification material is reused for a selected item. Capture/replay
accepts full fetched text up to the same UTF-8 byte ceiling. Short classifier inputs
keep legacy whitespace normalization; long inputs retain block boundaries.

Long text is divided into bounded blocks. Rare matching title/anchor terms rank
blocks; adjacent blocks retain local qualifications and headings when extraction
preserved them. Publisher metadata keeps its attribution wrapper. No lexical match
uses bounded distributed sampling, explicitly distinguished from relevance matching.
All excerpts carry omission notices; matching never establishes factual truth.
Summary audit records aggregate source and selected lengths, the input strategy,
available source sections, source labels actually used, and half-open character
ranges. Ordinary
ranges identify their own material origin; `research_chars` ranges refer to the
assembled research sections, whose names remain in the audit. Research section selection precedes this budget; each chosen section receives a
separate share with head/tail coverage so a title match cannot displace results or
limitations. Comment sampling remains independently bounded.

Shared sufficiency instructions apply to short and long sources: only facts relevant
to the current item count, the HN title is not evidence, and a concrete short
announcement can suffice. Absence from excerpts is not absence from the page.
Selection is lexical, not semantic, and makes no additional provider calls.
All summary routes share an event-scope instruction: summarize the item rather
than the entire source page, retain relevant conditions and limitations, and omit
other updates that only share a product or keywords. Source evidence overrides
unsupported title claims. This is a generation instruction, not a post-generation
quality gate; evidence selection and provider-call counts are unchanged.

## Provider Selection and OpenRouter

`create_model_backend()` reads `DAILY_BRIEF_MODEL_BACKEND` (`gemini` by default for
existing installations; `openrouter` opts in). Generate, retry, evaluate-model and
library defaults use the same factory. Unknown providers and missing credentials
fail before CLI collection. Evaluation disables Gemini cross-model fallback;
OpenRouter never uses cross-model fallback.

OpenRouter defaults to `qwen/qwen3.8-flash` with reasoning disabled for classification
(512 output tokens), and `openai/gpt-6-luna` with requested low reasoning for summaries
(8192 output tokens). Actual reasoning usage can be zero. All summary routes reuse
the existing schemas and validators, including the comments-only roundup list.
Requests use strict JSON schema and require provider parameter support; same-model
provider fallback is allowed. The fixed HTTPS endpoint rejects redirects.

Each HTTP attempt is bounded by a 90-second timeout and 256 KiB response limit.
429, 5xx and transport failures get at most two retries with bounded backoff and
per-model request spacing. 400/401/402/403, invalid JSON or task output, refusal,
non-stop completion and insufficient material do not trigger retries or model
switching. Embedded errors in successful HTTP responses follow the same policy.
Untrusted provider error bodies and credentials are not logged.

A backend instance reserves a conservative input/output cost before every attempt,
using UTF-8 input bytes plus framing allowance and configured per-million price
caps. Luna reserves at least its cache-write input rate. Valid response cost
replaces that attempt's reservation; unknown-cost failures retain reservations.
The default $0.25 instance budget is shared across classification, summaries and
retries; it resets in a new process and is not a monthly account limit. Price caps
are sent to the provider router. Custom model IDs require explicit input/output
caps; operators must also account for that model's caching charges.

Safe per-attempt records are available in `request_records`, and logs include
reported cost and accounted total for both tasks. Summary token diagnostics use
the existing candidate audit fields; `last_summary_usage` additionally exposes
cost/cache details and `last_summary_provider` the upstream provider. No costs or
provider error bodies enter the public brief schema.

Configuration and rollout notes are in [docs/openrouter.md](../../../docs/openrouter.md).

## Summary Model Fallback and Quotas

These rules live in `gemini_backend.py`, with daily-quota detection in
`gemini_api.py`. Quota values and their operational meaning are listed in
[the API quota guide](../../../docs/api-quotas.md) (Chinese).

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

## Model Evaluation

Capture is written by `generation/pipeline.py` through
`capture_model_evaluation_input(...)`; replay runs through `daily-brief
evaluate-model`. Both live in `model_evaluation.py`.

Model comparison is intentionally separate from generation. A generation run can
capture the exact classifier and summarizer inputs, and `evaluate-model` can
replay that immutable input without fetching sources, rendering a brief, or
modifying recommendation and publishing state. Capture schema 6 records
`summary_input_mode`: new ordinary candidates use `materials`, while historical
captures retain their legacy mode. It preserves each material origin and retrieval
method as well as the summary result and declared source usage. Schemas 3, 4, and 5
remain readable. Replay records insufficient material separately from provider
failures and does not retrieve fallback material.

## Dependency Direction

```text
__init__           -> factory, openrouter_backend, gemini_backend, model_backend, model_evaluation, summarizer
factory            -> openrouter_backend, gemini_backend
openrouter_backend -> gemini_output, gemini_api (shared error/size definitions), summarizer, topic_classifier
gemini_backend     -> gemini_output, gemini_api, summarizer, topic_classifier
gemini_output      -> gemini_api, summarizer, topic_classifier
model_evaluation   -> model_backend, summarizer
model_backend      -> topic_classifier
summarizer         -> evidence_selection
topic_classifier   -> evidence_selection
all but evidence_selection and gemini_api -> models; model_evaluation also -> config, article_fetcher.contracts
```

This package never imports `generation/`, `candidates/`, `recovery/`, or
`output/`. Provider wire behavior stays in its adapter; `gemini_output.py` retains its historical name while providing shared task schemas and validators; production
and evaluation share `model_backend.py` and the normalization in `summarizer.py`.
Production model identifiers remain explicit rather than moving aliases.

## Tests

- `tests/test_summarizer.py`: routes, prompts, context budgets, and normalization.
- `tests/test_topic_classifier.py`: classification prompt construction and evidence use.
- `tests/test_evidence_selection.py`: excerpt selection and omission notices.
- `tests/test_gemini_backend.py` and `tests/test_gemini_fallback.py`: request
  payloads, response validation, retries, pacing, and summary model fallback.
- `tests/test_model_backend.py`: the provider-neutral topic-decision contract.
- `tests/test_model_evaluation.py`: capture schemas and replay.

OpenRouter tests cover task routes, validation, redirects/errors, bounded retries, usage and cost reservations. Tests use fake HTTP openers and never call live model APIs.
