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
   overviews are reused after selection, with a code-owned partial-comment
   attribution. This preselection work is bounded by the classification pool.
   Confirmed core candidates join one ranked pool. Confirmed outside candidates
   must also satisfy the exploration eligibility rules and are ranked separately.
6. `material.py` (with `recovery/`): Selected external stories are
   retrieved under the fuller summary policy.
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
   is excluded. An independently fetched matching title, explicit publisher
   cross-post/republication backlink to the source, and substantive body are
   required. YouTube candidates use the existing captions path and must declare
   narration of that source. Search snippets and bare/canonical backlinks are
   insufficient. Rejections continue to the existing Reuters recovery routes.
7. `summaries.py` and `material.py`: If every external-source retrieval and
   recovery path fails for a selected story, or its summary call explicitly finds
   the retrieved material insufficient, `material.py` asks `candidates/hn_client.py` for a
   bounded HN discussion sample as the final fallback.
   `llm/summarizer.py` selects the generic, memorial, research, or HN-discussion route
   from available evidence. An external-source retrieval failure never becomes a
   title- or model-knowledge-based article summary.
8. `pipeline.py`: `output/render.py` writes the readable Markdown, validated public JSON,
   and private candidate audit. `candidates/history.py` then records selected item IDs. An
   empty brief writes a `.no-content` marker instead of public JSON.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade: `run_generate`, `GenerateResult`, `SourceCollectionError` |
| `pipeline.py` | Stage order, candidate collection, history exclusion, and artifact writes |
| `classification.py` | Keyword routing, bounded topic classification, roundup assessment, and section selection |
| `summaries.py` | Selected-item summary loop, discussion fallback, and summary diagnostics |
| `material.py` | Classification/summary retrieval modes, recovery dispatch, and HN discussion material |

## Dependency Direction

Keep dependencies directed from the pipeline toward later, lower-level stages:

```text
__init__       -> pipeline
pipeline       -> classification, summaries
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

- `tests/test_cli.py`: end-to-end generation through `run_generate(...)` with
  injected fakes, plus command dispatch in `cli.main()`.
- `tests/test_same_article_pipeline.py`: same-article recovery and its hand-off
  to the other recovery routes and to discussion fallback.
- `tests/test_gemini_fallback.py` and `tests/test_gemini_backend.py`: summary
  diagnostics recorded by `summaries.generate_candidate_summary(...)` with the
  real Gemini adapter and a fake transport.

During development, run the relevant tests. Before completing a behavior or code
change, run the full suite as required by the repository guide:

```sh
pytest -q tests/test_cli.py tests/test_same_article_pipeline.py
pytest -q
```
