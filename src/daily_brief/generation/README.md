# Generation

This package implements the pipeline behind `daily-brief generate`: it turns
collected Hacker News stories into a rendered brief, a public payload, and a
private candidate audit. Its package-level facade is the only import used by
the rest of the application:

```python
from daily_brief.generation import run_generate
```

The package is responsible for stage order, candidate routing and selection,
material preparation, recovery dispatch, summary orchestration, and writing
date-scoped artifacts. Argument parsing and command dispatch stay in `cli.py`.
Article transport and extraction stay in [`article_fetcher/`](../article_fetcher/README.md);
search-based recovery attempts and their validation stay in `recovery/`;
prompts, routes, evidence selection, and model adapters stay in `llm/`; rule-based
collection, scoring, history, and selection stay in `candidates/`; rendering
and publishing stay in `output/`.

## Generation Flow

`run_generate(...)` and `run_retry(...)` use `llm.create_model_backend()` when no backend is injected, matching CLI provider selection through `DAILY_BRIEF_MODEL_BACKEND`.

`run_generate(...)` runs these stages in order. Each step names the module in
this package that owns it.

1. `pipeline.py`: `time_window.py` calculates the daily collection window in the
   Asia/Singapore timezone.
2. `pipeline.py`: `candidates/hn_client.py` collects recent Algolia stories and hot stories
   from the official Hacker News API. Both sources are required; failure of either
   source aborts the run before date-scoped artifacts are written or replaced.
3. `pipeline.py`: `candidates/selection.py` deduplicates candidates, while
   `candidates/history.py` excludes recently recommended stories.
   `candidates/keywords.py` and `candidates/scoring.py` establish
   the initial core candidates and ranking order.
4. `classification.py`: A bounded set of remaining candidates, including
   keyword-matched HN self-posts, is fetched under the classification retrieval
   policy (through `material.py`) and classified by article evidence as AI, other
   core computing, outside the core scope, or uncertain. Retrieval and classifier
   failures fail closed for that candidate.
   The classifier can first route project-sharing, tool-recommendation, and
   practical-experience solicitations as `community_roundup`. Only self-posts
   qualify; informative self-posts and general debates keep the normal route.
   Roundups fetch a bounded HN comment sample, prioritizing top-level answers,
   then receive a second topical classification based on the comments. The
   question provides context only. All self-posts share the existing candidate
   inspection limit, with at most two classification calls per candidate.
5. `classification.py`: Before final selection, roundups must also pass their
   summary call (through `summaries.py`): comments must support at least two
   concrete projects, tools, or practical examples.
   Insufficient comments, uncertain topics, retrieval errors, or model failures
   exclude the item so other eligible candidates can fill the slots. Successful
   overviews are reused after selection, with comment usage recorded in the audit
   and public provenance, without adding a fixed source prefix. This preselection
   work is bounded by the classification pool.
   Confirmed core candidates join one ranked pool. Confirmed outside candidates
   must also satisfy the exploration eligibility rules and are ranked separately.
6. `material.py` (with `recovery/`): Selected external stories are
   retrieved under the fuller summary policy. For every ordinary selected item,
   it also obtains one bounded HN discussion sample before summary generation;
   this is independent of external retrieval success or apparent webpage
   sufficiency. A failed or empty discussion sample is recorded and does not
   prevent the remaining material from being summarized.
   Material fetched during classification is reused. Every fetched public PDF is
   Adobe PDF-to-Markdown first when credentials are configured, with the same
   bounded hard timeout in classification and summary retrieval, conversion
   duration logging, and a logged local `pypdf` fallback.
   Specialized GitHub, YouTube, HTML, and PDF paths remain behind the same bounded
   retrieval interface; recovery material is accepted only after deterministic
   validation.
   After an origin browser challenge exhausts retrieval, selected-item summary
   retrieval first tries `recovery/same_article.py`: one title-based Tavily basic query,
   ten discovery candidates, and at most three unique candidate fetches. Only HN
   is excluded. An independently fetched nonempty title, explicit publisher
   cross-post/republication backlink to the source, and substantive body are
   required. YouTube candidates use the existing captions path and must declare
   narration of that source. Search snippets and bare/canonical backlinks are
   insufficient. Rejections continue to source-appropriate recovery: Reuters syndicated copies
   or same-event reporting from other publishers. The latter reuses already
   fetched pages before issuing an additional search, while preserving the
   original URL and marking accepted material as `alternate_reporting`.
7. `summaries.py`: Ordinary items make one summary call after material preparation.
   `llm/summarizer.py` presents webpage metadata, extracted webpage material, HN
   self-post text, and the discussion sample as separately labeled inputs. It
   assesses sufficiency across the material supplied to that one call; a semantic
   insufficiency result does not cause a second discussion-fallback call. The
   model returns one integrated summary and its declared sources. Code validates
   source availability and records usage without adding source prefixes or a
   separate comment note. Recorded per-call requested reasoning effort follows the
   summary diagnostics into private audit and optional public generation info;
   no effort is inferred for backends or historical items without a record.
   `community_roundup` remains the exception: its comment-led overview was
   assessed before selection and is reused. An external-source retrieval failure
   never becomes a title- or model-knowledge-based article summary.
8. `pipeline.py`: `output/render.py` writes the readable Markdown, validated public JSON,
   and private candidate audit. `candidates/history.py` then records selected item IDs. An
   empty brief writes a `.no-content` marker instead of public JSON.

## Retry Existing Items

`daily-brief retry --date YYYY-MM-DD` refreshes selected items whose saved summary,
webpage retrieval, or comment retrieval failed (including insufficient/skipped
summaries). `--item HN_ID` can be repeated to explicitly refresh selected items,
including a successful but unhelpful summary. A previous retry's failed HN post
request also remains eligible. The command requires the matching public JSON and
candidate audit; missing, invalid, or inconsistent inputs fail before retrieval.

`retry.py` loads those two artifacts to preserve selected IDs, section order,
titles, links, original points/comment counts, and recommendation reasons. It
fetches fresh HN submission text, follows the existing selected-item webpage
recovery policy, and retrieves a new bounded comment sample. Ordinary items use
the same four-material summary stage as generation. Selected community roundups
repeat their existing comment qualification and structured overview without
running topic classification or selection again. Historical retry dates determine
the Wayback time window; fetched live pages and discussions reflect the retry time.

A successful summary replaces only that item's public summary/provenance/status
and generation diagnostics, then regenerates Markdown from the saved selection.
A failed attempt leaves its prior public result and base audit intact; its latest
attempt diagnostics are stored under `last_retry` in the candidate audit. This
single record is replaced on the next retry, not appended indefinitely. HN post acquisition is recorded separately in the candidate audit and public
`generation_info`, so a failed fresh story request cannot appear as an empty post.
The public generation info remains paired with the saved summary when a retry
fails. No raw page, post, comment text, or model-input capture is written. Explicit model
comparison captures remain a separate opt-in workflow.

Writes use atomic file replacement and check for changes to the inputs made while
retrieval was running. Ordinary write errors roll back artifacts already replaced;
this is not a cross-file crash transaction. Retry does not change recommendation
history or publishing state and never publishes. Use `publish --date YYYY-MM-DD`
separately after checking the local result. No eligible items is a no-op; any
unsuccessful target gives the CLI a nonzero exit status, even if other items update.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade: generation and retry entry points, results, and errors |
| `pipeline.py` | Stage order, candidate collection, history exclusion, and artifact writes |
| `retry.py` | Refresh selected saved items, preserve successful results, and replace local artifacts |
| `classification.py` | Keyword routing, bounded topic classification, roundup assessment, and section selection |
| `summaries.py` | Selected-item single-call summary loop and summary diagnostics |
| `material.py` | Classification/summary retrieval modes, recovery dispatch, and bounded HN discussion material |

## Dependency Direction

Keep dependencies directed from the pipeline toward later, lower-level stages:

```text
__init__       -> pipeline, retry
pipeline       -> classification, summaries
retry          -> summaries, material
classification -> summaries, material
summaries      -> material
```

A module must not import a module above it in this list, and no module in this
package imports `cli.py`. `cli.py` imports only the package facade. Other
packages, including `recovery/`, never import this package; this keeps the
dependency graph free of cycles.

## Invariants

- External collaborators (story sources, article fetcher, discussion fetcher,
  search finders, classifier, summarizer, and model backend) remain injectable
  through `run_generate(...)`, so tests never reach live services.
- Behavioral invariants shared with other packages, such as fail-closed
  retrieval, recovery eligibility, and public/private output boundaries, are
  listed under Core Invariants in the [package guide](../README.md#core-invariants).

## Tests

End-to-end tests run `run_generate(...)` with injected fakes and live in
`tests/generation/`, one file per stage module: `test_pipeline.py`,
`test_classification.py`, `test_material.py`, `test_recovery.py`, and
`test_summaries.py`, and `test_retry.py`. `tests/generation/conftest.py` replaces the default model
backend, article fetcher, and discussion fetcher for every test there; shared
fakes and story builders live in `tests/fakes.py`.

- `tests/test_same_article_pipeline.py`: same-article recovery and its hand-off
  to the other recovery routes and to discussion fallback.
- `tests/generation/test_openrouter_pipeline.py`: the real OpenRouter adapter with fake HTTP responses through roundup classification, comment assessment, summary reuse, and rendering.
- `tests/test_gemini_fallback.py` and `tests/test_gemini_backend.py`: summary
  diagnostics recorded by `summaries.generate_candidate_summary(...)` with the
  real Gemini adapter and a fake transport.

During development, run the relevant tests. Before completing a behavior or code
change, run the full suite as required by the repository guide:

```sh
pytest -q tests/generation tests/test_same_article_pipeline.py
pytest -q
```
