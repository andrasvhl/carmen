"""When a proactive message should arrive silently: read from the vault, never set here.

The window is the Sleep row of `_system/scheduling/working-conditions.md`, the file that
holds the machine-read windows (Week Structure says so). Read fresh on every
call, so changing the note changes the bridge with no deploy (the owner,
2026-10-04: "This should be sth pulled from either weekly structure or sth
similar").

    | Sleep | **Wake 07:30-07:45 weekdays, ~09:00 weekends.** Wind-down from 21:30,
    lights off ~23:00 (later when going out). ...

Quiet runs from lights off to the latest wake time: on a weekday night until
07:45, and on Friday and Saturday nights until the weekend wake. A row that
no longer parses falls back to DEFAULTS and says so in the log, rather than
messaging at 3am or never.
"""

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

TZ = ZoneInfo("America/Los_Angeles")
SOURCE = Path("_system") / "scheduling" / "working-conditions.md"


@dataclass(frozen=True)
class Window:
    lights_off: time
    weekday_wake: time
    weekend_wake: time
    source: str          # "vault" or why the defaults were used


DEFAULTS = Window(time(23, 0), time(7, 45), time(9, 0), "defaults")

_TIME = r"(\d{1,2})[:.](\d{2})"


def _t(h: str, m: str) -> time:
    return time(int(h), int(m))


def parse(text: str) -> Window:
    """The Sleep row of working-conditions, or DEFAULTS with the reason."""
    row = next((l for l in text.splitlines() if re.match(r"\s*\|\s*Sleep\s*\|", l)), "")
    if not row:
        return Window(*_fields(DEFAULTS), source="defaults: no Sleep row")
    plain = row.replace("*", "")
    wake = re.search(rf"Wake\s+{_TIME}(?:\s*[-–]\s*{_TIME})?\s+weekdays", plain, re.I)
    weekend = re.search(rf"~?\s*{_TIME}\s+weekends", plain, re.I)
    off = re.search(rf"lights\s+off\s+~?\s*{_TIME}", plain, re.I)
    if not (wake and weekend and off):
        return Window(*_fields(DEFAULTS), source="defaults: Sleep row did not parse")
    # A range ("07:30-07:45") means "awake by the later end".
    weekday = _t(wake.group(3), wake.group(4)) if wake.group(3) else _t(wake.group(1), wake.group(2))
    return Window(_t(*off.groups()), weekday, _t(*weekend.groups()), "vault")


def _fields(w: Window) -> tuple[time, time, time]:
    return w.lights_off, w.weekday_wake, w.weekend_wake


def window(vault: Path | None = None) -> Window:
    try:
        return parse(((vault or config.VAULT) / SOURCE).read_text(encoding="utf-8"))
    except OSError as exc:
        return Window(*_fields(DEFAULTS), source=f"defaults: {type(exc).__name__}")


def is_quiet(now: datetime | None = None, win: Window | None = None) -> bool:
    """True between lights off and the next morning's wake, Pacific time."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    win = win or window()
    t = now.time()
    if t >= win.lights_off:
        return True
    # Before lights off: quiet only if still before this morning's wake. The
    # morning belongs to the day it is, so Saturday and Sunday use the weekend wake.
    wake = win.weekend_wake if now.weekday() >= 5 else win.weekday_wake
    return t < wake


def next_loud(now: datetime | None = None, win: Window | None = None) -> datetime:
    """When quiet ends, for the log line."""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    win = win or window()
    day = now.date() if now.time() < win.lights_off else now.date() + timedelta(days=1)
    wake = win.weekend_wake if day.weekday() >= 5 else win.weekday_wake
    return datetime.combine(day, wake, TZ)
