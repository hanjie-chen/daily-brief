import json
import logging
from types import SimpleNamespace

import pytest

from daily_brief import cli
from daily_brief.cli import build_parser, main
from daily_brief.generation import SourceCollectionError
from daily_brief.llm.gemini_backend import GeminiBackend as RealGeminiBackend
from daily_brief.llm.model_evaluation import capture_model_evaluation_input
from daily_brief.models import Candidate
from fakes import FakeGeminiBackendFactory, story


@pytest.fixture(autouse=True)
def prevent_live_model_backend(monkeypatch):
    monkeypatch.setattr(cli, "GeminiBackend", FakeGeminiBackendFactory)


def test_parser_defaults_to_generate_command():
    parser = build_parser()

    args = parser.parse_args([])

    assert args.command == "generate"
    assert args.output_dir == "briefs"
    assert args.data_dir == "data"
    assert args.date is None
    assert args.force is False
    assert args.dry_run is False
    assert args.capture_model_inputs is False


def test_main_dry_run_does_not_create_output_directories_or_files(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "briefs"
    data_dir = tmp_path / "data"

    def fail_if_called(**kwargs):
        raise AssertionError("dry-run should not generate files")

    monkeypatch.setattr(cli, "run_generate", fail_if_called)

    exit_code = main(
        [
            "generate",
            "--output-dir",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert not output_dir.exists()
    assert not data_dir.exists()


def test_main_publish_targets_current_daily_brief_date(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "daily_window",
        lambda: SimpleNamespace(date_label="2026-07-31"),
    )
    monkeypatch.setattr(
        cli,
        "publish_brief",
        lambda **kwargs: (
            calls.append(kwargs) or SimpleNamespace(published=1, skipped=0)
        ),
    )

    assert main(["publish"]) == 0

    assert calls[0]["date_label"] == "2026-07-31"


def test_main_publish_explicit_date_overrides_current_date(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cli,
        "daily_window",
        lambda: (_ for _ in ()).throw(
            AssertionError("explicit publish date must not read the current window")
        ),
    )
    monkeypatch.setattr(
        cli,
        "publish_brief",
        lambda **kwargs: (
            calls.append(kwargs) or SimpleNamespace(published=1, skipped=0)
        ),
    )

    assert main(["publish", "--date", "2026-07-25"]) == 0

    assert calls[0]["date_label"] == "2026-07-25"


def test_main_uses_gemini_backend_for_production_generate(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "run_generate", lambda **kwargs: calls.append(kwargs))

    assert main(["generate"]) == 0

    assert len(calls) == 1
    assert calls[0]["model_backend"].name == "fake"


def test_main_reports_inconclusive_no_content_as_generate_failure(
    monkeypatch, caplog
):
    monkeypatch.setattr(
        cli,
        "run_generate",
        lambda **kwargs: (_ for _ in ()).throw(
            SourceCollectionError(
                "news source collection failed: algolia"
            )
        ),
    )

    with caplog.at_level(logging.ERROR, logger="daily_brief"):
        exit_code = main(["generate"])

    assert exit_code == 1
    assert "component=generate status=failed" in caplog.text
    assert "news source collection failed: algolia" in caplog.text


def test_main_reports_missing_gemini_key_for_production_generate(monkeypatch, caplog):
    monkeypatch.setattr(cli, "GeminiBackend", RealGeminiBackend)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with caplog.at_level(logging.ERROR, logger="daily_brief"):
        exit_code = main(["generate"])

    assert exit_code == 1
    assert "GEMINI_API_KEY is not configured" in caplog.text


def test_main_reports_missing_gemini_key_for_evaluation(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cli, "GeminiBackend", RealGeminiBackend)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with caplog.at_level(logging.ERROR, logger="daily_brief"):
        exit_code = main(
            [
                "evaluate-model",
                "--date",
                "2026-07-20",
                "--data-dir",
                str(tmp_path / "data"),
            ]
        )

    assert exit_code == 1
    assert "GEMINI_API_KEY is not configured" in caplog.text


def test_main_evaluate_model_replays_captured_input(tmp_path):
    data_dir = tmp_path / "data"
    input_path = data_dir / "model-eval-inputs/2026-07-20.json"
    capture_model_evaluation_input(
        input_path,
        "2026-07-20",
        [[Candidate(story("1", "AI tool", story_text="Grounded facts."))]],
        [Candidate(story("1", "AI tool", story_text="Grounded facts."))],
    )

    exit_code = main(
        [
            "evaluate-model",
            "--date",
            "2026-07-20",
            "--data-dir",
            str(data_dir),
        ]
    )

    assert exit_code == 0
    output_path = data_dir / "model-evaluations/2026-07-20-fake.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["backend"] == "fake"
    assert payload["exploration_classifications"][0]["status"] == "success"
    assert payload["summaries"][0]["summary"] == "Summary for AI tool"
