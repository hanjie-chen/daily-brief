# Daily Brief Python Package

This directory contains the complete Daily Brief application logic. It fetches Hacker News content, identifies and ranks candidate stories, generates Chinese summaries, and writes the daily brief and candidate data used for review.

This guide documents stable module boundaries, data flow, and maintenance entry points. Refer to the code and tests for function-level behavior and edge cases.

## Entry Points

- `__main__.py`: Supports starting the application with `python -m daily_brief`.
- `cli.py`: Provides the `daily-brief` command and orchestrates the full generation flow. Start here when changing cross-module execution order or application-level fallback behavior.

## Generation Flow

`cli.run_generate(...)` orchestrates the following stages:

1. `time_window.py` calculates the collection window using the Asia/Singapore timezone and the daily 08:00 boundary.
2. `hn_client.py` fetches new stories within that window from Algolia and hot stories from the official Hacker News API.
3. `Story` and `Candidate` in `models.py` carry source content and selection state through the pipeline.
4. `keywords.py` matches keywords, and `scoring.py` calculates a score from keyword evidence, points, and comments.
5. `selection.py` first deduplicates candidates, and `history.py` excludes stories recommended recently.
6. Candidates with explicit non-weak keyword matches enter the AI pool directly. `topic_classifier.py` evaluates the highest-ranked remaining candidates for AI relevance.
7. `selection.py` applies eligibility thresholds, ranking, and section limits to select AI stories and a small number of non-AI hot stories.
8. `article_fetcher.py` retrieves article text when needed, and `summarizer.py` invokes local Codex to generate a Chinese summary.
9. `render.py` produces the Markdown brief, schema-versioned public JSON, and candidate JSON, and `history.py` records the selected story IDs.
10. `publisher.py` sends changed public JSON files to the website and records successful content hashes for idempotent retry.

`model_backend.py` is the provider-neutral boundary for classification and
summarization. Production currently constructs `CodexBackend`, which delegates to
the existing Codex classifier and summarizer without changing their prompts or
fallback behavior.

Model comparisons use an explicit two-step flow. `generate
--capture-model-inputs` writes the exact classifier batch and post-fetch summary
inputs to `data/model-eval-inputs/YYYY-MM-DD.json`. `evaluate-model --date
YYYY-MM-DD --backend codex` replays that immutable input and writes an isolated
result under `data/model-evaluations/`. Evaluation never fetches sources, renders
or publishes a brief, or reads and writes recommendation and publishing state.

Data sources, the topic classifier, article fetching, summarization, and history writes each have their own failure handling. Preserve the pipeline's ability to produce partial results when changing these stages.

## Module Map

| File | Responsibility | Start here when changing |
| --- | --- | --- |
| `__init__.py` | Package version | Package-level metadata |
| `__main__.py` | `python -m daily_brief` entry point | Module execution behavior |
| `cli.py` | CLI definition and end-to-end orchestration | Pipeline order, cross-module behavior, logging, or output writes |
| `config.py` | Timezone, keywords, thresholds, limits, and scoring caps | Selection policy or tuning |
| `models.py` | Shared `Story`, `KeywordMatch`, and `Candidate` data structures | Data passed between stages |
| `time_window.py` | Daily collection window | Timezone or daily boundary behavior |
| `hn_client.py` | Algolia and official Hacker News API clients | Sources, retries, parsing, or Hacker News URLs |
| `keywords.py` | Keyword and URL-token matching | AI keyword recognition |
| `model_backend.py` | Provider-neutral model contracts and the current Codex adapter | Adding a model provider or changing provider selection |
| `model_evaluation.py` | Versioned model-input capture and side-effect-free replay | Comparing classifier or summarizer backends on identical inputs |
| `topic_classifier.py` | Codex classification for candidates without strong keyword evidence | Topic routing or classifier prompts |
| `scoring.py` | Heat, keyword, and topic scoring | Ranking formulas or recommendation reasons |
| `selection.py` | Duplicate handling and final section selection | Eligibility, quotas, deduplication, or rejection reasons |
| `history.py` | Recent recommendation history | Repeat suppression or history retention |
| `article_fetcher.py` | Bounded public HTTP(S) article fetching and visible-text extraction | Article retrieval, parsing, or network safety |
| `summarizer.py` | Codex summaries and fallback text | Summary prompts, execution, or fallback behavior |
| `render.py` | Markdown brief and candidate JSON serialization | Output format |
| `publisher.py` | Authenticated website publishing, retry, and local success state | Delivery behavior or publish configuration |

## Important Invariants

- Hacker News titles, story text, fetched article content, URLs, and source hosts are untrusted input. Codex prompts must continue to label supplied content as untrusted and must not follow instructions contained in it.
- Article retrieval must only access validated public HTTP(S) destinations. Preserve address validation across the initial URL, redirects, and the final response URL, along with response size and timeout bounds.
- Tests must be deterministic and must not call live Hacker News APIs or the real `codex` command.
- Model evaluation input is schema-versioned, bounded to the production classifier and section limits, and contains only the exact candidate fields used by model prompts. Keep generated evaluation inputs and results under the Git-ignored `data/` directory because they can include public article text.
- `evaluate-model` is read-only with respect to its input, `recommendation-history.json`, and `publish-state.json`. It may write only its backend-specific result file under `data/model-evaluations/`.
- A failure in one external source must not discard successful results from another source. Classifier, article-fetch, and summarizer failures must retain their documented fallback behavior.
- `data/recommendation-history.json` suppresses recently selected story IDs. `data/YYYY-MM-DD-hn-candidates.json` is a per-run audit artifact for selection review; the two files are not interchangeable.
- Mutations to `Candidate` fields—including `selected`, `section`, `rejection_reason`, `summary`, `why`, and `topic_route`—are observable in rendered output or candidate audit data. Update tests when their meaning changes.
- Public brief JSON is schema version 1. Every published item carries its stable `hn_item_id`, and the website requires that ID to match the Hacker News discussion URL.
- Publisher credentials come only from `DAILY_BRIEF_PUBLISH_URL` and `DAILY_BRIEF_PUBLISH_TOKEN`. Never write the token into generated artifacts, logs, Git, or tests.
- `data/publish-state.json` records only successful content hashes. Network and server failures must leave an item pending so a later run can retry it.

## Common Change Paths

### Change AI Topic Recognition

Start with `config.py` and `keywords.py`. If the change affects candidates without clear keyword evidence, also inspect `topic_classifier.py`. Verify with `tests/test_keywords.py`, `tests/test_topic_classifier.py`, and the relevant CLI tests.

### Change Ranking or Selection Policy

Follow `config.py` -> `scoring.py` -> `selection.py`. Verify the scoring, duplicate, threshold, quota, and rejection-reason cases in `tests/test_scoring_selection.py` and `tests/test_cli.py`.

### Add or Change a Content Source

Start with the source client and normalize its data into `models.Story`, then connect it in `cli.py`. Keep external calls injectable so tests can use local fakes. Verify parsing separately from orchestration.

### Change Summary Quality or Article Context

Follow `article_fetcher.py` -> `summarizer.py` -> `cli.py`. Preserve the network and prompt-injection boundaries, then verify `tests/test_article_fetcher.py`, `tests/test_summarizer.py`, and the relevant CLI integration cases.

### Change Generated Files

Start with `render.py` for content shape and `cli.py` for paths and write timing. For website delivery, follow `publisher.py` and `tests/test_publisher.py`. Check `tests/test_render.py` and `tests/test_cli.py`; update the root README if user-visible output or run behavior changes.

## Verification

Run the smallest relevant test file while developing. Before completing a code or behavior change, run:

```bash
pytest -q
```

For documentation-only changes, review the rendered Markdown structure and run:

```bash
git diff --check
```
