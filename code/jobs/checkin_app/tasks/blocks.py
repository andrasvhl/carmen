"""A closed task's future calendar blocks go; a reopened one gets them back (Nov 11).

owner decision, Nov 11, on SYSTEM.md's "closing a task closes its calendar blocks" (Sep 28), which Carmen
never did: "Not for old ones, yes for future ones." Every closer saves through tools/steps.write,
which calls on_close when a task goes from open to done or cancelled, and on_reopen when it opens
again. A block belongs to the task by the task id on its `Task:` line, on the main and Focus
calendars; one that starts after now is deleted, a past or running one stays. What was deleted is
kept (state checkin_app/cleared_blocks.json, 30 days) so a reopen puts it back. Without the
calendar write token (plan_today_prerun --auth-write) nothing is deleted; a calendar error is
recorded in the step history as blocks_not_cleared.
"""
from __future__ import annotations

import datetime as dt

from runner import config

from ..adapters import calendar, clock, state

STORE = config.STATE_DIR / "checkin_app" / "cleared_blocks.json"
KEEP_DAYS = 30


# D-21
def on_close(task_id: str, now: dt.datetime | None = None) -> dict:
    """Delete the task's future blocks and keep them. A history event: blocks_cleared or blocks_not_cleared."""
    if not task_id:
        return {}   # no id, no block can name it (blocks carry "Task: ... (t-xxxxxx)")
    now = now or dt.datetime.now(clock.TZ)
    try:
        found = calendar.future_blocks(task_id, now)
        for b in found:
            calendar.delete_event(b["cal"], b["event"]["id"])
    except calendar.NoWrite:
        return {}   # no write token yet: nothing to clear, nothing to record (he has not run --auth-write)
    except Exception as exc:   # noqa: BLE001 - a close never fails for the calendar
        return {"event": "blocks_not_cleared", "why": f"{type(exc).__name__}: {exc}"[:200]}
    if found:
        def f(d):
            d = {k: v for k, v in (d or {}).items() if v.get("at", "") >= (now - dt.timedelta(days=KEEP_DAYS)).isoformat()}
            d[task_id] = {"at": now.isoformat(timespec="seconds"), "blocks": found}
            return d
        state.update(STORE, f, {})
    return {"event": "blocks_cleared", "n": len(found),
            "blocks": [{"title": b["event"].get("summary", ""), "start": (b["event"].get("start") or {}).get("dateTime", "")} for b in found]}


def on_reopen(task_id: str, now: dt.datetime | None = None) -> dict:
    """Put back the future blocks the close deleted (those still in the future)."""
    now = now or dt.datetime.now(clock.TZ)
    kept = (state.read(STORE, {}) or {}).get(task_id) or {}
    back = []
    try:
        for b in kept.get("blocks") or []:
            start = (b["event"].get("start") or {}).get("dateTime", "")
            if start and dt.datetime.fromisoformat(start.replace("Z", "+00:00")) > now:
                calendar.insert_event(b["cal"], b["event"])
                back.append(b["event"].get("summary", ""))
    except calendar.NoWrite:
        return {}
    except Exception as exc:   # noqa: BLE001
        return {"event": "blocks_not_restored", "why": f"{type(exc).__name__}: {exc}"[:200]}

    def f(d):
        d = dict(d or {})
        d.pop(task_id, None)
        return d
    if kept:
        state.update(STORE, f, {})
    return {"event": "blocks_restored", "n": len(back), "titles": back} if kept else {}
