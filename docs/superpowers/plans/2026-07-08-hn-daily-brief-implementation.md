# HN Daily Brief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local Python CLI that generates a Hacker News daily brief Markdown file and a raw candidate JSON snapshot from the approved v1 design.

**Architecture:** Use a small Python package under `src/daily_brief/` with focused modules for models, keyword matching, scoring/selection, HN clients, summarization, rendering, and CLI orchestration. Runtime dependencies stay at zero by using Python 3.12 standard library; tests use `pytest` and fake clients/summarizers so normal test runs never call the network or `codex exec`.

**Tech Stack:** Python 3.12, standard library (`urllib.request`, `json`, `datetime`, `zoneinfo`, `subprocess`, `argparse`, `html.parser`), pytest, local `codex exec` for real summary generation.

---

## File Structure

- `pyproject.toml`: package metadata, pytest config, console script entrypoint.
- `README.md`: add local setup and run commands in Chinese.
- `src/daily_brief/__init__.py`: package marker and version.
- `src/daily_brief/__main__.py`: support `python -m daily_brief`.
- `src/daily_brief/models.py`: dataclasses for stories, keyword matches, selection records, brief items, and run outputs.
- `src/daily_brief/config.py`: constants for keywords, scoring weights, thresholds, output paths, and timezone.
- `src/daily_brief/time_window.py`: Asia/Singapore daily window calculation.
- `src/daily_brief/keywords.py`: tokenization, boundary matching, longest-match-wins, URL weak-signal handling.
- `src/daily_brief/scoring.py`: score calculation and AI relevance metadata.
- `src/daily_brief/selection.py`: deduplication, AI item selection, Non-AI hot selection, rejection reasons.
- `src/daily_brief/hn_client.py`: Algolia and HN official API clients using `urllib.request`.
- `src/daily_brief/summarizer.py`: `codex exec` summarizer and deterministic fallback summary behavior.
- `src/daily_brief/render.py`: Markdown and JSON snapshot rendering.
- `src/daily_brief/cli.py`: command-line orchestration and filesystem writes.
- `tests/test_time_window.py`: timezone/window tests.
- `tests/test_keywords.py`: keyword matching and scoring token behavior tests.
- `tests/test_scoring_selection.py`: scoring, deduplication, thresholds, and section selection tests.
- `tests/test_render.py`: Markdown and JSON output tests.
- `tests/test_cli.py`: end-to-end orchestration with fake clients and fake summarizer.

## Task 1: Project Skeleton And CLI Entry Point

**Files:**
- Create: `pyproject.toml`
- Create: `src/daily_brief/__init__.py`
- Create: `src/daily_brief/__main__.py`
- Create: `src/daily_brief/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write a failing CLI smoke test**

Create `tests/test_cli.py`:

```python
from daily_brief.cli import build_parser


def test_parser_defaults_to_generate_command():
    parser = build_parser()

    args = parser.parse_args([])

    assert args.command == "generate"
    assert args.output_dir == "briefs"
    assert args.data_dir == "data"
    assert args.dry_run is False
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
pytest tests/test_cli.py::test_parser_defaults_to_generate_command -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'daily_brief'`.

- [ ] **Step 3: Add package metadata and minimal CLI**

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "daily-brief"
version = "0.1.0"
description = "Personal daily information brief generator"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
daily-brief = "daily_brief.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

Create `src/daily_brief/__init__.py`:

```python
__version__ = "0.1.0"
```

Create `src/daily_brief/__main__.py`:

```python
from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `src/daily_brief/cli.py`:

```python
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-brief")
    parser.add_argument(
        "command",
        nargs="?",
        default="generate",
        choices=["generate"],
        help="Command to run.",
    )
    parser.add_argument("--output-dir", default="briefs")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    return 0
```

- [ ] **Step 4: Run the CLI smoke test**

Run:

```bash
pytest tests/test_cli.py::test_parser_defaults_to_generate_command -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/daily_brief tests/test_cli.py
git commit -m "Add Python project skeleton"
```

## Task 2: Time Window Calculation

**Files:**
- Create: `src/daily_brief/time_window.py`
- Modify: `src/daily_brief/config.py`
- Test: `tests/test_time_window.py`

- [ ] **Step 1: Write failing time window tests**

Create `tests/test_time_window.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_brief.time_window import daily_window


def test_daily_window_uses_singapore_8am_boundary():
    now = datetime(2026, 7, 8, 9, 15, tzinfo=ZoneInfo("Asia/Singapore"))

    window = daily_window(now)

    assert window.date_label == "2026-07-08"
    assert window.start == datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert window.end == datetime(2026, 7, 8, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))


def test_daily_window_before_8am_uses_previous_label():
    now = datetime(2026, 7, 8, 7, 59, tzinfo=ZoneInfo("Asia/Singapore"))

    window = daily_window(now)

    assert window.date_label == "2026-07-07"
    assert window.start == datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert window.end == datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
```

- [ ] **Step 2: Run failing tests**

Run:

```bash
pytest tests/test_time_window.py -q
```

Expected: FAIL with `ModuleNotFoundError` or missing `daily_window`.

- [ ] **Step 3: Implement time window**

Create `src/daily_brief/config.py`:

```python
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Singapore")
RUN_HOUR = 8
```

Create `src/daily_brief/time_window.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import RUN_HOUR, TIMEZONE


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    date_label: str


def daily_window(now: datetime | None = None) -> TimeWindow:
    current = now or datetime.now(TIMEZONE)
    current = current.astimezone(TIMEZONE)
    today_boundary = datetime.combine(current.date(), time(RUN_HOUR), tzinfo=TIMEZONE)

    if current >= today_boundary:
        end = today_boundary
    else:
        end = today_boundary - timedelta(days=1)

    start = end - timedelta(days=1)
    return TimeWindow(start=start, end=end, date_label=end.date().isoformat())
```

- [ ] **Step 4: Run time window tests**

Run:

```bash
pytest tests/test_time_window.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/config.py src/daily_brief/time_window.py tests/test_time_window.py
git commit -m "Add daily time window calculation"
```

## Task 3: Story Models And Keyword Matching

**Files:**
- Create: `src/daily_brief/models.py`
- Create: `src/daily_brief/keywords.py`
- Modify: `src/daily_brief/config.py`
- Test: `tests/test_keywords.py`

- [ ] **Step 1: Write failing keyword tests**

Create `tests/test_keywords.py`:

```python
from daily_brief.keywords import match_keywords


def names(matches):
    return [match.keyword for match in matches]


def test_keyword_matching_uses_boundaries():
    text = "Medieval-style fortifications and storage systems"

    matches = match_keywords(title=text, story_text="", url="")

    assert "eval" not in names(matches)
    assert "RAG" not in names(matches)


def test_abbreviations_require_uppercase_standalone_tokens():
    assert "AI" in names(match_keywords(title="How AI is reshaping education", story_text="", url=""))
    assert "AI" not in names(match_keywords(title="How ai is reshaping education", story_text="", url=""))
    assert "RAG" not in names(match_keywords(title="average storage wins", story_text="", url=""))


def test_longest_match_wins_avoids_repeated_short_token_scores():
    matches = match_keywords(title="AI coding agent built with Claude", story_text="", url="")

    assert "AI coding" in names(matches)
    assert "coding agent" in names(matches)
    assert "Claude" in names(matches)
    assert "AI" not in names(matches)
    assert "agent" not in names(matches)


def test_url_tokens_are_weak_only():
    matches = match_keywords(title="A normal release note", story_text="", url="https://example.com/developer/tools")

    assert [(match.keyword, match.weight) for match in matches] == [("developer tools", "weak")]
```

- [ ] **Step 2: Run failing keyword tests**

Run:

```bash
pytest tests/test_keywords.py -q
```

Expected: FAIL with missing module or functions.

- [ ] **Step 3: Implement models and keyword matching**

Create `src/daily_brief/models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Story:
    source: str
    hn_item_id: str
    title: str
    source_url: str
    hn_discussion_url: str
    created_at: str
    points: int
    comments: int
    story_text: str = ""
    fetched_text: str = ""


@dataclass(frozen=True)
class KeywordMatch:
    keyword: str
    weight: str
    source: str
    bonus: float
    start: int
    end: int


@dataclass
class Candidate:
    story: Story
    matched_keywords: list[KeywordMatch] = field(default_factory=list)
    score: float = 0.0
    selected: bool = False
    section: str = ""
    rejection_reason: str = ""
    summary: str = ""
    why: str = ""
```

Add to `src/daily_brief/config.py`:

```python
HIGH_WEIGHT_KEYWORDS = [
    "AI coding",
    "coding agent",
    "AI agent",
    "LLM",
    "Claude",
    "OpenAI",
    "Anthropic",
    "ChatGPT",
    "Cursor",
    "Copilot",
    "MCP",
    "RAG",
    "AI developer tools",
]

MEDIUM_HIGH_WEIGHT_KEYWORDS = [
    "Gemini",
    "Google AI",
    "Meta AI",
    "xAI",
    "Mistral",
    "Perplexity",
    "AI workflow",
    "AI productivity",
    "assistant",
    "chatbot",
    "AI app",
    "AI tool",
    "AI automation",
]

MEDIUM_WEIGHT_KEYWORDS = [
    "AI",
    "inference",
    "fine-tuning",
    "eval",
    "AI benchmark",
    "LLM benchmark",
    "GPU",
    "embedding",
    "vector database",
]

WEAK_KEYWORDS = [
    "agent",
    "agents",
    "model",
    "workflow",
    "automation",
    "productivity",
    "training",
    "benchmark",
    "developer tools",
]

LOW_WEIGHT_KEYWORDS = [
    "funding",
    "acquisition",
    "regulation",
    "lawsuit",
]

ABBREVIATIONS = {"AI", "LLM", "RAG", "MCP", "GPU"}
```

Create `src/daily_brief/keywords.py`:

```python
from __future__ import annotations

import re
from urllib.parse import urlparse

from .config import (
    ABBREVIATIONS,
    HIGH_WEIGHT_KEYWORDS,
    LOW_WEIGHT_KEYWORDS,
    MEDIUM_HIGH_WEIGHT_KEYWORDS,
    MEDIUM_WEIGHT_KEYWORDS,
    WEAK_KEYWORDS,
)
from .models import KeywordMatch

WEIGHT_BONUS = {
    "high": 4.0,
    "medium_high": 2.5,
    "medium": 1.5,
    "low": 0.5,
    "weak": 0.0,
}


def match_keywords(title: str, story_text: str, url: str) -> list[KeywordMatch]:
    primary_text = "\n".join(part for part in [title, story_text] if part)
    matches = _match_primary_text(primary_text)
    matches.extend(_match_url_tokens(url))
    return matches


def _match_primary_text(text: str) -> list[KeywordMatch]:
    specs = []
    for weight, keywords in [
        ("high", HIGH_WEIGHT_KEYWORDS),
        ("medium_high", MEDIUM_HIGH_WEIGHT_KEYWORDS),
        ("medium", MEDIUM_WEIGHT_KEYWORDS),
        ("low", LOW_WEIGHT_KEYWORDS),
        ("weak", WEAK_KEYWORDS),
    ]:
        for keyword in keywords:
            specs.append((keyword, weight))

    specs.sort(key=lambda item: len(item[0]), reverse=True)
    occupied: list[tuple[int, int]] = []
    matches: list[KeywordMatch] = []

    for keyword, weight in specs:
        for match in _iter_keyword_matches(text, keyword):
            span = match.span()
            if _overlaps(span, occupied):
                continue
            occupied.append(span)
            matches.append(
                KeywordMatch(
                    keyword=keyword,
                    weight=weight,
                    source="text",
                    bonus=WEIGHT_BONUS[weight],
                    start=span[0],
                    end=span[1],
                )
            )

    return sorted(matches, key=lambda match: match.start)


def _iter_keyword_matches(text: str, keyword: str):
    if keyword in ABBREVIATIONS:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])"
        return re.finditer(pattern, text)

    flags = re.IGNORECASE
    pattern = rf"(?<![A-Za-z0-9]){re.escape(keyword).replace(r'\ ', r'\s+')}(?![A-Za-z0-9])"
    return re.finditer(pattern, text, flags)


def _match_url_tokens(url: str) -> list[KeywordMatch]:
    if not url:
        return []

    parsed = urlparse(url)
    token_text = " ".join(part for part in [parsed.hostname or "", parsed.path.replace("/", " ").replace("-", " ")] if part)
    matches: list[KeywordMatch] = []
    for keyword in ["AI", *WEAK_KEYWORDS]:
        for match in _iter_keyword_matches(token_text, keyword):
            matches.append(
                KeywordMatch(
                    keyword=keyword,
                    weight="weak",
                    source="url",
                    bonus=0.0,
                    start=match.start(),
                    end=match.end(),
                )
            )
            return matches
    return matches


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < used_end and end > used_start for used_start, used_end in occupied)
```

- [ ] **Step 4: Run keyword tests**

Run:

```bash
pytest tests/test_keywords.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/config.py src/daily_brief/models.py src/daily_brief/keywords.py tests/test_keywords.py
git commit -m "Add keyword matching rules"
```

## Task 4: Scoring, Deduplication, And Selection

**Files:**
- Create: `src/daily_brief/scoring.py`
- Create: `src/daily_brief/selection.py`
- Modify: `src/daily_brief/config.py`
- Test: `tests/test_scoring_selection.py`

- [ ] **Step 1: Write failing scoring and selection tests**

Create `tests/test_scoring_selection.py`:

```python
from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.scoring import score_candidate
from daily_brief.selection import dedupe_candidates, select_sections


def story(item_id, title, points=50, comments=10, url=None):
    return Story(
        source="test",
        hn_item_id=str(item_id),
        title=title,
        source_url=url or f"https://example.com/{item_id}",
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at="2026-07-08T00:00:00+08:00",
        points=points,
        comments=comments,
    )


def match(keyword, weight, bonus):
    return KeywordMatch(keyword=keyword, weight=weight, source="text", bonus=bonus, start=0, end=len(keyword))


def test_score_uses_log_heat_and_bonus_caps():
    candidate = Candidate(
        story=story(1, "AI coding with Claude", points=100, comments=20),
        matched_keywords=[
            match("AI coding", "high", 4.0),
            match("Claude", "high", 4.0),
            match("AI agent", "high", 4.0),
        ],
    )

    scored = score_candidate(candidate)

    assert round(scored.score, 2) == 16.14
    assert scored.why == "keywords: AI coding, Claude, AI agent"


def test_select_ai_requires_score_and_minimum_points():
    low_heat = score_candidate(Candidate(story=story(1, "AI agent", points=0, comments=0), matched_keywords=[match("AI agent", "high", 4.0)]))
    enough_heat = score_candidate(Candidate(story=story(2, "AI agent", points=10, comments=0), matched_keywords=[match("AI agent", "high", 4.0)]))

    ai_items, hot_items = select_sections([low_heat, enough_heat], [])

    assert [item.story.hn_item_id for item in ai_items] == ["2"]
    assert hot_items == []
    assert low_heat.rejection_reason == "below_ai_minimum"


def test_non_ai_hot_uses_threshold_or_fallback():
    hot = Candidate(story=story(1, "SQLite release", points=320, comments=12))
    fallback = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [fallback, hot])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].section == "non_ai_hot"


def test_non_ai_hot_fallback_selects_one_when_no_threshold_met():
    first = Candidate(story=story(1, "SQLite release", points=120, comments=12))
    second = Candidate(story=story(2, "Compiler notes", points=90, comments=10))

    ai_items, hot_items = select_sections([], [second, first])

    assert ai_items == []
    assert [item.story.hn_item_id for item in hot_items] == ["1"]
    assert hot_items[0].why == "today's hottest non-AI story"


def test_dedupe_prefers_ai_candidate_by_hn_item_id():
    ai_candidate = Candidate(story=story(1, "AI story"), section="ai")
    hot_candidate = Candidate(story=story(1, "AI story"), section="non_ai_hot")

    deduped = dedupe_candidates([hot_candidate, ai_candidate])

    assert len(deduped) == 1
    assert deduped[0].section == "ai"
```

- [ ] **Step 2: Run failing scoring and selection tests**

Run:

```bash
pytest tests/test_scoring_selection.py -q
```

Expected: FAIL with missing `scoring` and `selection` modules.

- [ ] **Step 3: Implement scoring and selection**

Append to `src/daily_brief/config.py`:

```python
AI_MAX_ITEMS = 5
AI_MIN_SCORE = 6.0
AI_MIN_POINTS = 10
NON_AI_MAX_ITEMS = 2
NON_AI_POINTS_THRESHOLD = 300
NON_AI_COMMENTS_THRESHOLD = 150
HIGH_WEIGHT_BONUS_CAP = 6.0
MEDIUM_HIGH_WEIGHT_BONUS_CAP = 5.0
MEDIUM_WEIGHT_BONUS_CAP = 3.0
LOW_WEIGHT_BONUS_CAP = 1.0
KEYWORD_BONUS_CAP = 10.0
TOPIC_BONUS_CAP = 4.0
TOPIC_KEYWORDS = {
    "AI coding",
    "coding agent",
    "AI agent",
    "AI developer tools",
    "AI workflow",
    "AI productivity",
    "AI automation",
}
```

Create `src/daily_brief/scoring.py`:

```python
from __future__ import annotations

import math

from .config import (
    HIGH_WEIGHT_BONUS_CAP,
    KEYWORD_BONUS_CAP,
    LOW_WEIGHT_BONUS_CAP,
    MEDIUM_HIGH_WEIGHT_BONUS_CAP,
    MEDIUM_WEIGHT_BONUS_CAP,
    TOPIC_BONUS_CAP,
    TOPIC_KEYWORDS,
)
from .models import Candidate

LAYER_CAPS = {
    "high": HIGH_WEIGHT_BONUS_CAP,
    "medium_high": MEDIUM_HIGH_WEIGHT_BONUS_CAP,
    "medium": MEDIUM_WEIGHT_BONUS_CAP,
    "low": LOW_WEIGHT_BONUS_CAP,
}


def score_candidate(candidate: Candidate) -> Candidate:
    heat = math.log(candidate.story.points + 1) + 0.5 * math.log(candidate.story.comments + 1)
    keyword_bonus = min(_keyword_bonus(candidate), KEYWORD_BONUS_CAP)
    topic_bonus = min(sum(2.0 for match in candidate.matched_keywords if match.keyword in TOPIC_KEYWORDS), TOPIC_BONUS_CAP)
    candidate.score = heat + keyword_bonus + topic_bonus
    candidate.why = _why(candidate)
    return candidate


def _keyword_bonus(candidate: Candidate) -> float:
    total = 0.0
    for layer, cap in LAYER_CAPS.items():
        layer_total = sum(match.bonus for match in candidate.matched_keywords if match.weight == layer)
        total += min(layer_total, cap)
    return total


def _why(candidate: Candidate) -> str:
    if not candidate.matched_keywords:
        return ""
    keywords = ", ".join(match.keyword for match in candidate.matched_keywords[:5])
    return f"keywords: {keywords}"
```

Create `src/daily_brief/selection.py`:

```python
from __future__ import annotations

from .config import (
    AI_MAX_ITEMS,
    AI_MIN_POINTS,
    AI_MIN_SCORE,
    NON_AI_COMMENTS_THRESHOLD,
    NON_AI_MAX_ITEMS,
    NON_AI_POINTS_THRESHOLD,
)
from .models import Candidate


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    by_key: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.story.hn_item_id or candidate.story.source_url
        existing = by_key.get(key)
        if existing is None or _priority(candidate) > _priority(existing):
            by_key[key] = candidate
    return list(by_key.values())


def select_sections(ai_candidates: list[Candidate], non_ai_candidates: list[Candidate]) -> tuple[list[Candidate], list[Candidate]]:
    ai_items = _select_ai(ai_candidates)
    ai_keys = {item.story.hn_item_id or item.story.source_url for item in ai_items}
    hot_pool = [candidate for candidate in non_ai_candidates if (candidate.story.hn_item_id or candidate.story.source_url) not in ai_keys]
    hot_items = _select_non_ai_hot(hot_pool)
    return ai_items, hot_items


def _select_ai(candidates: list[Candidate]) -> list[Candidate]:
    selected: list[Candidate] = []
    for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
        if candidate.score < AI_MIN_SCORE or candidate.story.points < AI_MIN_POINTS:
            candidate.selected = False
            candidate.section = ""
            candidate.rejection_reason = "below_ai_minimum"
            continue
        candidate.selected = True
        candidate.section = "ai"
        candidate.rejection_reason = ""
        selected.append(candidate)
        if len(selected) == AI_MAX_ITEMS:
            break
    return selected


def _select_non_ai_hot(candidates: list[Candidate]) -> list[Candidate]:
    sorted_candidates = sorted(candidates, key=lambda item: (item.story.points, item.story.comments), reverse=True)
    over_threshold = [
        candidate
        for candidate in sorted_candidates
        if candidate.story.points >= NON_AI_POINTS_THRESHOLD or candidate.story.comments >= NON_AI_COMMENTS_THRESHOLD
    ]
    selected = over_threshold[:NON_AI_MAX_ITEMS] if over_threshold else sorted_candidates[:1]
    for candidate in selected:
        candidate.selected = True
        candidate.section = "non_ai_hot"
        candidate.rejection_reason = ""
        candidate.why = "HN-wide hot story" if over_threshold else "today's hottest non-AI story"
    for candidate in sorted_candidates:
        if candidate not in selected:
            candidate.selected = False
            candidate.rejection_reason = "not_selected"
    return selected


def _priority(candidate: Candidate) -> int:
    if candidate.section == "ai" or candidate.matched_keywords:
        return 2
    if candidate.section == "non_ai_hot":
        return 1
    return 0
```

- [ ] **Step 4: Run scoring and selection tests**

Run:

```bash
pytest tests/test_scoring_selection.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/config.py src/daily_brief/scoring.py src/daily_brief/selection.py tests/test_scoring_selection.py
git commit -m "Add HN story scoring and selection"
```

## Task 5: HN API Clients

**Files:**
- Create: `src/daily_brief/hn_client.py`
- Test: extend `tests/test_cli.py` or create `tests/test_hn_client.py`

- [ ] **Step 1: Write failing client parsing tests**

Create `tests/test_hn_client.py`:

```python
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_brief.hn_client import parse_algolia_hit, parse_hn_item


def test_parse_algolia_hit_to_story():
    hit = {
        "objectID": "123",
        "title": "AI coding agent",
        "url": "https://example.com/post",
        "created_at": "2026-07-08T00:00:00Z",
        "points": 42,
        "num_comments": 7,
        "story_text": "A small demo",
    }

    story = parse_algolia_hit(hit)

    assert story.hn_item_id == "123"
    assert story.title == "AI coding agent"
    assert story.hn_discussion_url == "https://news.ycombinator.com/item?id=123"
    assert story.points == 42
    assert story.comments == 7
    assert story.story_text == "A small demo"


def test_parse_hn_item_to_story():
    item = {
        "id": 456,
        "title": "SQLite release",
        "url": "https://sqlite.org/release",
        "time": 1783500000,
        "score": 320,
        "descendants": 80,
    }

    story = parse_hn_item(item)

    assert story.hn_item_id == "456"
    assert story.source == "hn_official"
    assert story.points == 320
    assert story.comments == 80
    assert story.hn_discussion_url == "https://news.ycombinator.com/item?id=456"
```

- [ ] **Step 2: Run failing client tests**

Run:

```bash
pytest tests/test_hn_client.py -q
```

Expected: FAIL with missing module or functions.

- [ ] **Step 3: Implement HN client parsing and fetching**

Create `src/daily_brief/hn_client.py`:

```python
from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Story
from .time_window import TimeWindow

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search_by_date"
HN_TOPSTORIES_URL = "https://hacker-news.firebaseio.com/v0/topstories.json"
HN_BESTSTORIES_URL = "https://hacker-news.firebaseio.com/v0/beststories.json"
HN_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"


def fetch_algolia_stories(window: TimeWindow, page_size: int = 100) -> list[Story]:
    start_ts = int(window.start.timestamp())
    end_ts = int(window.end.timestamp())
    stories: list[Story] = []
    page = 0

    while True:
        query = urlencode(
            {
                "tags": "story",
                "numericFilters": f"created_at_i>{start_ts},created_at_i<={end_ts}",
                "hitsPerPage": page_size,
                "page": page,
            }
        )
        payload = _get_json(f"{ALGOLIA_URL}?{query}")
        hits = payload.get("hits", [])
        stories.extend(parse_algolia_hit(hit) for hit in hits)
        if page >= payload.get("nbPages", 0) - 1:
            break
        page += 1
    return stories


def fetch_hot_stories(limit_ids: int = 100) -> list[Story]:
    ids = []
    for url in [HN_TOPSTORIES_URL, HN_BESTSTORIES_URL]:
        ids.extend(_get_json(url)[:limit_ids])
    seen: set[int] = set()
    stories: list[Story] = []
    for item_id in ids:
        if item_id in seen:
            continue
        seen.add(item_id)
        item = _get_json(HN_ITEM_URL.format(item_id=item_id))
        if item and item.get("type") == "story":
            stories.append(parse_hn_item(item))
    return stories


def parse_algolia_hit(hit: dict) -> Story:
    item_id = str(hit.get("objectID") or "")
    title = hit.get("title") or hit.get("story_title") or ""
    source_url = hit.get("url") or f"https://news.ycombinator.com/item?id={item_id}"
    return Story(
        source="algolia",
        hn_item_id=item_id,
        title=title,
        source_url=source_url,
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at=hit.get("created_at") or "",
        points=int(hit.get("points") or 0),
        comments=int(hit.get("num_comments") or 0),
        story_text=hit.get("story_text") or "",
    )


def parse_hn_item(item: dict) -> Story:
    item_id = str(item.get("id") or "")
    created = datetime.fromtimestamp(int(item.get("time") or 0), tz=UTC).isoformat()
    return Story(
        source="hn_official",
        hn_item_id=item_id,
        title=item.get("title") or "",
        source_url=item.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
        hn_discussion_url=f"https://news.ycombinator.com/item?id={item_id}",
        created_at=created,
        points=int(item.get("score") or 0),
        comments=int(item.get("descendants") or 0),
        story_text=item.get("text") or "",
    )


def _get_json(url: str):
    request = Request(url, headers={"User-Agent": "daily-brief/0.1"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))
```

- [ ] **Step 4: Run client tests**

Run:

```bash
pytest tests/test_hn_client.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/hn_client.py tests/test_hn_client.py
git commit -m "Add Hacker News API clients"
```

## Task 6: Summary Generation

**Files:**
- Create: `src/daily_brief/summarizer.py`
- Test: `tests/test_summarizer.py`

- [ ] **Step 1: Write failing summarizer tests**

Create `tests/test_summarizer.py`:

```python
from daily_brief.models import Candidate, Story
from daily_brief.summarizer import CodexSummarizer, fallback_summary


def candidate():
    return Candidate(
        story=Story(
            source="test",
            hn_item_id="1",
            title="AI coding agent",
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=30,
            comments=5,
            story_text="A demo of an AI coding agent.",
        )
    )


def test_fallback_summary_uses_title_and_stats():
    text = fallback_summary(candidate())

    assert "AI coding agent" in text
    assert "30 points" in text


def test_codex_summarizer_builds_prompt_and_returns_stdout(monkeypatch):
    calls = {}

    def fake_run(args, input, text, capture_output, timeout, check):
        calls["args"] = args
        calls["input"] = input

        class Result:
            stdout = "中文摘要"
            stderr = ""

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    summary = CodexSummarizer(timeout_seconds=5).summarize(candidate())

    assert summary == "中文摘要"
    assert calls["args"][:3] == ["codex", "exec", "--ephemeral"]
    assert "AI coding agent" in calls["input"]
```

- [ ] **Step 2: Run failing summarizer tests**

Run:

```bash
pytest tests/test_summarizer.py -q
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement summarizer**

Create `src/daily_brief/summarizer.py`:

```python
from __future__ import annotations

import subprocess

from .models import Candidate


class CodexSummarizer:
    def __init__(self, timeout_seconds: int = 90) -> None:
        self.timeout_seconds = timeout_seconds

    def summarize(self, candidate: Candidate) -> str:
        result = subprocess.run(
            ["codex", "exec", "--ephemeral", "Write a concise Chinese summary for this Hacker News item."],
            input=_build_prompt(candidate),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=True,
        )
        return result.stdout.strip()


def fallback_summary(candidate: Candidate) -> str:
    return (
        f"{candidate.story.title}。"
        f"当前 HN 热度为 {candidate.story.points} points / {candidate.story.comments} comments；"
        "摘要生成失败时保留此基础信息。"
    )


def _build_prompt(candidate: Candidate) -> str:
    keywords = ", ".join(match.keyword for match in candidate.matched_keywords) or "none"
    return f"""请用中文写 1-2 句话摘要，说明这条 Hacker News 内容为什么值得看。

Title: {candidate.story.title}
Source URL: {candidate.story.source_url}
HN Discussion: {candidate.story.hn_discussion_url}
Points: {candidate.story.points}
Comments: {candidate.story.comments}
Matched keywords: {keywords}
Story text:
{candidate.story.story_text or candidate.story.fetched_text or "(not available)"}
"""
```

- [ ] **Step 4: Run summarizer tests**

Run:

```bash
pytest tests/test_summarizer.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/summarizer.py tests/test_summarizer.py
git commit -m "Add Codex summary generation"
```

## Task 7: Rendering Markdown And Candidate JSON

**Files:**
- Create: `src/daily_brief/render.py`
- Test: `tests/test_render.py`

- [ ] **Step 1: Write failing render tests**

Create `tests/test_render.py`:

```python
import json

from daily_brief.models import Candidate, KeywordMatch, Story
from daily_brief.render import render_candidates_json, render_markdown


def candidate(section="ai", selected=True):
    item = Candidate(
        story=Story(
            source="algolia",
            hn_item_id="1",
            title="AI coding agent",
            source_url="https://example.com",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=30,
            comments=5,
        ),
        matched_keywords=[KeywordMatch("AI coding", "high", "text", 4.0, 0, 9)],
        score=9.2,
        selected=selected,
        section=section,
        summary="这是一个 AI coding agent 项目。",
        why="keywords: AI coding",
    )
    return item


def test_render_markdown_contains_required_fields():
    markdown = render_markdown("2026-07-08", [candidate()], [])

    assert "# Daily Brief - 2026-07-08" in markdown
    assert "## Hacker News: AI" in markdown
    assert "### AI coding agent" in markdown
    assert "Summary: 这是一个 AI coding agent 项目。" in markdown
    assert "Stats: 30 points / 5 comments" in markdown


def test_render_candidates_json_uses_snake_case_fields():
    data = json.loads(render_candidates_json([candidate(selected=False)]))

    assert data[0]["hn_item_id"] == "1"
    assert data[0]["matched_keywords"] == ["AI coding"]
    assert data[0]["selected"] is False
    assert "rejection_reason" in data[0]
```

- [ ] **Step 2: Run failing render tests**

Run:

```bash
pytest tests/test_render.py -q
```

Expected: FAIL with missing module.

- [ ] **Step 3: Implement rendering**

Create `src/daily_brief/render.py`:

```python
from __future__ import annotations

import json

from .models import Candidate


def render_markdown(date_label: str, ai_items: list[Candidate], hot_items: list[Candidate]) -> str:
    lines = [f"# Daily Brief - {date_label}", ""]
    lines.extend(_render_section("Hacker News: AI", ai_items))
    lines.extend(_render_section("Hacker News: Non-AI Hot", hot_items))
    return "\n".join(lines).rstrip() + "\n"


def render_candidates_json(candidates: list[Candidate]) -> str:
    payload = []
    for candidate in candidates:
        story = candidate.story
        payload.append(
            {
                "source": story.source,
                "hn_item_id": story.hn_item_id,
                "title": story.title,
                "source_url": story.source_url,
                "hn_discussion_url": story.hn_discussion_url,
                "created_at": story.created_at,
                "points": story.points,
                "comments": story.comments,
                "matched_keywords": [match.keyword for match in candidate.matched_keywords],
                "score": round(candidate.score, 4),
                "selected": candidate.selected,
                "section": candidate.section,
                "rejection_reason": candidate.rejection_reason,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _render_section(title: str, items: list[Candidate]) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.extend(["No items selected.", ""])
        return lines

    for item in items:
        story = item.story
        lines.extend(
            [
                f"### {story.title}",
                "",
                f"- Summary: {item.summary}",
                f"- Why: {item.why}",
                f"- Source: {story.source_url}",
                f"- Discussion: {story.hn_discussion_url}",
                f"- Stats: {story.points} points / {story.comments} comments",
                "",
            ]
        )
    return lines
```

- [ ] **Step 4: Run render tests**

Run:

```bash
pytest tests/test_render.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/daily_brief/render.py tests/test_render.py
git commit -m "Add brief rendering"
```

## Task 8: End-To-End CLI Orchestration

**Files:**
- Modify: `src/daily_brief/cli.py`
- Test: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: Add failing end-to-end CLI test with fakes**

Append to `tests/test_cli.py`:

```python
from daily_brief.cli import run_generate
from daily_brief.models import Story


class FakeSummarizer:
    def summarize(self, candidate):
        return f"摘要：{candidate.story.title}"


def test_run_generate_writes_markdown_and_json(tmp_path):
    algolia_stories = [
        Story(
            source="algolia",
            hn_item_id="1",
            title="AI coding agent with Claude",
            source_url="https://example.com/ai",
            hn_discussion_url="https://news.ycombinator.com/item?id=1",
            created_at="2026-07-08T00:00:00+08:00",
            points=40,
            comments=10,
        )
    ]
    hot_stories = [
        Story(
            source="hn_official",
            hn_item_id="2",
            title="SQLite release",
            source_url="https://sqlite.org",
            hn_discussion_url="https://news.ycombinator.com/item?id=2",
            created_at="2026-07-08T00:00:00+08:00",
            points=350,
            comments=20,
        )
    ]

    result = run_generate(
        output_dir=tmp_path / "briefs",
        data_dir=tmp_path / "data",
        date_label="2026-07-08",
        algolia_stories=algolia_stories,
        hot_stories=hot_stories,
        summarizer=FakeSummarizer(),
    )

    assert result.brief_path.read_text(encoding="utf-8").startswith("# Daily Brief - 2026-07-08")
    assert "AI coding agent with Claude" in result.brief_path.read_text(encoding="utf-8")
    assert "SQLite release" in result.brief_path.read_text(encoding="utf-8")
    assert result.data_path.exists()
```

- [ ] **Step 2: Run failing CLI orchestration test**

Run:

```bash
pytest tests/test_cli.py::test_run_generate_writes_markdown_and_json -q
```

Expected: FAIL with missing `run_generate`.

- [ ] **Step 3: Implement orchestration**

Replace `src/daily_brief/cli.py` with:

```python
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from .hn_client import fetch_algolia_stories, fetch_hot_stories
from .keywords import match_keywords
from .models import Candidate, Story
from .render import render_candidates_json, render_markdown
from .scoring import score_candidate
from .selection import dedupe_candidates, select_sections
from .summarizer import CodexSummarizer, fallback_summary
from .time_window import daily_window


@dataclass(frozen=True)
class GenerateResult:
    brief_path: Path
    data_path: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-brief")
    parser.add_argument(
        "command",
        nargs="?",
        default="generate",
        choices=["generate"],
        help="Command to run.",
    )
    parser.add_argument("--output-dir", default="briefs")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def run_generate(
    output_dir: str | Path,
    data_dir: str | Path,
    date_label: str | None = None,
    algolia_stories: list[Story] | None = None,
    hot_stories: list[Story] | None = None,
    summarizer: CodexSummarizer | None = None,
) -> GenerateResult:
    window = daily_window()
    label = date_label or window.date_label
    output_path = Path(output_dir) / f"{label}.md"
    data_path = Path(data_dir) / f"{label}-hn-candidates.json"

    source_ai_stories = algolia_stories if algolia_stories is not None else fetch_algolia_stories(window)
    source_hot_stories = hot_stories if hot_stories is not None else fetch_hot_stories()
    active_summarizer = summarizer or CodexSummarizer()

    ai_candidates = [_candidate_from_story(story) for story in source_ai_stories]
    hot_candidates = [Candidate(story=story) for story in source_hot_stories if not match_keywords(story.title, story.story_text, story.source_url)]
    all_candidates = dedupe_candidates([*ai_candidates, *hot_candidates])
    ai_pool = [candidate for candidate in all_candidates if candidate.matched_keywords]
    hot_pool = [candidate for candidate in all_candidates if not candidate.matched_keywords]
    ai_items, hot_items = select_sections(ai_pool, hot_pool)

    for candidate in [*ai_items, *hot_items]:
        try:
            candidate.summary = active_summarizer.summarize(candidate)
        except Exception:
            candidate.summary = fallback_summary(candidate)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(label, ai_items, hot_items), encoding="utf-8")
    data_path.write_text(render_candidates_json(all_candidates), encoding="utf-8")
    return GenerateResult(brief_path=output_path, data_path=data_path)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        if args.dry_run:
            return 0
        run_generate(output_dir=args.output_dir, data_dir=args.data_dir)
    return 0


def _candidate_from_story(story: Story) -> Candidate:
    matches = match_keywords(story.title, story.story_text, story.source_url)
    candidate = Candidate(story=story, matched_keywords=matches)
    return score_candidate(candidate)
```

- [ ] **Step 4: Run all tests**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 5: Update README**

Modify `README.md` to include this section:

    ## 本地运行

    ```bash
    python -m daily_brief generate
    ```

    默认输出：

    - `briefs/YYYY-MM-DD.md`
    - `data/YYYY-MM-DD-hn-candidates.json`

    ## 测试

    ```bash
    pytest -q
    ```

- [ ] **Step 6: Commit**

```bash
git add README.md src/daily_brief/cli.py tests/test_cli.py
git commit -m "Wire HN brief generation CLI"
```

## Task 9: Real Network Smoke Test

**Files:**
- No source file changes expected unless the smoke test exposes a bug.

- [ ] **Step 1: Run full test suite**

Run:

```bash
pytest -q
```

Expected: PASS.

- [ ] **Step 2: Run CLI dry-run**

Run:

```bash
python -m daily_brief --dry-run
```

Expected: exit code 0 and no files created.

- [ ] **Step 3: Run real generation**

Run:

```bash
python -m daily_brief generate
```

Expected:

- `briefs/YYYY-MM-DD.md` exists for the current Asia/Singapore window.
- `data/YYYY-MM-DD-hn-candidates.json` exists.
- The Markdown contains both `## Hacker News: AI` and `## Hacker News: Non-AI Hot`.
- If `codex exec` fails for any item, the file still contains fallback summaries.

- [ ] **Step 4: Inspect output**

Run:

```bash
ls -la briefs data
sed -n '1,180p' briefs/*.md
python -m json.tool data/*-hn-candidates.json >/dev/null
```

Expected: output files are readable, JSON validates, and Markdown has no obvious broken formatting.

- [ ] **Step 5: Leave generated files untracked by default**

Default: do not commit generated `briefs/` or `data/` files. If the smoke test exposed a source code bug, commit only the source/test fix and leave generated files untracked.

## Self-Review Notes

- Spec coverage: the plan covers the 08:00 Asia/Singapore window, Algolia 24h retrieval, official HN hot retrieval, keyword semantics, longest-match-wins, AI score and heat thresholds, Non-AI fallback behavior, JSON snapshot, Markdown rendering, `codex exec` summaries, and failure fallback.
- Testing coverage: unit tests cover matching, scoring, selection, rendering, time window, summary subprocess behavior, and fake end-to-end generation. The final task adds a real network smoke test.
- Dependency scope: runtime uses only Python 3.12 standard library. `pytest` is test-only.
- Implementation stop line: after this plan is approved, stop debating scoring constants until real JSON snapshots provide evidence.
