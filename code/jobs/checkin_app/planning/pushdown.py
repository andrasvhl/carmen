"""Push-down (Nov 10, the owner: "we will need a mechanism that pushes things down or out since e.g.
sara kabiri i threw it out a couple of times and it should not be prioritised").

Each time he takes a task out of the day with Not today / Not tomorrow / Not important, or says
Not now, the task is pushed down in Claude's ranking: more with every repeat, fading over
WINDOW days. A task that is due or overdue is never pushed: the due key ranks first, and Plan's
Due group shows it whatever its rank. Read by planning/read.py `build_view`; written by the
tap handlers in tasks/actions.py. Logged as `pushed_down`.
"""
from __future__ import annotations

import datetime as dt

from runner import config

from ..adapters import state
from ..adapters.taplog import log_event

FILE = config.STATE_DIR / "checkin_app" / "pushdown.json"
WINDOW = 14       # days a push counts, fading linearly to nothing
MAX_LEVEL = 3     # three or more recent pushes rank the same: at the bottom of Claude's order


def record(row_id: str, day: dt.date, reason: str) -> int:
    """One push for the task, dated with the Carmen day. Returns its level now."""
    out = {}

    def f(raw):
        cur = raw if isinstance(raw, dict) else {}
        old = (day - dt.timedelta(days=WINDOW)).isoformat()
        cur = {k: [x for x in v if isinstance(x, dict) and str(x.get("d", "")) > old] for k, v in cur.items() if isinstance(v, list)}
        cur = {k: v for k, v in cur.items() if v}
        cur.setdefault(row_id, []).append({"d": day.isoformat(), "r": str(reason)[:20]})
        out["level"] = level(row_id, day, cur)
        return cur
    state.update(FILE, f, {})
    log_event("pushed_down", id=row_id, reason=reason, level=out.get("level", 0), day=day.isoformat())
    return out.get("level", 0)


def unrecord(row_id: str, day: dt.date, reason: str) -> None:
    """Undo of a take-out (r8 F5): the push that tap recorded goes again, the latest one of that
    day and reason."""
    def f(raw):
        cur = raw if isinstance(raw, dict) else {}
        xs = cur.get(row_id) if isinstance(cur.get(row_id), list) else []
        for k in range(len(xs) - 1, -1, -1):
            if isinstance(xs[k], dict) and xs[k].get("d") == day.isoformat() and xs[k].get("r") == str(reason)[:20]:
                del xs[k]
                break
        if not xs:
            cur.pop(row_id, None)
        return cur
    state.update(FILE, f, {})
    log_event("push_undone", id=row_id, reason=reason, day=day.isoformat())


def load() -> dict:
    d = state.read(FILE, {})
    return d if isinstance(d, dict) else {}


def weight(row_id: str, day: dt.date, data: dict | None = None) -> float:
    """The task's recent pushes, each counting 1 on its day and fading to 0 after WINDOW days."""
    w = 0.0
    for x in (load() if data is None else data).get(row_id) or []:
        try:
            age = (day - dt.date.fromisoformat(str(x.get("d", ""))[:10])).days
        except ValueError:
            continue
        if 0 <= age < WINDOW:
            w += 1 - age / WINDOW
    return w


def level(row_id: str, day: dt.date, data: dict | None = None) -> int:
    """0 (not pushed) to MAX_LEVEL: one fresh push is 1, two are 2, and so on; old ones fade."""
    return min(MAX_LEVEL, int(round(weight(row_id, day, data))))
