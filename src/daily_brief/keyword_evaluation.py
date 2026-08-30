from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from .config import RUN_HOUR, TIMEZONE
from .hn_client import fetch_algolia_stories
from .keywords import match_keywords
from .models import Story
from .time_window import TimeWindow

CORPUS_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1


def collect_corpus(
    first_brief_date: date,
    last_brief_date: date,
    *,
    fetcher: Callable[[TimeWindow], list[Story]] = fetch_algolia_stories,
) -> dict[str, Any]:
    if first_brief_date > last_brief_date:
        raise ValueError("first brief date must not be after last brief date")

    start = datetime.combine(
        first_brief_date - timedelta(days=1),
        time(RUN_HOUR),
        tzinfo=TIMEZONE,
    )
    end = datetime.combine(last_brief_date, time(RUN_HOUR), tzinfo=TIMEZONE)
    stories: list[Story] = []
    brief_date = first_brief_date
    while brief_date <= last_brief_date:
        window_end = datetime.combine(brief_date, time(RUN_HOUR), tzinfo=TIMEZONE)
        stories.extend(
            fetcher(
                TimeWindow(
                    start=window_end - timedelta(days=1),
                    end=window_end,
                    date_label=brief_date.isoformat(),
                )
            )
        )
        brief_date += timedelta(days=1)
    return {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "first_brief_date": first_brief_date.isoformat(),
        "last_brief_date": last_brief_date.isoformat(),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "stories": [
            {
                "hn_item_id": story.hn_item_id,
                "title": story.title,
                "story_text": story.story_text,
                "url": story.source_url,
                "created_at": story.created_at,
            }
            for story in stories
        ],
    }


def evaluate_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
    _validate_corpus(corpus)
    keyword_counts: Counter[str] = Counter()
    hits: list[dict[str, Any]] = []

    for story in corpus["stories"]:
        matches = match_keywords(
            story["title"],
            story["story_text"],
            story["url"],
        )
        if not matches:
            continue

        unique_keywords = dict.fromkeys(match.keyword for match in matches)
        keyword_counts.update(unique_keywords.keys())
        hits.append(
            {
                **story,
                "matches": [asdict(match) for match in matches],
            }
        )

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "corpus": {
            "first_brief_date": corpus["first_brief_date"],
            "last_brief_date": corpus["last_brief_date"],
            "window_start": corpus["window_start"],
            "window_end": corpus["window_end"],
            "story_count": len(corpus["stories"]),
        },
        "keyword_counts": dict(sorted(keyword_counts.items())),
        "hits": hits,
    }


def _validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise ValueError("unsupported keyword evaluation corpus schema")

    required_metadata = {
        "first_brief_date",
        "last_brief_date",
        "window_start",
        "window_end",
    }
    if not required_metadata.issubset(corpus):
        raise ValueError("keyword evaluation corpus metadata is incomplete")

    stories = corpus.get("stories")
    if not isinstance(stories, list):
        raise ValueError("keyword evaluation corpus stories must be a list")
    required_story_fields = {
        "hn_item_id",
        "title",
        "story_text",
        "url",
        "created_at",
    }
    for story in stories:
        if not isinstance(story, dict) or not required_story_fields.issubset(story):
            raise ValueError("keyword evaluation corpus story is invalid")
        if not all(isinstance(story[field], str) for field in required_story_fields):
            raise ValueError("keyword evaluation corpus story fields must be strings")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("keyword evaluation input must be a JSON object")
    return payload


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and replay Algolia stories through production keyword matching.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Fetch the inclusive range of production brief windows from Algolia.",
    )
    collect.add_argument("--first-brief-date", required=True, type=_date_argument)
    collect.add_argument("--last-brief-date", required=True, type=_date_argument)
    collect.add_argument("--output", required=True, type=Path)

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Run production match_keywords() over a saved corpus.",
    )
    evaluate.add_argument("--corpus", required=True, type=Path)
    evaluate.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "collect":
        payload = collect_corpus(args.first_brief_date, args.last_brief_date)
        _write_json(args.output, payload)
    else:
        payload = evaluate_corpus(_read_json(args.corpus))
        _write_json(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
