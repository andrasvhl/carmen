"""What became of the day (Oct 31), a job owning only its own bookkeeping: at 23:50 Pacific one
day_outcome entry in the tap log, every task pre-selected or selected with what became of it; days
the server missed are written late on the next start.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from runner import config

from .adapters import clock
from .adapters import notes
from .adapters import state
from .adapters import taplog
from .planning import read as planning_read


# --- what became of the day (Oct 31) ---------------------------------------------------
# owner decision, Oct 31, "already do 1+2": the log knew what he picked in the morning, never whether it
# got done, so a good pick could not be told from one that sat all day. At 23:50 Pacific the
# server writes one `day_outcome` entry: every task Claude pre-selected or he selected, with what
# became of it, and the tasks he closed that were in neither. Days the server missed (down, or
# restarted past midnight) are written on the next start from the notes as they are then, marked
# `late`, so "moved" there means moved by now.


OUTCOMES = config.STATE_DIR / "checkin_app" / "outcomes.json"

OUTCOME_AT = dt.time(23, 50)


def outcome_of(name: str, day: dt.date, hidden: set) -> dict:
    p = notes._task_file(name)
    if not p:   # a row with no task note (from mail or chats)
        return {"id": name, "task": False, "outcome": "hidden" if name in hidden else "unknown"}
    fm, iso = notes._fm_of(p), day.isoformat()
    done_on = fm.get("completedDate", "")[:10]
    status, sched = fm.get("status", ""), fm.get("scheduled", "")[:10]
    if status == "done":
        outcome = "done" if done_on == iso else "done_later" if done_on > iso else "done_before"
    elif status == "cancelled":
        outcome = "cancelled"
    elif sched and sched > iso:
        outcome = "moved"
    else:
        outcome = "open"
    # The day's Log lines verbatim, minus Claude's own preparation: what happened to it that day.
    lines = [ln[2:].strip() for ln in re.findall(rf"^- {re.escape(clock._label(day))} · .*$", fm["_body"], re.M)
             if "prepared by Claude" not in ln]
    est = fm.get("timeEstimate", "")
    return {"id": name, "task": True, "outcome": outcome, "log": lines[:8], "scheduled": sched,
            "completed": done_on, "priority": fm.get("priority", ""), "est": est}


def day_outcome(day: dt.date, late: bool = False) -> dict:
    iso = day.isoformat()
    ov = planning_read.load_overrides()
    selected = list(ov["selected"].get(iso, []))
    hidden = set(ov["hidden"].get(iso, []))
    pre, opens = [], 0
    try:
        lines = taplog.LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for ln in lines:   # one torn line (two processes append here) must not hide the rest
        try:
            e = json.loads(ln)
            if e.get("kind") == "open" and e.get("at", "")[:10] == iso:
                opens += 1
                pre = [i for i in (e.get("shown") or {}).get("today") or [] if isinstance(i, str)] or pre
        except (ValueError, AttributeError, TypeError):
            continue
    tasks = []
    for name in dict.fromkeys(pre + selected):
        r = outcome_of(name, day, hidden)
        r.update(pre_selected=name in pre, selected=name in selected, one=bool(pre) and name == pre[0])
        tasks.append(r)
    seen = {t["id"] for t in tasks}
    unplanned = []
    for folder in (config.INBOX, config.VAULT / "Archive" / "Inbox"):
        for p in folder.glob("*.md") if folder.exists() else []:
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            if p.stem not in seen and re.search(rf"^completedDate:\s*['\"]?{iso}", head, re.M) \
                    and re.search(r"^type:\s*['\"]?action", head, re.M) and "not picked up, closed by Carmen" not in p.read_text(encoding="utf-8", errors="replace"):
                unplanned.append(p.stem)
    count = lambda pred: sum(1 for t in tasks if pred(t))
    return {"day": iso, "late": late, "opens": opens, "tasks": tasks, "closed_unplanned": unplanned,
            "counts": {"selected": len(selected), "pre_selected": len(pre),
                       "selected_done": count(lambda t: t["selected"] and t["outcome"] == "done"),
                       "pre_selected_done": count(lambda t: t["pre_selected"] and t["outcome"] == "done"),
                       "open": count(lambda t: t["outcome"] == "open"), "moved": count(lambda t: t["outcome"] == "moved"),
                       "closed_unplanned": len(unplanned)}}


def record_outcomes(now: dt.datetime | None = None, catch_up: int = 3) -> list[str]:
    """Write today's at 23:50, and any of the last few days that were missed. Returns the days written."""
    now = now or dt.datetime.now(clock.TZ)
    try:
        done = json.loads(OUTCOMES.read_text())
    except (OSError, ValueError):
        done = {}
    done = done if isinstance(done, dict) else {}
    wrote = []
    days = [(now.date() - dt.timedelta(days=i), True) for i in range(catch_up, 0, -1)]
    if now.time() >= OUTCOME_AT:
        days.append((now.date(), False))
    ov = planning_read.load_overrides()
    for day, late in days:
        iso = day.isoformat()
        if iso in done:
            continue
        # A missed day counts only if the app was used: something selected, or the day opened.
        out = day_outcome(day, late=late)
        if late and not (ov["selected"].get(iso) or out["opens"]):
            done[iso] = "skipped: app not used"
            continue
        taplog.log_event("day_outcome", **out)
        done[iso] = now.isoformat(timespec="seconds")
        wrote.append(iso)
    keep = {(now.date() - dt.timedelta(days=i)).isoformat() for i in range(30)}
    state.write(OUTCOMES, {d: v for d, v in done.items() if d in keep})
    return wrote
