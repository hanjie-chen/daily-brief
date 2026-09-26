from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

LOGGER = logging.getLogger(__name__)
DEFAULT_HISTORY_DAYS = 7


def load_history(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("history must be an object")
        if not all(
            isinstance(label, str)
            and isinstance(item_ids, list)
            and all(isinstance(item_id, str) for item_id in item_ids)
            for label, item_ids in payload.items()
        ):
            raise ValueError("history entries must be string lists")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        LOGGER.warning(
            "component=recommendation_history status=invalid error=%s message=%s",
            type(exc).__name__,
            exc,
        )
        return {}
    return payload


def recent_ids(
    history: dict[str, list[str]],
    date_label: str,
    days: int = DEFAULT_HISTORY_DAYS,
) -> set[str]:
    current_date = date.fromisoformat(date_label)
    result: set[str] = set()
    for label, item_ids in history.items():
        try:
            age = (current_date - date.fromisoformat(label)).days
        except ValueError:
            continue
        if 1 <= age <= days:
            result.update(item_ids)
    return result


def save_history(
    path: Path,
    history: dict[str, list[str]],
    date_label: str,
    selected_ids: list[str],
    days: int = DEFAULT_HISTORY_DAYS,
) -> None:
    current_date = date.fromisoformat(date_label)
    retained: dict[str, list[str]] = {}
    for label, item_ids in history.items():
        try:
            age = (current_date - date.fromisoformat(label)).days
        except ValueError:
            continue
        if 0 <= age <= days and label != date_label:
            retained[label] = item_ids

    retained[date_label] = list(dict.fromkeys(selected_ids))
    retained = dict(sorted(retained.items()))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(retained, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
