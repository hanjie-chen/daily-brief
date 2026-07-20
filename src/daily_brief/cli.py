from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from .article_fetcher import fetch_article_text
from .config import TOPIC_CLASSIFIER_MAX_CANDIDATES
from .history import load_history, recent_ids, save_history
from .hn_client import fetch_algolia_stories, fetch_hot_stories
from .keywords import match_keywords
from .models import Candidate, Story
from .render import render_candidates_json, render_markdown
from .scoring import score_candidate
from .selection import dedupe_candidates, select_sections
from .summarizer import CodexSummarizer, fallback_summary
from .time_window import daily_window
from .topic_classifier import CodexTopicClassifier

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerateResult:
    brief_path: Path
    data_path: Path


def _fetch_source(
    source: str,
    fetch: Callable[[], list[Story]],
    failure_prefix: str,
    clock: Callable[[], float],
) -> tuple[list[Story], str]:
    started = clock()
    try:
        stories = fetch()
    except Exception as exc:
        duration = clock() - started
        LOGGER.error(
            "source=%s status=failed duration=%.3fs error=%s message=%s",
            source,
            duration,
            type(exc).__name__,
            exc,
        )
        return [], f"{failure_prefix} ({exc})."

    duration = clock() - started
    LOGGER.info(
        "source=%s status=success stories=%d duration=%.3fs",
        source,
        len(stories),
        duration,
    )
    return stories, ""


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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate" and not args.dry_run:
        run_generate(output_dir=args.output_dir, data_dir=args.data_dir)
    return 0


def run_generate(
    output_dir,
    data_dir,
    date_label: str | None = None,
    algolia_stories: list[Story] | None = None,
    hot_stories: list[Story] | None = None,
    summarizer=None,
    classifier=None,
    article_fetcher=None,
    clock: Callable[[], float] = time.monotonic,
) -> GenerateResult:
    window = daily_window()
    label = date_label or window.date_label
    ai_note = ""
    hot_note = ""
    if algolia_stories is not None:
        algolia_items = algolia_stories
    else:
        algolia_items, ai_note = _fetch_source(
            "algolia",
            lambda: fetch_algolia_stories(window),
            "Today's AI data source failed: Algolia request failed",
            clock,
        )

    if hot_stories is not None:
        hot_items = hot_stories
    else:
        hot_items, hot_note = _fetch_source(
            "hn_official",
            fetch_hot_stories,
            "Today's HN hot data source failed: HN official API request failed",
            clock,
        )

    candidates = dedupe_candidates([*map(_candidate, algolia_items), *map(_candidate, hot_items)])
    history_path = Path(data_dir) / "recommendation-history.json"
    recommendation_history = load_history(history_path)
    recent_item_ids = recent_ids(recommendation_history, label)
    eligible_candidates: list[Candidate] = []
    for candidate in candidates:
        if candidate.story.hn_item_id and candidate.story.hn_item_id in recent_item_ids:
            candidate.selected = False
            candidate.section = ""
            candidate.rejection_reason = "recently_selected"
        else:
            eligible_candidates.append(candidate)

    known_ai_candidates = [
        candidate for candidate in eligible_candidates if _has_non_weak_keyword_match(candidate)
    ]
    unmatched_candidates = [
        candidate for candidate in eligible_candidates if not _has_non_weak_keyword_match(candidate)
    ]
    classification_batch = sorted(
        unmatched_candidates,
        key=lambda candidate: (candidate.score, candidate.story.points, candidate.story.comments),
        reverse=True,
    )[:TOPIC_CLASSIFIER_MAX_CANDIDATES]
    topic_classifier = classifier or CodexTopicClassifier()
    try:
        classified_ai_ids = topic_classifier.classify(classification_batch)
    except Exception as exc:
        LOGGER.error(
            "component=topic_classifier status=failed error=%s message=%s",
            type(exc).__name__,
            exc,
        )
        classified_ai_ids = set()

    classified_ai_candidates = [
        candidate for candidate in unmatched_candidates if candidate.story.hn_item_id in classified_ai_ids
    ]
    for candidate in classified_ai_candidates:
        candidate.why = "topic classifier: AI"
    classified_ai_identity = {id(candidate) for candidate in classified_ai_candidates}
    ai_pool = [*known_ai_candidates, *classified_ai_candidates]
    hot_pool = [
        candidate for candidate in unmatched_candidates if id(candidate) not in classified_ai_identity
    ]

    ai_items, selected_hot_items = select_sections(ai_pool, hot_pool)
    article_client = article_fetcher or fetch_article_text
    summary_client = summarizer or CodexSummarizer()
    for candidate in [*ai_items, *selected_hot_items]:
        if (
            not candidate.story.story_text.strip()
            and candidate.story.source_url
            and candidate.story.source_url != candidate.story.hn_discussion_url
        ):
            try:
                fetched_text = article_client(candidate.story.source_url).strip()
                if fetched_text:
                    candidate.story = replace(candidate.story, fetched_text=fetched_text)
            except Exception as exc:
                LOGGER.error(
                    "component=article_fetch item_id=%s status=failed error=%s message=%s",
                    candidate.story.hn_item_id,
                    type(exc).__name__,
                    exc,
                )
        try:
            candidate.summary = summary_client.summarize(candidate)
        except Exception as exc:
            print(f"Summary failed for {candidate.story.title}: {exc}", file=sys.stderr)
            candidate.summary = fallback_summary(candidate)

    output_path = Path(output_dir) / f"{label}.md"
    data_path = Path(data_dir) / f"{label}-hn-candidates.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(label, ai_items, selected_hot_items, ai_note=ai_note, hot_note=hot_note),
        encoding="utf-8",
    )
    data_path.write_text(render_candidates_json(candidates), encoding="utf-8")
    try:
        save_history(
            history_path,
            recommendation_history,
            label,
            [candidate.story.hn_item_id for candidate in [*ai_items, *selected_hot_items]],
        )
    except Exception as exc:
        LOGGER.error(
            "component=recommendation_history status=failed error=%s message=%s",
            type(exc).__name__,
            exc,
        )
    LOGGER.info(
        "status=completed ai_items=%d hot_items=%d brief=%s data=%s",
        len(ai_items),
        len(selected_hot_items),
        output_path,
        data_path,
    )
    return GenerateResult(brief_path=output_path, data_path=data_path)


def _candidate(story: Story) -> Candidate:
    return score_candidate(Candidate(story=story, matched_keywords=_keyword_matches(story)))


def _has_non_weak_keyword_match(candidate: Candidate) -> bool:
    return any(match.weight != "weak" for match in candidate.matched_keywords)


def _keyword_matches(story: Story):
    return match_keywords(story.title, story.story_text, story.source_url)
