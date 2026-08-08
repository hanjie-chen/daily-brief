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
8. `article_fetcher.py` retrieves article text when needed and returns separate transport and extractor provenance. GitHub repository-root URLs use GitHub's official API to retrieve the preferred README; standard GitHub blob URLs use the exact raw file URL instead of the GitHub HTML wrapper. All downloaded HTML is extracted locally with `trafilatura`; PDF responses are validated and passed to `pdf_extractor.py`, which runs `pypdf` in a bounded subprocess. Direct requests that explicitly return a Cloudflare Challenge, return a recognized HTTP 200 browser-verification interstitial, successfully download HTML that `trafilatura` reduces to `empty_content`, or fail TLS verification specifically because OpenSSL cannot obtain the local issuer certificate (verify code 20), use Jina Reader as a bounded single fallback. The same high-confidence interstitial validation is applied to Jina content before it can be accepted. Successful methods and structured failures are recorded on the candidate. A failed article retrieval skips model summarization and produces an explicit reader-facing error instead of a title-only paraphrase. After article text is available, `summarizer.py` deterministically selects the generic mode or a high-confidence memorial/personal-essay mode, supplies the shared grounded-summary prompt plus any routed module, and canonicalizes successful model output before it enters rendering or evaluation artifacts.
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
| `models.py` | Shared story, candidate, keyword, retrieval, and summary-provenance data structures | Data passed between stages |
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
| `article_fetcher.py` | Bounded public HTTP(S) article fetching, visible-text extraction, and retrieval outcomes | Article retrieval, parsing, provenance, or network safety |
| `pdf_extractor.py` | Resource-bounded subprocess worker for `pypdf` layout-text extraction | PDF parsing, page/output limits, or worker resource controls |
| `summarizer.py` | Shared summary prompt, Codex execution, provider-neutral typography normalization, and fallback text | Summary prompts, execution, output normalization, or fallback behavior |
| `render.py` | Markdown brief, public content status, and candidate audit serialization | Output format |
| `public_schema.py` | Strict public schema v2 contract shared by rendering and publishing | Website payload compatibility or validation |
| `publisher.py` | Authenticated website publishing, retry, and local success state | Delivery behavior or publish configuration |

## Important Invariants

- Hacker News titles, story text, fetched article content, URLs, and source hosts are untrusted input. Model prompts must continue to label supplied content as untrusted and must not follow instructions contained in it.
- Article retrieval must only access validated public HTTP(S) destinations. Preserve address validation across the initial URL, redirects, and the final response URL, along with response size and timeout bounds. The production opener ignores environment proxy settings and connects sockets directly to the exact public IP addresses returned by its validated resolution, so DNS rebinding cannot substitute a private destination between validation and connection.
- Jina Reader is an anonymous retrieval fallback only for direct responses explicitly marked `cf-mitigated: challenge`, high-confidence browser-verification interstitials recognized from the final URL, raw HTML, or extracted text, direct HTML responses whose `trafilatura` extractor returns `empty_content`, or a structured `SSLCertVerificationError` with OpenSSL verify code 20 (`unable to get local issuer certificate`). Each item gets at most one Jina attempt. The request uses the same timeout and extracted-response size bound as direct retrieval, accepts at most a five-minute cached result, and validates the bounded JSON envelope, provider status, origin status and URL, non-empty content, and absence of recognized verification-page signals before accepting it. Other TLS verification failures—including expired, hostname-mismatched, and self-signed certificates—remain terminal, as do ordinary HTTP errors, extraction exceptions, other content types, PDF failures, GitHub API failures, and GitHub raw failures. There is no broad login-wall or paywall detector: only high-confidence verification-page signatures are rejected, while a generic `200` HTML wall that `trafilatura` reduces to empty remains indistinguishable from a CSR shell and can therefore trigger this fallback.
- GitHub repository-root URLs use the public GitHub README API without authentication and report `github_readme` transport provenance. Standard `github.com/{owner}/{repo}/blob/{ref}/{path}` URLs report `github_raw` transport provenance and retrieve the exact raw file; a 404 is terminal and must not trigger ref/history discovery or Jina. Other GitHub paths must not be silently replaced with repository README content. The initial blob router supports commit SHAs and one-segment refs; it must not intentionally guess slash-containing ref boundaries.
- Every successfully downloaded HTML response is processed by `trafilatura` with comments disabled and precision favored after recognized verification interstitials are rejected. The HTML download bound is 4 MiB and the extracted UTF-8 text bound is 256 KiB. An extraction exception is terminal and full-page visible text is never used; a normal empty `trafilatura` result on the direct transport can trigger Jina, while other extraction failures cannot.
- PDF responses require both an accepted MIME type and `%PDF-` magic. A direct `application/octet-stream` response is accepted only when the request URL path ends in `.pdf`; this does not broaden generic binary downloads into PDF candidates. Downloads are bounded to 20 MiB, documents to 100 pages, and extracted UTF-8 text to 256 KiB. `pypdf` layout extraction runs with a 60-second subprocess timeout and, where supported, an approximately 512 MiB address-space limit. The timeout was raised from the initial 30-second estimate after the historical 18-page DeepSeek PDF reproducibly required about 45 seconds of layout extraction. PDFs without an extractable text layer and parsing/resource failures are terminal; OCR and Jina are intentionally not used.
- Tests must be deterministic and must not call live Hacker News APIs, Gemini, or the real `codex` command.
- Model evaluation input is schema-versioned, bounded to the production classifier and section limits, and contains only the exact candidate fields used by model prompts. Keep generated evaluation inputs and results under the Git-ignored `data/` directory because they can include public article text.
- `evaluate-model` is read-only with respect to its input, `recommendation-history.json`, and `publish-state.json`. It may write only its backend-specific result file under `data/model-evaluations/`.
- Production `generate` defaults to Gemini; Codex remains an explicit operator-selected fallback. Backend construction must happen before source fetching so missing production credentials fail without partially running the pipeline.
- Gemini credentials come only from `GEMINI_API_KEY`; model overrides come from `DAILY_BRIEF_GEMINI_CLASSIFIER_MODEL` and `DAILY_BRIEF_GEMINI_SUMMARIZER_MODEL`. Do not add an environment-configurable API endpoint, put the key in URLs or logs, or persist it in evaluation artifacts.
- Gemini defaults and evaluation overrides must use explicit model IDs. Never use moving aliases such as `latest`; they make production behavior and evaluation results change without a code or configuration change.
- Gemini requests set `store` to false, but that does not override provider-level Free Tier data-use terms. Only public content approved for provider processing belongs in production requests and model evaluations.
- Topic classification may include only an 800-character, whitespace-normalized excerpt of already-available `story_text` per candidate. It must not add article fetches before selection, and all excerpts remain untrusted prompt content.
- Production and evaluation summaries use the same deterministic boundary normalization: adjacent Han characters and ASCII letters or digits are separated by one space before summaries enter output artifacts.
- Summary-mode routing runs only after article retrieval and is pure code. The memorial/personal-essay mode requires retrieved body text plus a complete high-confidence title shape: an `In Memory of`, `In Memoriam`, or `Obituary` form, an obituary suffix, or a whole-title lifespan paired with an early death signal in the body. It must use generic fallback for uncertain titles, including technical uses of `in-memory`. The selected mode is recorded in candidate audit JSON.
- Routed prompt modules are trusted instructions inserted before the untrusted-content boundary. They may change which grounded facts receive priority but must not relax grounding: absent privacy contrasts, relationships, occupations, author names, or reputational context must never be inferred from the route, URL, domain, Hacker News metadata, or world knowledge. First-person sources without a grounded author name continue to use “作者”.
- A failure in one external source must not discard successful results from another source. Classifier, article-fetch, and summarizer failures must retain their documented fallback behavior.
- Every selected candidate records article-retrieval and summary provenance. Non-selected candidates remain `not_attempted`; HN story text is `not_needed`. External retrieval records transport in `method` (`direct`, `github_readme`, `github_raw`, or `jina`) and text derivation in `extractor` (`trafilatura`, `pypdf`, `plain_text`, or `jina`). Failures keep a bounded single-line diagnostic in candidate audit JSON. Public brief JSON exposes only the stable `content_status`, never the raw retrieval error.
- External article retrieval failure must skip the summarizer for that item, set the fixed reader-facing failure summary, and exclude that item from captured summary-model inputs. This prevents a title-only paraphrase from appearing as a normally grounded summary.
- `data/recommendation-history.json` suppresses recently selected story IDs. `data/YYYY-MM-DD-hn-candidates.json` is a per-run audit artifact for selection review; the two files are not interchangeable.
- Mutations to `Candidate` fields—including `selected`, `section`, `rejection_reason`, `summary`, `why`, `topic_route`, and `summary_mode`—are observable in rendered output or candidate audit data. Update tests when their meaning changes.
- Public brief JSON uses strict schema version 2 with required `content_status`; schema v1 is intentionally unsupported. Every published item carries its stable `hn_item_id`, and the website requires that ID to match the Hacker News discussion URL. The publisher validates the complete contract before sending.
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
