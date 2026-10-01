from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from .generation import RetryError, SourceCollectionError, run_generate, run_retry
from .llm import (
    GeminiBackend,
    ModelBackend,
    ModelEvaluationInputError,
    run_model_evaluation,
)
from .output import PublishError, publish_brief
from .time_window import daily_window

LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="daily-brief")
    parser.add_argument(
        "command",
        nargs="?",
        default="generate",
        choices=["generate", "publish", "evaluate-model", "retry"],
        help="Command to run.",
    )
    parser.add_argument("--output-dir", default="briefs")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--date")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--capture-model-inputs", action="store_true")
    parser.add_argument("--item", dest="item_ids", action="append")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_command_arguments(parser, args)
    if args.dry_run:
        return 0
    if args.command == "generate":
        try:
            backend = _model_backend()
        except ValueError as exc:
            LOGGER.error("component=model_backend status=failed message=%s", exc)
            return 1
        try:
            run_generate(
                output_dir=args.output_dir,
                data_dir=args.data_dir,
                capture_model_inputs=args.capture_model_inputs,
                model_backend=backend,
            )
        except SourceCollectionError as exc:
            LOGGER.error("component=generate status=failed message=%s", exc)
            return 1
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
            backend = GeminiBackend.from_environment(summarizer_fallback_models=())
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
    elif args.command == "retry":
        try:
            backend = _model_backend()
            result = run_retry(
                output_dir=args.output_dir,
                data_dir=args.data_dir,
                date_label=args.date,
                item_ids=args.item_ids,
                model_backend=backend,
            )
        except (RetryError, OSError, ValueError) as exc:
            LOGGER.error("component=retry status=failed message=%s", exc)
            return 1
        LOGGER.info(
            "component=retry status=completed attempted=%d updated=%d failed=%d",
            result.attempted,
            result.updated,
            result.failed,
        )
        return 1 if result.failed else 0
    return 0


def _validate_command_arguments(parser: argparse.ArgumentParser, args) -> None:
    if args.item_ids and args.command != "retry":
        parser.error("--item is only supported by retry")
    if args.capture_model_inputs and args.command == "retry":
        parser.error("retry does not support --capture-model-inputs")
    if args.command != "retry":
        return
    if not args.date:
        parser.error("retry requires --date YYYY-MM-DD")
    try:
        if date.fromisoformat(args.date).isoformat() != args.date:
            raise ValueError
    except ValueError:
        parser.error("retry requires --date YYYY-MM-DD")
    for item_id in args.item_ids or []:
        if not item_id.isascii() or not item_id.isdecimal() or int(item_id) <= 0:
            parser.error("--item must be a positive numeric Hacker News item id")


def _model_backend() -> ModelBackend:
    return GeminiBackend.from_environment()
