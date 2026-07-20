# Daily Brief Quality Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve topic recall, heat-sensitive ranking, cross-day novelty, summary grounding, and partial-failure behavior without changing historical briefs or snapshots.

**Architecture:** Keep deterministic keyword routing for known AI topics, supplement it with one batch Codex classification of high-heat unmatched candidates, and make every external operation independently fallible. Add focused modules for recommendation history, topic classification, and safe article extraction while keeping CLI orchestration explicit and dependency-injectable for deterministic tests.

**Tech Stack:** Python 3.12 standard library, dataclasses, urllib, subprocess, pytest.

---

## File Map

- `src/daily_brief/config.py`: keyword vocabulary, score caps, classification/history/article limits.
- `src/daily_brief/keywords.py`: case-sensitive brand matching.
- `src/daily_brief/scoring.py`: unique-signal scoring and distinct `Why` output.
- `src/daily_brief/topic_classifier.py`: one safe batch Codex classifier call and response validation.
- `src/daily_brief/history.py`: load, query, prune, and atomically save recommendation history.
- `src/daily_brief/article_fetcher.py`: public-network validation, bounded text/HTML fetch, and extraction.
- `src/daily_brief/hn_client.py`: list- and item-level failure isolation.
- `src/daily_brief/summarizer.py`: grounded prompt and honest fallback.
- `src/daily_brief/cli.py`: integrate routing, history, enrichment, and local degradation.
- `tests/`: focused unit and end-to-end regression coverage; no live network or Codex calls.

### Task 1: Unique relevance scoring and vocabulary recovery

**Files:**
- Modify: `src/daily_brief/config.py`
- Modify: `src/daily_brief/keywords.py`
- Modify: `src/daily_brief/scoring.py`
- Modify: `tests/test_keywords.py`
- Modify: `tests/test_scoring_selection.py`

- [ ] **Step 1: Write failing keyword and scoring tests**

Add cases proving `GPT`, `Codex`, `Qwen`, `Kimi`, `Grok`, `DeepSeek`, `Fable`, `Moonshot`, and `open weights` are non-weak signals; proper-name signals are case-sensitive; repeated identical keyword matches add and display once; total keyword relevance caps at 5 and topic relevance caps at 1.

```python
def test_current_model_names_are_strong_ai_signals():
    for title in ["Qwen 3.8", "The Kimi K3 Moment", "OpenAI Codex Micro", "GPT-5.6 release"]:
        assert any(match.weight != "weak" for match in match_keywords(title, "", ""))


def test_duplicate_keyword_occurrences_score_once():
    candidate = Candidate(
        story=story(1, "Claude repeated", points=100, comments=20),
        matched_keywords=[match("Claude", "high", 4.0), match("Claude", "high", 4.0)],
    )
    scored = score_candidate(candidate)
    assert round(scored.score, 2) == 10.14
    assert scored.why == "keywords: Claude"
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `pytest -q tests/test_keywords.py tests/test_scoring_selection.py`

Expected: failures for missing model names, duplicate scoring, and old cap expectations.

- [ ] **Step 3: Implement vocabulary and unique scoring**

Add explicit high-signal names, a `CASE_SENSITIVE_KEYWORDS` set used by `_iter_keyword_matches`, `KEYWORD_BONUS_CAP = 5.0`, and `TOPIC_BONUS_CAP = 1.0`. In scoring, preserve only the first match for each `(keyword, weight)` pair before computing layer, topic, and display values. Remove the weak-occurrence bonus.

```python
def _unique_matches(candidate: Candidate):
    seen = set()
    for match in candidate.matched_keywords:
        key = (match.keyword, match.weight)
        if key not in seen:
            seen.add(key)
            yield match
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `pytest -q tests/test_keywords.py tests/test_scoring_selection.py`

Expected: all keyword and scoring-selection tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/config.py src/daily_brief/keywords.py src/daily_brief/scoring.py tests/test_keywords.py tests/test_scoring_selection.py
git commit -m "Rebalance AI relevance scoring"
```

### Task 2: Batch Codex topic classifier

**Files:**
- Create: `src/daily_brief/topic_classifier.py`
- Create: `tests/test_topic_classifier.py`

- [ ] **Step 1: Write failing classifier tests**

Cover one subprocess call for a batch, strict JSON parsing, output IDs limited to supplied candidates, neutral temporary working directory, and malformed/empty output raising `RuntimeError`.

```python
def test_classifier_returns_only_supplied_ids(monkeypatch):
    monkeypatch.setattr("subprocess.run", fake_result('["1", "unknown"]'))
    result = CodexTopicClassifier().classify([candidate("1", "Qwen 3.8"), candidate("2", "SQLite")])
    assert result == {"1"}
```

- [ ] **Step 2: Run classifier tests and verify RED**

Run: `pytest -q tests/test_topic_classifier.py`

Expected: import failure because `topic_classifier.py` does not exist.

- [ ] **Step 3: Implement the classifier**

Implement `CodexTopicClassifier(timeout_seconds=90).classify(candidates) -> set[str]`. Build one prompt containing item IDs, titles, and URL hosts; label all entries untrusted; request only a JSON string array. Run `codex exec --ephemeral --skip-git-repo-check --sandbox read-only` in a neutral temporary directory, parse the complete stdout as JSON, require a list of strings, and intersect it with supplied IDs.

```python
payload = json.loads(result.stdout.strip())
if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
    raise RuntimeError("topic classifier returned invalid JSON")
return set(payload) & allowed_ids
```

- [ ] **Step 4: Run classifier tests and verify GREEN**

Run: `pytest -q tests/test_topic_classifier.py`

Expected: all classifier tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/topic_classifier.py tests/test_topic_classifier.py
git commit -m "Add batch AI topic classifier"
```

### Task 3: Seven-day recommendation history

**Files:**
- Create: `src/daily_brief/history.py`
- Create: `tests/test_history.py`

- [ ] **Step 1: Write failing history tests**

Cover missing and malformed files returning empty history, IDs from the previous seven calendar dates being excluded, current-date records being ignored for idempotent reruns, eight-day-old records being allowed, and atomic replacement preserving only the current seven-day window plus the generated date.

```python
def test_recent_ids_ignore_current_date_and_expire_after_seven_days():
    history = {
        "2026-07-20": ["same-day"],
        "2026-07-19": ["yesterday"],
        "2026-07-13": ["seven-days"],
        "2026-07-12": ["expired"],
    }
    assert recent_ids(history, "2026-07-20") == {"yesterday", "seven-days"}
```

- [ ] **Step 2: Run history tests and verify RED**

Run: `pytest -q tests/test_history.py`

Expected: import failure because `history.py` does not exist.

- [ ] **Step 3: Implement history functions**

Provide `load_history(path)`, `recent_ids(history, date_label, days=7)`, and `save_history(path, history, date_label, selected_ids, days=7)`. Validate the JSON object shape, log malformed state and return `{}`, parse ISO dates, replace the same-date entry, prune records outside `0 <= age <= days`, write UTF-8 JSON to a sibling temporary file, then call `Path.replace`.

- [ ] **Step 4: Run history tests and verify GREEN**

Run: `pytest -q tests/test_history.py`

Expected: all history tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/history.py tests/test_history.py
git commit -m "Track recent recommendations"
```

### Task 4: HN request isolation

**Files:**
- Modify: `src/daily_brief/hn_client.py`
- Modify: `tests/test_hn_client.py`

- [ ] **Step 1: Write failing partial-success tests**

Add tests proving a failed topstories request still uses beststories, a failed beststories request keeps topstories, one failed item does not discard later items, and both list failures raise `RequestFailedError` for the existing source-level handler.

```python
def test_fetch_hot_stories_skips_failed_item(monkeypatch):
    responses = {
        HN_TOPSTORIES_URL: [1, 2],
        HN_BESTSTORIES_URL: [],
        HN_ITEM_URL.format(item_id=2): {"id": 2, "type": "story", "title": "kept"},
    }
    def fake_get_json(url):
        if url == HN_ITEM_URL.format(item_id=1):
            raise RequestFailedError("bad item")
        return responses[url]
    monkeypatch.setattr("daily_brief.hn_client._get_json", fake_get_json)
    assert [story.hn_item_id for story in fetch_hot_stories()] == ["2"]
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `pytest -q tests/test_hn_client.py`

Expected: current implementation propagates list/item failures.

- [ ] **Step 3: Implement list and item isolation**

Catch each list request separately, count successful list endpoints, and raise a terminal `RequestFailedError` only if neither list succeeded. Catch each item request, log `source=hn_official item_id=... status=skipped`, and continue.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `pytest -q tests/test_hn_client.py`

Expected: all HN client tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/hn_client.py tests/test_hn_client.py
git commit -m "Isolate HN hot item failures"
```

### Task 5: Safe selected-article extraction and grounded summaries

**Files:**
- Create: `src/daily_brief/article_fetcher.py`
- Create: `tests/test_article_fetcher.py`
- Modify: `src/daily_brief/summarizer.py`
- Modify: `tests/test_summarizer.py`

- [ ] **Step 1: Write failing fetch and prompt tests**

Test public HTTP/HTTPS validation with an injected resolver, rejection of loopback/private/reserved addresses and unsafe redirects, content-type and byte limits, HTML script/style removal and whitespace collapse, plain text decoding, and grounded prompt wording. Assert summary prompts omit points/comments and the fallback says a reliable summary is unavailable.

```python
def test_extract_html_omits_script_and_collapses_text():
    assert extract_html("<article>Hello <b>world</b></article><script>bad()</script>") == "Hello world"


def test_prompt_requires_grounded_facts_and_omits_hn_heat(monkeypatch):
    CodexSummarizer().summarize(candidate(story_text="Explicit source fact."))
    assert "Explicit source fact." in captured_prompt
    assert "不要推断" in captured_prompt
    assert "Points:" not in captured_prompt
    assert "Comments:" not in captured_prompt
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `pytest -q tests/test_article_fetcher.py tests/test_summarizer.py`

Expected: article module import failure and old prompt/fallback assertions fail.

- [ ] **Step 3: Implement bounded article fetching**

Implement `fetch_article_text(url, opener=None, resolver=socket.getaddrinfo, timeout_seconds=15, max_bytes=262144)`. Validate scheme, hostname, and every resolved address with `ipaddress.ip_address(address).is_global`; use a redirect handler that revalidates destinations; accept only `text/html` and `text/plain`; read at most `max_bytes + 1`; decode using the declared charset or UTF-8; and extract visible HTML with an `HTMLParser` that ignores `script`, `style`, `noscript`, `svg`, and `template` content.

- [ ] **Step 4: Implement the grounded summary prompt**

Keep the existing content precedence. Replace recommendation-oriented instructions with: summarize only facts explicitly present, do not infer causes/outcomes/later events, use only title-level facts when body is unavailable, and do not mention HN popularity. Remove points, comments, and matched keywords from the prompt. Change fallback text to `未能生成可靠摘要，请查看原文或讨论。`.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run: `pytest -q tests/test_article_fetcher.py tests/test_summarizer.py`

Expected: all article and summarizer tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/daily_brief/article_fetcher.py src/daily_brief/summarizer.py tests/test_article_fetcher.py tests/test_summarizer.py
git commit -m "Ground summaries in selected article text"
```

### Task 6: Integrate routing, history, and enrichment

**Files:**
- Modify: `src/daily_brief/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing end-to-end tests**

Add injectable fake classifier and article fetcher dependencies. Cover a zero-keyword Qwen candidate being promoted to AI, classifier failure preserving keyword-selected AI, a prior-date selected ID receiving `recently_selected`, same-date reruns selecting the same item, selected external text reaching the summarizer, fetch failure leaving brief generation intact, and classifier-only AI using `topic classifier: AI` for `Why`.

```python
def test_classifier_promotes_unmatched_hot_story_to_ai(tmp_path):
    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-20",
        algolia_stories=[story("1", "Unseen Model Launch", points=750, comments=500)],
        hot_stories=[],
        classifier=FakeClassifier({"1"}),
        article_fetcher=lambda url: "",
        summarizer=FakeSummarizer(),
    )
    assert "Unseen Model Launch" in result.brief_path.read_text()
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `pytest -q tests/test_cli.py`

Expected: `run_generate` lacks classifier/article dependencies and history behavior.

- [ ] **Step 3: Refactor candidate routing**

Score candidates from both sources, deduplicate once, mark recent IDs before routing, and classify at most 30 eligible weak/unmatched candidates sorted by `(points, comments)`. Route known and classifier-returned candidates to AI; route remaining candidates to non-AI. Set classifier-routed reasons explicitly. Catch classifier errors and log `component=topic_classifier status=failed`.

- [ ] **Step 4: Enrich only selected candidates**

Before summarization, fetch external text only when `story_text` is empty and source URL differs from the HN discussion URL. Use `dataclasses.replace` to set frozen `Story.fetched_text`. Catch and log each article failure without changing selection.

- [ ] **Step 5: Integrate history**

Load `data_dir/recommendation-history.json`, exclude only prior-seven-date IDs, preserve excluded candidates in the diagnostic snapshot with `recently_selected`, write brief and dated snapshot, then atomically save selected IDs for the generated date.

- [ ] **Step 6: Update existing CLI test dependencies**

Every existing `run_generate` test supplies `FakeClassifier(set())` and a no-network article fetcher so tests remain deterministic. Update the one official OpenAI routing expectation because all deduplicated sources now participate in AI classification.

- [ ] **Step 7: Run CLI tests and verify GREEN**

Run: `pytest -q tests/test_cli.py`

Expected: all CLI tests pass without network or Codex.

- [ ] **Step 8: Commit**

```bash
git add src/daily_brief/cli.py tests/test_cli.py
git commit -m "Integrate quality and novelty pipeline"
```

### Task 7: Full verification and replay checks

**Files:**
- Modify only if verification reveals a requirement mismatch.

- [ ] **Step 1: Run the full deterministic suite**

Run: `pytest -q`

Expected: all tests pass; no test calls live HN, arbitrary articles, or Codex.

- [ ] **Step 2: Check formatting and repository scope**

Run: `git diff --check HEAD~6..HEAD && git status --short`

Expected: no whitespace errors; only intentional implementation-plan state remains uncommitted if it was not committed earlier.

- [ ] **Step 3: Review historical-output preservation**

Run: `git status --short -- briefs data`

Expected: no tracked or untracked historical output changes inside the worktree.

- [ ] **Step 4: Commit the implementation plan if still uncommitted**

```bash
git add docs/2026-07-20-quality-pipeline-implementation-plan.md
git commit -m "Document quality pipeline implementation plan"
```
