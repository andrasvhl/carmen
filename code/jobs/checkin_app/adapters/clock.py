"""Clock adapter: one Pacific day. The box runs UTC, so every date the app writes or shows is taken in
TZ; the working day turns at 04:00 (do_day).
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


# The box runs UTC: without this an evening tap logged tomorrow's date and
# "Tomorrow" skipped a day (Oct 30, his first test).
TZ = ZoneInfo("America/Los_Angeles")


def _label(d: dt.date) -> str:
    return d.strftime("%b ") + str(d.day)


TURN_HOUR = 4          # the day turns at 4:00 (owner decision, Nov 1)


def do_day(now: dt.datetime) -> dt.date:
    return (now - dt.timedelta(hours=TURN_HOUR)).date()
