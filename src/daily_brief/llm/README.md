# LLM

This package holds everything that talks to, or prepares input for, a language
model: topic-classification and summary prompts, bounded evidence excerpts, the
provider-neutral backend contract, the Gemini adapter, and model-input capture
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
| `gemini_backend.py` | Gemini adapter: structured output, validation, pacing, retry, and summary model fallback |
| `topic_classifier.py` | Topic-classification prompt and labels |
| `summarizer.py` | Summary routes, prompts, sufficiency rules, source prefixes, and normalization |
| `evidence_selection.py` | Bounded title-aware excerpts with explicit omissions |
| `model_evaluation.py` | Versioned model-input capture and side-effect-free replay |

## Summary Sufficiency

Prompts and sufficiency rules live in `summarizer.py`; `gemini_backend.py`
validates each route's structured response.

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

`evidence_selection.py` selects excerpts; `summarizer.py` and
`topic_classifier.py` apply them at the model boundary. Full retrieved text is
kept outside this package, as described under Retrieved Material Limits in the
[package guide](../README.md#retrieved-material-limits).

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

## Summary Model Fallback and Quotas

These rules live in `gemini_backend.py`. Quota values and their operational
meaning are listed in [the API quota guide](../../../docs/api-quotas.md) (Chinese).

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
modifying recommendation and publishing state. Capture schema 5 preserves immutable
classification inputs before and after roundup comment retrieval, content kind,
and preselection summary inputs (including rejected roundups). It also preserves
source and HN-discussion inputs when both are attempted for an ordinary item;
schema 3 and 4 captures remain readable. Replay records insufficient material separately from
provider failures and does not retrieve fallback material.

## Dependency Direction

```text
__init__           -> gemini_backend, model_backend, model_evaluation, summarizer
gemini_backend     -> summarizer, topic_classifier
model_evaluation   -> model_backend, summarizer
model_backend      -> topic_classifier
summarizer         -> evidence_selection
topic_classifier   -> evidence_selection
all but evidence_selection -> models; model_evaluation also -> config, article_fetcher.contracts
```

This package never imports `generation/`, `candidates/`, `recovery/`, or
`output/`. Provider-specific behavior stays in `gemini_backend.py`; production
and evaluation share `model_backend.py` and the normalization in `summarizer.py`.

## Tests

- `tests/test_summarizer.py`: routes, prompts, context budgets, and normalization.
- `tests/test_topic_classifier.py`: classification prompt construction and evidence use.
- `tests/test_evidence_selection.py`: excerpt selection and omission notices.
- `tests/test_gemini_backend.py` and `tests/test_gemini_fallback.py`: request
  payloads, response validation, retries, pacing, and summary model fallback.
- `tests/test_model_backend.py`: the provider-neutral topic-decision contract.
- `tests/test_model_evaluation.py`: capture schemas and replay.

Tests use a fake HTTP opener and never call the Gemini API.
