"""Planning, capacity: how much of the day is free for tasks. Every timed event is fixed (used time), a
slot (blocks that hold tasks: Big rocks, Admin backup, the focus mornings) or free; free minutes are
counted between 09:00 and the 21:00 wind-down, from now on today. Read from the Calendar adapter.
Planning v2 (capacity, fit) builds on this file.
"""
from __future__ import annotations

import datetime as dt
import re

from ..adapters import calendar
from ..adapters import clock
from ..tasks import read as tasks


# --- the day's time budget ------------------------------------------------------------
# owner decision, Oct 30: show how much estimated time is already planned against how much
# the day has. Every timed event is one of three kinds (owner decision, Nov 2: free time ignored his
# Week Structure, and Big rocks counted as busy although big-rock tasks go in there):
#   fixed: accepted events on the blocking calendars, and the standing structure on Focus
#          (morning routine, commute, lunch, dinner, email batch, recordings). Used time.
#   slot:  blocks that exist to hold tasks: ❗ Big rocks, Admin backup, the 🔒 focus mornings
#          (SCHEDULABLE BLOCKER). Time for tasks, shown apart from free.
#   free:  the rest of the window, including task blocks Claude booked on Focus (a `Task:`
#          line), whose time the planned side already counts, and invites not yet accepted.


DAY_START, DAY_END = 9 * 60, 21 * 60     # 09:00-21:00 every day, a deliberate rule (checkin-app.md § Known limitations), not the Week Structure's per-day times


def _merge(spans) -> list[tuple[int, int]]:
    merged = []
    for s, f in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], f))
        else:
            merged.append((s, f))
    return merged


def busy_spans(day: dt.date) -> list[tuple[int, int]] | None:
    """Merged fixed minutes-of-day (the kinds above). None if unreadable."""
    events = calendar.day_events(day)
    if events is None:
        return None
    return _merge((e["s"], e["f"]) for e in events if e["busy"])


def slot_spans(day: dt.date, fixed: list[tuple[int, int]]) -> list[dict]:
    """The day's slots, minus what fixed time sits inside them, each with its title."""
    out = []
    for e in calendar.day_events(day) or []:
        if not e.get("slot") or e.get("allday"):
            continue
        parts = [(e["s"], e["f"])]
        for fs, ff in fixed:
            parts = [q for a, b in parts for q in ((a, min(b, fs)), (max(a, ff), b)) if q[1] > q[0]]
        t = "Focus block" if "🔒" in e["title"] else re.sub(r"^\W+", "", e["title"]).strip()
        out += [{"s": a, "f": b, "title": t} for a, b in parts]
    return out


# Every free gap counts, however short (owner decision, Nov 5: "the 20 minute gap should not be
# closed"); raise this to leave out gaps shorter than it.
MIN_GAP = 0


def free_gaps(start: int, end: int, taken: list[tuple[int, int]]) -> int:
    """Minutes between start and end outside the taken spans, counting only gaps of MIN_GAP or more."""
    free, at = 0, start
    for s, f in _merge(taken) + [(end, end)]:
        gap = min(s, end) - at
        if gap >= MIN_GAP:
            free += gap
        at = max(at, min(f, end))
        if at >= end:
            break
    return free


def budget(day: dt.date, selection: list[dict]) -> dict:
    now = dt.datetime.now(clock.TZ)
    start = max(DAY_START, now.hour * 60 + now.minute) if day == now.date() else DAY_START
    if day < now.date():   # the Carmen day after midnight (Nov 9): its time has gone
        start = DAY_END
    spans = busy_spans(day)
    free, slots = None, []
    if spans is not None:
        slots = slot_spans(day, spans)
        free = free_gaps(start, DAY_END, spans + [(x["s"], x["f"]) for x in slots])
    planned = sum(tasks._minutes(r.get("est", "")) or 0 for r in selection)
    unestimated = sum(1 for r in selection if tasks._minutes(r.get("est", "")) is None)
    # The page recomputes planned on every Add and free from these each minute, so it is live.
    return {"free_min": free, "planned_min": planned, "unestimated": unestimated,
            "window": f"{start // 60:02d}:{start % 60:02d}-21:00",
            "busy": spans or [], "slots": slots, "day_start": DAY_START, "day_end": DAY_END,
            "min_gap": MIN_GAP, "start": start}
