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
6. Candidates with explicit non-weak keyword matches enter the AI pool directly. `topic_classifier.py` evaluates the highest-ranked remaining candidates for AI relevance using titles, source hosts, and bounded excerpts of already-available Hacker News story text.
7. `selection.py` applies eligibility thresholds, ranking, and section limits to select AI stories and a small number of non-AI hot stories.
8. `article_fetcher.py` retrieves article text when needed, and `summarizer.py` supplies the shared grounded-summary prompt and canonicalizes model output before it enters rendering or evaluation artifacts.
9. `render.py` produces the Markdown brief, schema-versioned public JSON, and candidate JSON, and `history.py` records the selected story IDs.
10. `publisher.py` sends only the targeted daily public JSON to the website and records its successful content hash for idempotent retry. Normal scheduled publishing targets the current Daily Brief date; historical publishing is explicit.

`model_backend.py` is the provider-neutral boundary for classification and
summarization. Production constructs `GeminiBackend` by default. `CodexBackend`
remains available as an explicit local fallback and delegates to the existing
Codex classifier and summarizer.

`gemini_backend.py` implements the production and evaluation provider boundary. It
uses the Interactions REST API directly through the Python standard library,
pins classification and summarization model IDs independently, requests structured
JSON, and validates provider output again against application invariants. The
adapter keeps its endpoint fixed, authenticates only with the `x-goog-api-key`
header, disables interaction storage, and performs bounded retry with backoff and
jitter only for network failures, HTTP 408/429, and 5xx responses. Provider retry
delays from `Retry-After`, structured `google.rpc.RetryInfo` details, or Gemini's
explicit `Please retry in ...` quota message take precedence over local backoff
and are capped at 60 seconds. The summarization generation budget leaves room for
model thinking tokens, while the returned summary remains constrained by the
structured schema and a local character cap.

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
| `model_backend.py` | Provider-neutral model contracts and the local Codex fallback adapter | Adding a model provider or changing provider selection |
| `gemini_backend.py` | Gemini Interactions API adapter, structured-output validation, and bounded retry | Gemini models, request/response handling, provider errors, or usage logging |
| `model_evaluation.py` | Versioned model-input capture and side-effect-free replay | Comparing classifier or summarizer backends on identical inputs |
| `topic_classifier.py` | Shared classification prompt and local Codex classifier for candidates without strong keyword evidence | Topic routing, bounded story-text context, or classifier prompts |
| `scoring.py` | Heat, keyword, and topic scoring | Ranking formulas or recommendation reasons |
| `selection.py` | Duplicate handling and final section selection | Eligibility, quotas, deduplication, or rejection reasons |
| `history.py` | Recent recommendation history | Repeat suppression or history retention |
| `article_fetcher.py` | Bounded public HTTP(S) article fetching and visible-text extraction | Article retrieval, parsing, or network safety |
| `summarizer.py` | Shared summary prompt, Codex execution, provider-neutral typography normalization, and fallback text | Summary prompts, execution, output normalization, or fallback behavior |
| `render.py` | Markdown brief and candidate JSON serialization | Output format |
| `publisher.py` | Authenticated website publishing, retry, and local success state | Delivery behavior or publish configuration |

## Important Invariants

- Hacker News titles, story text, fetched article content, URLs, and source hosts are untrusted input. Model prompts must continue to label supplied content as untrusted and must not follow instructions contained in it.
- Article retrieval must only access validated public HTTP(S) destinations. Preserve address validation across the initial URL, redirects, and the final response URL, along with response size and timeout bounds.
- Tests must be deterministic and must not call live Hacker News APIs, Gemini, or the real `codex` command.
- Model evaluation input is schema-versioned, bounded to the production classifier and section limits, and contains only the exact candidate fields used by model prompts. Keep generated evaluation inputs and results under the Git-ignored `data/` directory because they can include public article text.
- `evaluate-model` is read-only with respect to its input, `recommendation-history.json`, and `publish-state.json`. It may write only its backend-specific result file under `data/model-evaluations/`.
- Production `generate` defaults to Gemini; Codex remains an explicit operator-selected fallback. Backend construction must happen before source fetching so missing production credentials fail without partially running the pipeline.
- Gemini credentials come only from `GEMINI_API_KEY`; model overrides come from `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL` and `DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL`. Do not add an environment-configurable API endpoint, put the key in URLs or logs, or persist it in evaluation artifacts.
- Gemini requests set `store` to false, but that does not override provider-level Free Tier data-use terms. Only public content approved for provider processing belongs in production requests and model evaluations.
- Topic classification may include only an 800-character, whitespace-normalized excerpt of already-available `story_text` per candidate. It must not add article fetches before selection, and all excerpts remain untrusted prompt content.
- Production and evaluation summaries use the same deterministic boundary normalization: adjacent Han characters and ASCII letters or digits are separated by one space before summaries enter output artifacts.
- A failure in one external source must not discard successful results from another source. Classifier, article-fetch, and summarizer failures must retain their documented fallback behavior.
- `data/recommendation-history.json` suppresses recently selected story IDs. `data/YYYY-MM-DD-hn-candidates.json` is a per-run audit artifact for selection review; the two files are not interchangeable.
- Mutations to `Candidate` fields—including `selected`, `section`, `rejection_reason`, `summary`, `why`, and `topic_route`—are observable in rendered output or candidate audit data. Update tests when their meaning changes.
- Public brief JSON is schema version 1. Every published item carries its stable `hn_item_id`, and the website requires that ID to match the Hacker News discussion URL.
- Publisher credentials come only from `DAILY_BRIEF_PUBLISH_URL` and `DAILY_BRIEF_PUBLISH_TOKEN`. Never write the token into generated artifacts, logs, Git, or tests.
- Publisher requests use the stable `daily-brief-publisher/1.0` user agent so the authenticated machine-to-machine endpoint is not mistaken for a malformed browser client by the public edge.
- `data/publish-state.json` records only successful content hashes. Scheduled publishing targets one date and must never scan or automatically catch up historical files; operators can retry or repair a historical date explicitly with `publish --date`.

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
