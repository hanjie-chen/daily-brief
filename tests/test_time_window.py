from datetime import datetime
from zoneinfo import ZoneInfo

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
