# Candidates

This package holds the rule-based half of story selection: it collects Hacker
News stories, matches keywords, scores, deduplicates, remembers recent
recommendations, and fills the two sections. None of it calls a model. The
package-level facade is the import used by the rest of the application:

```python
from daily_brief.candidates import fetch_algolia_stories, score_candidate
```

Model-based topic decisions stay in `llm/topic_classifier.py`, and the order in
which rules and model calls are applied stays in `generation/classification.py`.
`hn_client.py` also samples HN discussions for summary fallback; the rules for
when that sample is sufficient stay in `generation/material.py`.

## Candidate Flow

`generation/` applies these rules in order. Each step names the module that
defines the rule.

1. `hn_client.py` collects two required sources. Algolia supplies every story
   created inside the daily window, page by page. The official API supplies the
   leading item IDs from the top and best lists; one failing list or item is
   logged and skipped, but both lists failing fails the source. Every collection
   request retries after the delays in `RETRY_DELAYS_SECONDS`. A story without an
   external URL is a self-post: its `source_url` is its HN discussion URL.
2. `keywords.py` matches the title and self-post text against the weighted
   lists in `config.py` (`HIGH_WEIGHT_KEYWORDS` down to `WEAK_KEYWORDS`).
   Matches respect word boundaries. `ABBREVIATIONS` and
   `CASE_SENSITIVE_KEYWORDS` match case-sensitively, abbreviations also match a
   plural `s`, and `VERSIONED_KEYWORDS` also match a version suffix such as
   `GPT-5`. When matches overlap, the longer keyword wins. The URL hostname and
   path are checked only for `AI` and `WEAK_KEYWORDS`, with the same case rules
   (a lowercase `ai.` hostname does not match), and at most one URL match is
   recorded, always as weak.
3. `scoring.py` sets `score` to HN heat, `ln(points + 1) + 0.5 * ln(comments + 1)`,
   plus a keyword bonus and a topic bonus. The keyword bonus adds each unique
   keyword's `WEIGHT_BONUS`, caps each weight layer at its `*_WEIGHT_BONUS_CAP`,
   then caps the total at `KEYWORD_BONUS_CAP`. Weak matches add nothing. The
   topic bonus counts `TOPIC_KEYWORDS` matches, plus the weak `developer tools`
   when a high or medium-high keyword also matched, up to `TOPIC_BONUS_CAP`. The
   recommendation reason (`why`) lists up to five matched keywords.
4. `selection.dedupe_candidates(...)` treats stories as duplicates when they
   share an HN item ID or an identical `source_url`, including through a chain
   of such links. It keeps the one with a non-weak keyword match, then the higher
   score, points, and comments, then the earlier position.
5. `history.py` excludes item IDs recommended within the previous
   `DEFAULT_HISTORY_DAYS` days. Today's own entry is not an exclusion, so a rerun
   for the same date can select the same stories again.
6. Routing happens in `generation/classification.py`. A non-self-post with a
   non-weak keyword match is a core candidate without a model call. All other
   candidates, including self-posts, are ordered by
   `selection.rank_exploration_candidates(...)` (score, highest first), and at
   most `EXPLORATION_CLASSIFIER_MAX_CANDIDATES` are classified from article
   evidence. A candidate confirmed as core without a non-weak keyword match
   receives `scoring.apply_article_evidence_bonus(...)`. A candidate confirmed as
   outside the core must pass `selection.meets_exploration_minimum(...)`:
   `NON_AI_POINTS_THRESHOLD` points or `NON_AI_COMMENTS_THRESHOLD` comments.
7. `selection.select_ai_candidates(...)` fills the core section (Tech picks,
   public key `ai`). It deduplicates the pool again, preferring candidates that
   meet the minimum, then takes candidates by score. Each must have at least
   `AI_MIN_SCORE` score and `AI_MIN_POINTS` points, up to `AI_MAX_ITEMS`.
8. `selection.select_exploration_candidates(...)` fills the outside section
   (public key `non_ai_hot`) by points and then comments, not by score, up to
   `NON_AI_MAX_ITEMS`.
9. After the brief is written, `history.save_history(...)` records today's
   selected IDs and drops entries older than `DEFAULT_HISTORY_DAYS`. An
   unreadable or malformed history file is logged and treated as empty; a
   failed save is logged and does not fail the run.

## Where the Numbers Live

Rules above name constants rather than values; change the constant, not this
guide.

| Constants | File |
| --- | --- |
| Keyword lists, `ABBREVIATIONS`, `CASE_SENSITIVE_KEYWORDS`, `TOPIC_KEYWORDS` | `config.py` |
| Bonus caps, `ARTICLE_EVIDENCE_BONUS`, section sizes and minimums, `EXPLORATION_CLASSIFIER_MAX_CANDIDATES` | `config.py` |
| `WEIGHT_BONUS`, `VERSIONED_KEYWORDS` | `candidates/keywords.py` |
| `DEFAULT_HISTORY_DAYS` | `candidates/history.py` |
| Request timeouts and retries, discussion sampling bounds (`MAX_DISCUSSION_*`) | `candidates/hn_client.py` |

## Audit Reasons

These values appear in the private candidate audit (`rejection_reason`):

| Value | Set by | Meaning |
| --- | --- | --- |
| `recently_selected` | `generation/pipeline.py` | Excluded by recommendation history |
| `not_selected` | `selection.py` | Lost deduplication, was not classified, or missed a section quota |
| `below_ai_minimum` | `selection.py` | Keyword-routed core candidate below `AI_MIN_SCORE` or `AI_MIN_POINTS` |
| `below_core_minimum` | `selection.py` | Article-confirmed core candidate below the same minimums |
| `core_not_selected` | `selection.py` | Article-confirmed core candidate that met the minimums but missed the quota |
| `below_exploration_minimum` | `generation/classification.py` | Outside candidate below the exploration minimum |

## Discussion Sampling

`hn_client.fetch_hn_discussion(...)` reads a discussion breadth-first, so
top-level answers come first. It stops at the comment, request, depth, or
character limits in `MAX_DISCUSSION_*`, or after `MAX_DISCUSSION_FAILED_ITEMS`
failed requests. Discussion requests use `DISCUSSION_REQUEST_TIMEOUT_SECONDS`
and are not retried. Dead and deleted comments are skipped, and each comment is
labeled with its position, depth, and author.

## Keyword Evaluation

`keyword_evaluation.py` checks keyword changes against real stories without
running the pipeline:

```sh
python scripts/evaluate_keywords.py collect --first-brief-date YYYY-MM-DD --last-brief-date YYYY-MM-DD --output corpus.json
python scripts/evaluate_keywords.py evaluate --corpus corpus.json --output report.json
```

`collect` fetches the Algolia stories for the given range of daily windows.
`evaluate` replays them through the production `match_keywords(...)` and reports
per-keyword counts and every matching story.

## Module Map

| File | Responsibility |
| --- | --- |
| `__init__.py` | Stable package facade and supported imports |
| `hn_client.py` | Algolia and official-API collection, and bounded discussion sampling |
| `keywords.py` | Weighted keyword and URL-token matching |
| `scoring.py` | Heat, keyword and topic bonuses, article evidence bonus, and `why` text |
| `selection.py` | Deduplication, exploration ranking and minimum, and section selection |
| `history.py` | Recent recommendation history |
| `keyword_evaluation.py` | Offline keyword corpus collection and replay |

## Dependency Direction

```text
__init__           -> hn_client, keywords, scoring, selection, history
keyword_evaluation -> hn_client, keywords
other modules      -> config / models / time_window (history imports none)
```

Modules in this package do not import `generation/`, `llm/`, or any network
client other than `hn_client.py`. Routing decisions that need model output
belong in `generation/classification.py`, not here.

## Tests

- `tests/test_hn_client.py`: collection, retries, and discussion sampling.
- `tests/test_keywords.py`: keyword matching.
- `tests/test_scoring_selection.py`: scoring, deduplication, and selection.
- `tests/test_history.py`: recommendation history.
- `tests/test_keyword_evaluation.py`: corpus collection and replay.

Tests inject fake HTTP responses and never call live Hacker News APIs.
