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


# D-15
TURN_HOUR = 4          # the day turns at 4:00 (owner decision, Nov 1)


def do_day(now: dt.datetime) -> dt.date:
    return (now - dt.timedelta(hours=TURN_HOUR)).date()


def carmen_day(now: dt.datetime | None = None) -> dt.date:
    """The Carmen day (owner decision, Nov 9, the one day model): 04:00 to 04:00 Pacific, the day on
    screen. Every recording action (Done, ticks and their ✅ date, Sent, Skip, Not today, Undo,
    Progress, wins, the meditation entry, + Task, the day's outcome) files under it, so a tap at
    00:30 still belongs to the evening's day. The calendar date is right only for the calendar
    itself and for timestamps."""
    return do_day(now or dt.datetime.now(TZ))
