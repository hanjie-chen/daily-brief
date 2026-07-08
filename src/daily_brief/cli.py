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


def main(argv: list[str] | None = None) -> int:
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
) -> GenerateResult:
    window = daily_window()
    label = date_label or window.date_label
    algolia_items = algolia_stories if algolia_stories is not None else fetch_algolia_stories(window)
    hot_items = hot_stories if hot_stories is not None else fetch_hot_stories()

    algolia_candidates = [_ai_candidate(story) for story in algolia_items]
    ai_candidates = [candidate for candidate in algolia_candidates if candidate.matched_keywords]
    hot_candidates = [
        *[candidate for candidate in algolia_candidates if not candidate.matched_keywords],
        *[_hot_candidate(story) for story in hot_items if not _has_keyword_match(story)],
    ]
    candidates = dedupe_candidates([*ai_candidates, *hot_candidates])
    retained_candidate_ids = {id(candidate) for candidate in candidates}
    ai_pool = [candidate for candidate in ai_candidates if id(candidate) in retained_candidate_ids]
    hot_pool = [candidate for candidate in hot_candidates if id(candidate) in retained_candidate_ids]

    ai_items, selected_hot_items = select_sections(ai_pool, hot_pool)
    summary_client = summarizer or CodexSummarizer()
    for candidate in [*ai_items, *selected_hot_items]:
        try:
            candidate.summary = summary_client.summarize(candidate)
        except Exception:
            candidate.summary = fallback_summary(candidate)

    output_path = Path(output_dir) / f"{label}.md"
    data_path = Path(data_dir) / f"{label}-hn-candidates.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(label, ai_items, selected_hot_items), encoding="utf-8")
    data_path.write_text(render_candidates_json(candidates), encoding="utf-8")
    return GenerateResult(brief_path=output_path, data_path=data_path)


def _ai_candidate(story: Story) -> Candidate:
    return score_candidate(Candidate(story=story, matched_keywords=_keyword_matches(story)))


def _hot_candidate(story: Story) -> Candidate:
    return Candidate(story=story)


def _has_keyword_match(story: Story) -> bool:
    return bool(_keyword_matches(story))


def _keyword_matches(story: Story):
    return match_keywords(story.title, story.story_text, story.source_url)
