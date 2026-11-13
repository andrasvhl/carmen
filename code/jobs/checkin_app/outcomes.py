"""What became of the day (Oct 31), a job owning only its own bookkeeping: at 03:55 Pacific, the end
of the Carmen day (Nov 9: the day turns at 04:00, so 00:00 to 04:00 counts for the evening), one
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

OUTCOME_AT = dt.time(3, 55)   # on the next calendar date, before the 04:00 turn (one day model, Nov 9)
ON_TIME = dt.timedelta(hours=1)   # written later than this after it was due: marked late


def due_at(day: dt.date) -> dt.datetime:
    """When a Carmen day's outcome is due: 03:55 on the next calendar date."""
    return dt.datetime.combine(day + dt.timedelta(days=1), OUTCOME_AT, clock.TZ)


def last_due(now: dt.datetime) -> dt.date:
    """The latest Carmen day whose outcome is due by now."""
    return (now - dt.timedelta(hours=OUTCOME_AT.hour, minutes=OUTCOME_AT.minute)).date() - dt.timedelta(days=1)


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
    iso, nxt = day.isoformat(), (day + dt.timedelta(days=1)).isoformat()
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
            if e.get("kind") == "open" and e.get("at", "")[:10] in (iso, nxt) \
                    and clock.do_day(dt.datetime.fromisoformat(e["at"])).isoformat() == iso:   # its Carmen day
                opens += 1
                pre = [i for i in (e.get("shown") or {}).get("today") or [] if isinstance(i, str)] or pre
        except (ValueError, AttributeError, TypeError):
            continue
    tasks = []
    for name in dict.fromkeys(pre + selected):
        r = outcome_of(name, day, hidden)
        r.update(pre_selected=name in pre, selected=name in selected, one=bool(pre) and name == pre[0])
        tasks.append(r)
    # The plan measured by part steps done, not tasks closed (Nov 10, Tier 3.6: a trek prep never
    # closes in a day, so "0 of Claude's picks done" was partly false). Measurement only.
    try:
        from .planning import do as dmod
        e = dmod._day(dmod._load(), day)
    except Exception:
        e = {}
    for r in tasks:
        cids = [x for x in ((e.get("chunk_ids") or {}).get(r["id"]) or []) if x]
        his = set((e.get("his_ticks") or {}).get(r["id"]) or [])
        cl = {x.get("id") for x in ((e.get("claude_ticks") or {}).get(r["id"]) or []) if isinstance(x, dict)}
        done = [x for x in cids if x in his or x in cl]
        r["part"] = {"steps": len(cids), "done": len(done), "his": sum(1 for x in done if x in his),
                     "finished": bool(cids) and len(done) == len(cids)}
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
                       "closed_unplanned": len(unplanned),
                       "part_steps": sum(t["part"]["steps"] for t in tasks),
                       "part_steps_done": sum(t["part"]["done"] for t in tasks),
                       "parts_finished": count(lambda t: t["part"]["finished"]),
                       "pre_selected_parts_finished": count(lambda t: t["pre_selected"] and t["part"]["finished"]),
                       "his_parts_finished": count(lambda t: t["selected"] and not t["pre_selected"] and t["part"]["finished"])}}


def record_outcomes(now: dt.datetime | None = None, catch_up: int = 3) -> list[str]:
    """Write the Carmen day just ended at 03:55, and any of the last few days that were missed (late
    when written over an hour after they were due). Returns the days written."""
    now = now or dt.datetime.now(clock.TZ)
    try:
        done = json.loads(OUTCOMES.read_text())
    except (OSError, ValueError):
        done = {}
    done = done if isinstance(done, dict) else {}
    wrote = []
    last = last_due(now)
    days = [(d, now - due_at(d) > ON_TIME) for d in (last - dt.timedelta(days=i) for i in range(catch_up, -1, -1))]
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
