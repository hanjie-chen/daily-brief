from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..alternate_reporting import AlternateReportingFinder
from ..config import TIMEZONE
from ..gemini_backend import GeminiBackend
from ..history import load_history, recent_ids, save_history
from ..hn_client import fetch_algolia_stories, fetch_hot_stories
from ..keywords import match_keywords
from ..model_backend import ModelBackend
from ..model_evaluation import capture_model_evaluation_input
from ..models import Candidate, Story
from ..public_schema import EmptyPublicBriefError, validate_public_brief
from ..render import render_candidates_json, render_markdown, render_public_brief_json
from ..same_article import SameArticleFinder
from ..scoring import score_candidate
from ..selection import dedupe_candidates
from ..syndicated_copy import SyndicatedCopyFinder
from ..time_window import TimeWindow, daily_window
from .classification import SelectionResult, classify_and_select_candidates
from .summaries import summarize_selected_candidates

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerateResult:
    brief_path: Path
    data_path: Path
    public_json_path: Path | None
    no_content_marker_path: Path | None = None
    model_input_path: Path | None = None


class SourceCollectionError(RuntimeError):
    """Raised when a Hacker News candidate source cannot be collected."""


@dataclass(frozen=True)
class _CandidatePool:
    all_candidates: list[Candidate]
    eligible_candidates: list[Candidate]
    history_path: Path
    recommendation_history: dict[str, list[str]]


def run_generate(
    output_dir,
    data_dir,
    date_label: str | None = None,
    algolia_stories: list[Story] | None = None,
    hot_stories: list[Story] | None = None,
    summarizer=None,
    classifier=None,
    article_fetcher=None,
    hn_discussion_fetcher=None,
    syndicated_finder: SyndicatedCopyFinder | None = None,
    alternate_reporting_finder: AlternateReportingFinder | None = None,
    clock: Callable[[], float] = time.monotonic,
    generated_at: str | None = None,
    model_backend: ModelBackend | None = None,
    capture_model_inputs: bool = False,
    same_article_finder: SameArticleFinder | None = None,
) -> GenerateResult:
    window = daily_window()
    label = date_label or window.date_label
    candidates = _collect_candidates(
        window,
        algolia_stories,
        hot_stories,
        clock,
    )
    candidate_pool = _exclude_recent_candidates(candidates, data_dir, label)

    backend = model_backend
    if classifier is None or summarizer is None:
        backend = backend or GeminiBackend.from_environment()

    selection = classify_and_select_candidates(
        candidate_pool.eligible_candidates,
        classifier or backend,
        article_fetcher,
        syndicated_finder,
        alternate_reporting_finder,
        window,
        clock,
        summary_client=summarizer or backend,
        hn_discussion_fetcher=hn_discussion_fetcher,
    )
    summarization_inputs = selection.summary_inputs + summarize_selected_candidates(
        selection.ai_items,
        selection.selected_hot_items,
        summarizer or backend,
        article_fetcher,
        hn_discussion_fetcher,
        syndicated_finder,
        alternate_reporting_finder,
        window,
        same_article_finder=same_article_finder,
    )
    return _persist_generation(
        output_dir,
        data_dir,
        label,
        generated_at,
        candidate_pool,
        selection,
        summarization_inputs,
        capture_model_inputs,
    )


def _fetch_source(
    source: str,
    fetch: Callable[[], list[Story]],
    clock: Callable[[], float],
) -> list[Story]:
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
        raise SourceCollectionError(
            f"news source collection failed: {source}"
        ) from exc

    duration = clock() - started
    LOGGER.info(
        "source=%s status=success stories=%d duration=%.3fs",
        source,
        len(stories),
        duration,
    )
    return stories


def _collect_candidates(
    window: TimeWindow,
    algolia_stories: list[Story] | None,
    hot_stories: list[Story] | None,
    clock: Callable[[], float],
) -> list[Candidate]:
    if algolia_stories is not None:
        algolia_items = algolia_stories
    else:
        algolia_items = _fetch_source(
            "algolia",
            lambda: fetch_algolia_stories(window),
            clock,
        )

    if hot_stories is not None:
        hot_items = hot_stories
    else:
        hot_items = _fetch_source(
            "hn_official",
            fetch_hot_stories,
            clock,
        )

    return dedupe_candidates(
        [*map(_candidate, algolia_items), *map(_candidate, hot_items)]
    )


def _exclude_recent_candidates(
    candidates: list[Candidate],
    data_dir,
    label: str,
) -> _CandidatePool:
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

    return _CandidatePool(
        all_candidates=candidates,
        eligible_candidates=eligible_candidates,
        history_path=history_path,
        recommendation_history=recommendation_history,
    )


def _persist_generation(
    output_dir,
    data_dir,
    label: str,
    generated_at: str | None,
    candidate_pool: _CandidatePool,
    selection: SelectionResult,
    summarization_inputs: list[Candidate],
    capture_model_inputs: bool,
) -> GenerateResult:
    ai_items = selection.ai_items
    selected_hot_items = selection.selected_hot_items
    output_path = Path(output_dir) / f"{label}.md"
    public_json_path = Path(output_dir) / f"{label}.json"
    no_content_marker_path = Path(output_dir) / f"{label}.no-content"
    data_path = Path(data_dir) / f"{label}-hn-candidates.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_markdown(label, ai_items, selected_hot_items),
        encoding="utf-8",
    )
    public_json_text = render_public_brief_json(
        label,
        generated_at or datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        ai_items,
        selected_hot_items,
    )
    public_payload = json.loads(public_json_text)
    written_public_json_path: Path | None
    written_marker_path: Path | None
    try:
        validate_public_brief(public_payload)
    except EmptyPublicBriefError:
        public_json_path.unlink(missing_ok=True)
        _atomic_write_text(no_content_marker_path, "")
        written_public_json_path = None
        written_marker_path = no_content_marker_path
        LOGGER.info("component=generate status=no_content date=%s", label)
    else:
        _atomic_write_text(public_json_path, public_json_text)
        no_content_marker_path.unlink(missing_ok=True)
        written_public_json_path = public_json_path
        written_marker_path = None
    data_path.write_text(
        render_candidates_json(candidate_pool.all_candidates),
        encoding="utf-8",
    )
    model_input_path = None
    if capture_model_inputs:
        model_input_path = Path(data_dir) / "model-eval-inputs" / f"{label}.json"
        capture_model_evaluation_input(
            model_input_path,
            label,
            selection.classification_batches,
            summarization_inputs,
        )
    try:
        save_history(
            candidate_pool.history_path,
            candidate_pool.recommendation_history,
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
        public_json_path=written_public_json_path,
        no_content_marker_path=written_marker_path,
        model_input_path=model_input_path,
    )


def _atomic_write_text(path: Path, content: str) -> None:
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
            temporary_path = Path(temporary_file.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _candidate(story: Story) -> Candidate:
    return score_candidate(
        Candidate(story=story, matched_keywords=_keyword_matches(story))
    )


def _keyword_matches(story: Story):
    return match_keywords(story.title, story.story_text, story.source_url)
