import json
import logging

from daily_brief.history import load_history, recent_ids, save_history


def test_load_history_returns_empty_for_missing_file(tmp_path):
    assert load_history(tmp_path / "missing.json") == {}


def test_load_history_logs_and_returns_empty_for_malformed_file(tmp_path, caplog):
    path = tmp_path / "recommendation-history.json"
    path.write_text("not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="daily_brief.history"):
        result = load_history(path)

    assert result == {}
    assert "component=recommendation_history status=invalid" in caplog.text


def test_load_history_rejects_invalid_shape(tmp_path):
    path = tmp_path / "recommendation-history.json"
    path.write_text(json.dumps({"2026-07-19": "not-a-list"}), encoding="utf-8")

    assert load_history(path) == {}


def test_recent_ids_ignore_current_date_and_expire_after_seven_days():
    history = {
        "2026-07-20": ["same-day"],
        "2026-07-19": ["yesterday"],
        "2026-07-13": ["seven-days"],
        "2026-07-12": ["expired"],
        "2026-07-21": ["future"],
    }

    assert recent_ids(history, "2026-07-20") == {"yesterday", "seven-days"}


def test_save_history_replaces_same_date_prunes_and_writes_valid_json(tmp_path):
    path = tmp_path / "nested" / "recommendation-history.json"
    history = {
        "2026-07-20": ["old-same-day"],
        "2026-07-19": ["yesterday"],
        "2026-07-12": ["expired"],
        "invalid": ["bad-date"],
    }

    save_history(path, history, "2026-07-20", ["new", "new", "second"])

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "2026-07-19": ["yesterday"],
        "2026-07-20": ["new", "second"],
    }
    assert not path.with_suffix(".json.tmp").exists()
