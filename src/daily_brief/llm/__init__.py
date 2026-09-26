"""Model-facing logic: prompts, evidence selection, provider adapters, and evaluation."""

from .gemini_backend import GeminiBackend
from .model_backend import ModelBackend, ensure_topic_decisions
from .model_evaluation import (
    ModelEvaluationInputError,
    capture_model_evaluation_input,
    run_model_evaluation,
)
from .summarizer import (
    ALTERNATE_REPORTING_SUMMARY_PREFIX,
    HN_DISCUSSION_SUMMARY_PREFIX,
    InsufficientSummaryMaterial,
    article_fetch_failure_summary,
    build_summary_context,
    fallback_summary,
    has_discussion_source,
    normalize_summary_text,
    route_summary_mode,
)


__all__ = [
    "ALTERNATE_REPORTING_SUMMARY_PREFIX",
    "HN_DISCUSSION_SUMMARY_PREFIX",
    "GeminiBackend",
    "InsufficientSummaryMaterial",
    "ModelBackend",
    "ModelEvaluationInputError",
    "article_fetch_failure_summary",
    "build_summary_context",
    "capture_model_evaluation_input",
    "ensure_topic_decisions",
    "fallback_summary",
    "has_discussion_source",
    "normalize_summary_text",
    "route_summary_mode",
    "run_model_evaluation",
]
