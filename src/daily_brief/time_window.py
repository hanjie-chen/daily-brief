from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import RUN_HOUR, TIMEZONE


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime
    date_label: str


def daily_window(now: datetime | None = None) -> TimeWindow:
    current = now or datetime.now(TIMEZONE)
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=TIMEZONE)
    current = current.astimezone(TIMEZONE)
    today_boundary = datetime.combine(current.date(), time(RUN_HOUR), tzinfo=TIMEZONE)

    if current >= today_boundary:
        end = today_boundary
    else:
        end = today_boundary - timedelta(days=1)

    start = end - timedelta(days=1)
    return TimeWindow(start=start, end=end, date_label=end.date().isoformat())
