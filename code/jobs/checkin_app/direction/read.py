"""Direction, what Planning and Tasks read: priority steps and their short form, and whether the Raise
priority offer is snoozed for a task. Goals and the week shape are in goals.py (the Goals tab).
"""
from __future__ import annotations

import datetime as dt
import json

from runner import config


FM_PRIO = {"4-high": "H", "3-normal": "N"}

PRIO_STEPS = ["1-none", "2-low", "3-normal", "4-high"]

NORAISE = config.STATE_DIR / "checkin_app" / "noraise.json"   # "Not now" on Raise priority, per task


def raise_dismissed(task: str, today: dt.date) -> bool:
    try:
        on = json.loads(NORAISE.read_text()).get(task, "")
        return bool(on) and (today - dt.date.fromisoformat(on)).days < 7
    except (OSError, ValueError, AttributeError, TypeError):
        return False
