"""His day: the Pacific calendar date, stdlib only (Nov 11, audit A4).

The box runs UTC, so `date.today()` and `datetime.now().astimezone()` turn to tomorrow at 16:00
PT (17:00 in summer): the task hook asked for tomorrow's Log label, `vt todos` showed tomorrow's
tasks as today's, and flight watches and the Telegram day rolled early. Everything that means
"his day" asks here. Stdlib only, so the task hook stays fast. Carmen's 04:00 day turn stays in
jobs/checkin_app/adapters/clock.py (the full move is a Monday build).

AUTOMATION_NOW (an ISO time) freezes the clock for tests that run a tool as a subprocess.
"""
from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

# D-20
TZ = ZoneInfo("America/Los_Angeles")


# D-22
PLAN_TOMORROW_HOUR = 19


def now() -> dt.datetime:
    """Now, in Pacific time."""
    frozen = os.environ.get("AUTOMATION_NOW")
    if frozen:
        t = dt.datetime.fromisoformat(frozen.replace("Z", "+00:00"))
        return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).astimezone(TZ)
    return dt.datetime.now(TZ)


def today() -> dt.date:
    """His calendar date (Pacific), whatever the box's time zone."""
    return now().date()
