import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from daily_brief.time_window import daily_window


def test_daily_window_uses_singapore_8am_boundary():
    now = datetime(2026, 7, 8, 9, 15, tzinfo=ZoneInfo("Asia/Singapore"))

    window = daily_window(now)

    assert window.date_label == "2026-07-08"
    assert window.start == datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert window.end == datetime(2026, 7, 8, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))


def test_daily_window_before_8am_uses_previous_label():
    now = datetime(2026, 7, 8, 7, 59, tzinfo=ZoneInfo("Asia/Singapore"))

    window = daily_window(now)

    assert window.date_label == "2026-07-07"
    assert window.start == datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert window.end == datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="requires process timezone support")
def test_daily_window_treats_naive_now_as_singapore_local(monkeypatch):
    original_timezone = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()

    try:
        window = daily_window(datetime(2026, 7, 8, 7, 59))
    finally:
        if original_timezone is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original_timezone)
        time.tzset()

    assert window.date_label == "2026-07-07"
    assert window.start == datetime(2026, 7, 6, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
    assert window.end == datetime(2026, 7, 7, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))
