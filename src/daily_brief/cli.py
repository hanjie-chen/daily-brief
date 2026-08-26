from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from .article_fetcher import ArticleFetchError, ArticleFetchResult, fetch_article
from .config import TIMEZONE, TOPIC_CLASSIFIER_MAX_CANDIDATES
from .gemini_backend import GeminiBackend
from .history import load_history, recent_ids, save_history
from .hn_client import fetch_algolia_stories, fetch_hot_stories
from .keywords import match_keywords
from .model_backend import ModelBackend
from .model_evaluation import (
    ModelEvaluationInputError,
    capture_model_evaluation_input,
    run_model_evaluation,
)
from .models import (
    ArticleRetrieval,
    Candidate,
    RetrievalFailure,
    Story,
    SyndicatedRecovery,
)
from .publisher import PublishError, publish_brief
from .render import render_candidates_json, render_markdown, render_public_brief_json
from .scoring import score_candidate
from .selection import dedupe_candidates, select_sections
from .summarizer import (
    article_fetch_failure_summary,
    build_summary_context,
    fallback_summary,
    normalize_summary_text,
    route_summary_mode,
)
from .syndicated_copy import (
    MAX_SYNDICATED_CANDIDATES,
    SyndicatedCandidate,
    SyndicatedCopyFinder,
    SyndicatedFinderError,
    TavilySyndicatedCopyFinder,
    is_reuters_url,
    normalize_allowed_candidate_url,
    validate_syndicated_copy,
)
from .time_window import daily_window

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerateResult:
    brief_path: Path
    data_path: Path
    public_json_path: Path
    model_input_path: Path | None = None


@dataclass(frozen=True)
class _FetchedMaterial:
    text: str
    method: str
    extractor: str
    attempts: int
    fallback_reason: str
    retrieved_url: str
    material_origin: str


@dataclass(frozen=True)
class _SyndicatedOutcome:
    material: _FetchedMaterial | None
    audit: SyndicatedRecovery


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
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run:
        return 0
    if args.command == "generate":
        try:
            backend = _model_backend()
        except ValueError as exc:
            LOGGER.error("component=model_backend status=failed message=%s", exc)
            return 1
        run_generate(
            output_dir=args.output_dir,
            data_dir=args.data_dir,
            capture_model_inputs=args.capture_model_inputs,
            model_backend=backend,
        )
    elif args.command == "publish":
        date_label = args.date or daily_window().date_label
        try:
            result = publish_brief(
                brief_dir=args.output_dir,
                data_dir=args.data_dir,
                date_label=date_label,
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
            backend = _model_backend()
            result = run_model_evaluation(
                input_path,
                Path(args.data_dir) / "model-evaluations",
                backend,
            )
        except (ModelEvaluationInputError, OSError, ValueError) as exc:
            LOGGER.error("component=model_evaluation status=failed message=%s", exc)
            return 1
        LOGGER.info(
            "component=model_evaluation status=completed backend=%s failures=%d output=%s",
            backend.name,
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
    syndicated_finder: SyndicatedCopyFinder | None = None,
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
        backend = backend or GeminiBackend.from_environment()
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
    summary_client = summarizer or backend
    summary_candidates = [*ai_items, *selected_hot_items]
    summarization_inputs = []
    for candidate in summary_candidates:
        if (
            candidate.story.source_url
            and candidate.story.source_url != candidate.story.hn_discussion_url
        ):
            article_client = article_fetcher or partial(
                fetch_article,
                wayback_not_before=_wayback_not_before(
                    candidate.story.created_at,
                    window.start,
                ),
                wayback_not_after=window.end,
            )
            try:
                material = _coerce_fetched_material(
                    article_client(candidate.story.source_url),
                    candidate.story.source_url,
                )
                candidate.story = replace(candidate.story, fetched_text=material.text)
                candidate.article_retrieval = ArticleRetrieval(
                    status="success",
                    method=material.method,
                    extractor=material.extractor,
                    attempts=material.attempts,
                    fallback_attempted=bool(material.fallback_reason),
                    fallback_reason=material.fallback_reason,
                    retrieved_url=material.retrieved_url,
                    material_origin=material.material_origin,
                )
                candidate.summary_basis = (
                    "youtube_caption"
                    if material.method == "youtube_caption"
                    else "fetched_article"
                )
                LOGGER.info(
                    "component=article_fetch item_id=%s status=success method=%s "
                    "extractor=%s fallback_reason=%s attempts=%d",
                    candidate.story.hn_item_id,
                    material.method,
                    material.extractor or "none",
                    material.fallback_reason or "none",
                    material.attempts,
                )
            except Exception as exc:
                original_failure = _retrieval_failure(exc)
                recovery = _SyndicatedOutcome(
                    material=None,
                    audit=SyndicatedRecovery(),
                )
                if _should_attempt_reuters_recovery(candidate, original_failure):
                    recovery = _attempt_syndicated_recovery(
                        candidate,
                        article_client,
                        syndicated_finder,
                    )
                if recovery.material is not None:
                    material = recovery.material
                    candidate.story = replace(
                        candidate.story, fetched_text=material.text
                    )
                    candidate.article_retrieval = ArticleRetrieval(
                        status="success",
                        method=material.method,
                        extractor=material.extractor,
                        attempts=material.attempts,
                        fallback_attempted=bool(material.fallback_reason),
                        fallback_reason=material.fallback_reason,
                        retrieved_url=material.retrieved_url,
                        material_origin="syndicated_copy",
                        origin_failure=original_failure,
                        syndicated_recovery=recovery.audit,
                    )
                    candidate.summary_basis = "fetched_article"
                    LOGGER.info(
                        "component=article_fetch item_id=%s status=success "
                        "material_origin=syndicated_copy method=%s extractor=%s "
                        "fallback_reason=%s attempts=%d",
                        candidate.story.hn_item_id,
                        material.method,
                        material.extractor or "none",
                        material.fallback_reason or "none",
                        material.attempts,
                    )
                else:
                    candidate.article_retrieval = ArticleRetrieval(
                        status="failed",
                        method=original_failure.method,
                        extractor=original_failure.extractor,
                        attempts=original_failure.attempts,
                        fallback_attempted=original_failure.fallback_attempted,
                        fallback_reason=original_failure.fallback_reason,
                        error_type=original_failure.error_type,
                        error_code=original_failure.error_code,
                        error_message=original_failure.error_message,
                        syndicated_recovery=recovery.audit,
                    )
                    candidate.summary = article_fetch_failure_summary(candidate)
                    candidate.summary_basis = "none"
                    candidate.summary_status = "skipped"
                    LOGGER.error(
                        "component=article_fetch item_id=%s status=failed method=%s "
                        "extractor=%s error=%s code=%s attempts=%d message=%s",
                        candidate.story.hn_item_id,
                        original_failure.method,
                        original_failure.extractor or "none",
                        original_failure.error_type,
                        original_failure.error_code,
                        original_failure.attempts,
                        original_failure.error_message,
                    )
                    continue
        elif candidate.story.story_text.strip():
            candidate.article_retrieval = ArticleRetrieval(
                status="not_needed",
                method="story_text",
                extractor="plain_text",
            )
            candidate.summary_basis = "story_text"
        else:
            candidate.article_retrieval = ArticleRetrieval(
                status="not_needed",
                method="title",
            )
            candidate.summary_basis = "title_only"
        candidate.summary_mode = route_summary_mode(candidate)
        summary_context = build_summary_context(candidate)
        candidate.summary_context_strategy = summary_context.strategy
        candidate.summary_context_source_chars = summary_context.source_chars
        candidate.summary_context_selected_chars = summary_context.selected_chars
        candidate.summary_context_sections = list(summary_context.sections)
        summarization_inputs.append(candidate)
        try:
            candidate.summary = normalize_summary_text(
                summary_client.summarize(candidate)
            )
            candidate.summary_status = "success"
        except Exception as exc:
            print(f"Summary failed for {candidate.story.title}: {exc}", file=sys.stderr)
            candidate.summary = fallback_summary(candidate)
            candidate.summary_status = "failed"

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
            summarization_inputs,
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


def _model_backend() -> ModelBackend:
    return GeminiBackend.from_environment()


def _candidate(story: Story) -> Candidate:
    return score_candidate(
        Candidate(story=story, matched_keywords=_keyword_matches(story))
    )


def _has_non_weak_keyword_match(candidate: Candidate) -> bool:
    return any(match.weight != "weak" for match in candidate.matched_keywords)


def _bounded_error_message(exc: Exception, max_chars: int = 500) -> str:
    return " ".join(str(exc).split())[:max_chars]


def _coerce_fetched_material(fetch_result, retrieved_url: str) -> _FetchedMaterial:
    if isinstance(fetch_result, ArticleFetchResult):
        text = fetch_result.text.strip()
        method = fetch_result.method
        extractor = fetch_result.extractor
        fallback_reason = fetch_result.fallback_reason
        attempts = fetch_result.attempts
        effective_url = fetch_result.retrieved_url or retrieved_url
        material_origin = fetch_result.material_origin
    elif isinstance(fetch_result, str):
        text = fetch_result.strip()
        method = "direct"
        extractor = "plain_text"
        fallback_reason = ""
        attempts = 1
        effective_url = retrieved_url
        material_origin = "original"
    else:
        raise ArticleFetchError(
            "article fetcher returned an invalid result",
            error_code="invalid_fetch_result",
            method="direct",
        )
    if not text:
        raise ArticleFetchError(
            "article response contained no visible text",
            error_code="empty_content",
            method=method,
            extractor=extractor,
            fallback_attempted=bool(fallback_reason),
            fallback_reason=fallback_reason,
            attempts=attempts,
        )
    return _FetchedMaterial(
        text=text,
        method=method,
        extractor=extractor,
        attempts=attempts,
        fallback_reason=fallback_reason,
        retrieved_url=effective_url,
        material_origin=material_origin,
    )


def _wayback_not_before(created_at: str, default: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return default
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return default
    return parsed - timedelta(days=1)


def _retrieval_failure(exc: Exception) -> RetrievalFailure:
    return RetrievalFailure(
        method=getattr(exc, "method", "") or "direct",
        extractor=getattr(exc, "extractor", ""),
        attempts=getattr(exc, "attempts", 1),
        fallback_attempted=getattr(exc, "fallback_attempted", False),
        fallback_reason=getattr(exc, "fallback_reason", ""),
        error_type=type(exc).__name__,
        error_code=getattr(exc, "error_code", "fetch_failed"),
        error_message=_bounded_error_message(exc),
    )


def _should_attempt_reuters_recovery(
    candidate: Candidate,
    failure: RetrievalFailure,
) -> bool:
    return (
        is_reuters_url(candidate.story.source_url)
        and failure.method == "jina"
        and failure.fallback_attempted
        and failure.fallback_reason == "datadome_challenge"
    )


def _attempt_syndicated_recovery(
    candidate: Candidate,
    article_client,
    finder: SyndicatedCopyFinder | None,
) -> _SyndicatedOutcome:
    active_finder = finder
    if active_finder is None:
        active_finder = TavilySyndicatedCopyFinder.from_environment()
    provider = getattr(active_finder, "provider", "unknown")
    try:
        discovered = active_finder.find(candidate)
    except Exception as exc:
        error_code = (
            exc.error_code
            if isinstance(exc, SyndicatedFinderError)
            else "finder_failed"
        )
        LOGGER.warning(
            "component=syndicated_recovery item_id=%s provider=%s "
            "status=finder_failed code=%s",
            candidate.story.hn_item_id,
            provider,
            error_code,
        )
        return _SyndicatedOutcome(
            material=None,
            audit=SyndicatedRecovery(
                status="finder_failed",
                provider=provider,
                error_code=error_code,
            ),
        )

    if not isinstance(discovered, list):
        return _SyndicatedOutcome(
            material=None,
            audit=SyndicatedRecovery(
                status="finder_failed",
                provider=provider,
                error_code="malformed_results",
            ),
        )

    attempted = 0
    rejection_reasons: list[str] = []
    seen_urls: set[str] = set()
    bounded_candidates = discovered[:MAX_SYNDICATED_CANDIDATES]
    for syndicated in bounded_candidates:
        if not isinstance(syndicated, SyndicatedCandidate):
            rejection_reasons.append("malformed_candidate")
            continue
        normalized_url = normalize_allowed_candidate_url(syndicated.url)
        if normalized_url is None:
            rejection_reasons.append("unsupported_url")
            continue
        if normalized_url in seen_urls:
            rejection_reasons.append("duplicate_url")
            continue
        seen_urls.add(normalized_url)
        attempted += 1
        try:
            material = _coerce_fetched_material(
                article_client(normalized_url),
                normalized_url,
            )
        except Exception as exc:
            rejection_reasons.append("fetch_failed")
            LOGGER.warning(
                "component=syndicated_recovery item_id=%s provider=%s "
                "status=candidate_fetch_failed code=%s",
                candidate.story.hn_item_id,
                provider,
                getattr(exc, "error_code", "fetch_failed"),
            )
            continue
        effective_url = normalize_allowed_candidate_url(material.retrieved_url)
        if effective_url is None:
            rejection_reasons.append("redirected_to_unsupported_url")
            continue
        material = replace(material, retrieved_url=effective_url)
        validation = validate_syndicated_copy(candidate, syndicated, material.text)
        if not validation.accepted:
            rejection_reasons.append(validation.reason)
            continue
        audit = SyndicatedRecovery(
            status="success",
            provider=provider,
            discovered_candidates=len(discovered),
            attempted_candidates=attempted,
            rejection_reasons=rejection_reasons,
        )
        LOGGER.info(
            "component=syndicated_recovery item_id=%s provider=%s "
            "status=success discovered=%d attempted=%d",
            candidate.story.hn_item_id,
            provider,
            len(discovered),
            attempted,
        )
        return _SyndicatedOutcome(material=material, audit=audit)

    status = "not_found" if not discovered else "exhausted"
    audit = SyndicatedRecovery(
        status=status,
        provider=provider,
        discovered_candidates=len(discovered),
        attempted_candidates=attempted,
        rejection_reasons=rejection_reasons,
    )
    LOGGER.warning(
        "component=syndicated_recovery item_id=%s provider=%s "
        "status=%s discovered=%d attempted=%d",
        candidate.story.hn_item_id,
        provider,
        status,
        len(discovered),
        attempted,
    )
    return _SyndicatedOutcome(material=None, audit=audit)


def _keyword_matches(story: Story):
    return match_keywords(story.title, story.story_text, story.source_url)
