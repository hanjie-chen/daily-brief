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
