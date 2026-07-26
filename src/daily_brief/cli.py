from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .article_fetcher import fetch_article_text
from .config import TIMEZONE, TOPIC_CLASSIFIER_MAX_CANDIDATES
from .gemini_backend import GeminiBackend
from .history import load_history, recent_ids, save_history
from .hn_client import fetch_algolia_stories, fetch_hot_stories
from .keywords import match_keywords
from .model_backend import CodexBackend, ModelBackend
from .model_evaluation import (
    ModelEvaluationInputError,
    capture_model_evaluation_input,
    run_model_evaluation,
)
from .models import Candidate, Story
from .publisher import PublishError, publish_pending
from .render import render_candidates_json, render_markdown, render_public_brief_json
from .scoring import score_candidate
from .selection import dedupe_candidates, select_sections
from .summarizer import fallback_summary
from .time_window import daily_window

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerateResult:
    brief_path: Path
    data_path: Path
    public_json_path: Path
    model_input_path: Path | None = None


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
        choices=["generate", "publish", "evaluate-model"],
        help="Command to run.",
    )
    parser.add_argument("--output-dir", default="briefs")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--date")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--capture-model-inputs", action="store_true")
    parser.add_argument("--backend", choices=["codex", "gemini"], default="codex")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "evaluate-model" and args.backend != "codex":
        parser.error("--backend gemini is currently limited to evaluate-model")
    if args.dry_run:
        return 0
    if args.command == "generate":
        run_generate(
            output_dir=args.output_dir,
            data_dir=args.data_dir,
            capture_model_inputs=args.capture_model_inputs,
        )
    elif args.command == "publish":
        try:
            result = publish_pending(
                brief_dir=args.output_dir,
                data_dir=args.data_dir,
                date_label=args.date,
                force=args.force,
            )
        except PublishError as exc:
            LOGGER.error("component=publisher status=failed message=%s", exc)
            return 1
        LOGGER.info(
            "component=publisher status=completed published=%d skipped=%d",
            result.published,
            result.skipped,
        )
    elif args.command == "evaluate-model":
        if not args.date:
            parser.error("evaluate-model requires --date YYYY-MM-DD")
        input_path = Path(args.data_dir) / "model-eval-inputs" / f"{args.date}.json"
        try:
            result = run_model_evaluation(
                input_path,
                Path(args.data_dir) / "model-evaluations",
                _model_backend(args.backend),
            )
        except (ModelEvaluationInputError, OSError, ValueError) as exc:
            LOGGER.error("component=model_evaluation status=failed message=%s", exc)
            return 1
        LOGGER.info(
            "component=model_evaluation status=completed backend=%s failures=%d output=%s",
            args.backend,
            result.failures,
            result.output_path,
        )
        return 1 if result.failures else 0
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
    generated_at: str | None = None,
    model_backend: ModelBackend | None = None,
    capture_model_inputs: bool = False,
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

    candidates = dedupe_candidates(
        [*map(_candidate, algolia_items), *map(_candidate, hot_items)]
    )
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
        candidate
        for candidate in eligible_candidates
        if _has_non_weak_keyword_match(candidate)
    ]
    for candidate in known_ai_candidates:
        candidate.topic_route = "keyword"
    unmatched_candidates = [
        candidate
        for candidate in eligible_candidates
        if not _has_non_weak_keyword_match(candidate)
    ]
    classification_batch = sorted(
        unmatched_candidates,
        key=lambda candidate: (
            candidate.score,
            candidate.story.points,
            candidate.story.comments,
        ),
        reverse=True,
    )[:TOPIC_CLASSIFIER_MAX_CANDIDATES]
    backend = model_backend
    if classifier is None or summarizer is None:
        backend = backend or CodexBackend()
    topic_classifier = classifier or backend
    classification_started = clock()
    try:
        classified_ai_ids = topic_classifier.classify(classification_batch)
    except Exception as exc:
        classification_duration = clock() - classification_started
        for candidate in classification_batch:
            candidate.topic_route = "classifier_failed"
        LOGGER.error(
            "component=topic_classifier status=failed candidates=%d duration=%.3fs error=%s message=%s",
            len(classification_batch),
            classification_duration,
            type(exc).__name__,
            exc,
        )
        classified_ai_ids = set()
    else:
        classification_duration = clock() - classification_started
        for candidate in classification_batch:
            candidate.topic_route = (
                "classifier_ai"
                if candidate.story.hn_item_id in classified_ai_ids
                else "classifier_non_ai"
            )
        LOGGER.info(
            "component=topic_classifier status=success candidates=%d ai_items=%d duration=%.3fs",
            len(classification_batch),
            sum(
                candidate.topic_route == "classifier_ai"
                for candidate in classification_batch
            ),
            classification_duration,
        )

    classified_ai_candidates = [
        candidate
        for candidate in unmatched_candidates
        if candidate.story.hn_item_id in classified_ai_ids
    ]
    for candidate in classified_ai_candidates:
        candidate.why = "topic classifier: AI"
    classified_ai_identity = {id(candidate) for candidate in classified_ai_candidates}
    ai_pool = [*known_ai_candidates, *classified_ai_candidates]
    hot_pool = [
        candidate
        for candidate in unmatched_candidates
        if id(candidate) not in classified_ai_identity
    ]

    ai_items, selected_hot_items = select_sections(ai_pool, hot_pool)
    article_client = article_fetcher or fetch_article_text
    summary_client = summarizer or backend
    summary_candidates = [*ai_items, *selected_hot_items]
    for candidate in summary_candidates:
        if (
            not candidate.story.story_text.strip()
            and candidate.story.source_url
            and candidate.story.source_url != candidate.story.hn_discussion_url
        ):
            try:
                fetched_text = article_client(candidate.story.source_url).strip()
                if fetched_text:
                    candidate.story = replace(
                        candidate.story, fetched_text=fetched_text
                    )
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
    public_json_path = Path(output_dir) / f"{label}.json"
    data_path = Path(data_dir) / f"{label}-hn-candidates.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(
            label, ai_items, selected_hot_items, ai_note=ai_note, hot_note=hot_note
        ),
        encoding="utf-8",
    )
    public_json_path.write_text(
        render_public_brief_json(
            label,
            generated_at or datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            ai_items,
            selected_hot_items,
            ai_note=("AI 数据源本次不可用，当前栏目可能不完整。" if ai_note else ""),
            hot_note=(
                "HN 热门数据源本次不可用，当前栏目可能不完整。" if hot_note else ""
            ),
        ),
        encoding="utf-8",
    )
    data_path.write_text(render_candidates_json(candidates), encoding="utf-8")
    model_input_path = None
    if capture_model_inputs:
        model_input_path = Path(data_dir) / "model-eval-inputs" / f"{label}.json"
        capture_model_evaluation_input(
            model_input_path,
            label,
            classification_batch,
            summary_candidates,
        )
    try:
        save_history(
            history_path,
            recommendation_history,
            label,
            [
                candidate.story.hn_item_id
                for candidate in [*ai_items, *selected_hot_items]
            ],
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
    return GenerateResult(
        brief_path=output_path,
        data_path=data_path,
        public_json_path=public_json_path,
        model_input_path=model_input_path,
    )


def _model_backend(name: str) -> ModelBackend:
    if name == "codex":
        return CodexBackend()
    if name == "gemini":
        return GeminiBackend.from_environment()
    raise ValueError(f"unsupported model backend: {name}")


def _candidate(story: Story) -> Candidate:
    return score_candidate(
        Candidate(story=story, matched_keywords=_keyword_matches(story))
    )


def _has_non_weak_keyword_match(candidate: Candidate) -> bool:
    return any(match.weight != "weak" for match in candidate.matched_keywords)


def _keyword_matches(story: Story):
    return match_keywords(story.title, story.story_text, story.source_url)
