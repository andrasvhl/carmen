"""Do: the day he executes from (owner decision, Nov 1: "we need to split ToDo into Select and then
the actual to do from where you execute ... ToDo switches sometimes, whereas we need like an
execution page").

One list per day, his commitment for it, which no re-ranking touches:
  - what he confirmed in Plan (Create or Update plan) or added to the day there (+),
  - what he committed to before and did not finish (carried over, tagged with the day it was
    first committed; after 3 days in a row the card asks Keep, Move or Drop),
  - what is scheduled or due that day (a prior commitment too).
Ordered by the calendar blocks booked for it, then carried, then in his order. Finished tasks go
to the Done fold; a task whose date moved past the day leaves with a "Moved to ..." line. A
refresh changes the cards (next step, "looks done"), never the list; something new or urgent
comes as a suggestion he accepts. The day turns at 4:00, so a late evening is still that day.

State: checkin_app/do.json, {"days": {"YYYY-MM-DD": {"ids", "carried", "dropped", "dismissed",
"kept"}}}. A day is written once on first sight (the carry-over) and then only by his taps.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse

from runner import config

STATE = config.STATE_DIR / "checkin_app" / "do.json"
TURN_HOUR = 4          # the day turns at 4:00 (owner decision, Nov 1)
ASK_AFTER = 3          # carried this many days in a row: Keep, Move or Drop
KEEP_DAYS = 14         # days of history kept in do.json
SUGGEST_MAX = 3


def do_day(now: dt.datetime) -> dt.date:
    return (now - dt.timedelta(hours=TURN_HOUR)).date()


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}}
    if not isinstance(d, dict) or not isinstance(d.get("days"), dict):
        return {"days": {}}
    return d


def _save(d: dict, today: dt.date) -> None:
    keep = {(today - dt.timedelta(days=i)).isoformat() for i in range(-2, KEEP_DAYS)}
    d["days"] = {k: v for k, v in d["days"].items() if k in keep}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)


def _day(d: dict, day: dt.date) -> dict:
    e = d["days"].get(day.isoformat())
    if not isinstance(e, dict):
        e = {}
    for k, empty in (("ids", []), ("carried", {}), ("dropped", []), ("dismissed", []), ("kept", {})):
        if not isinstance(e.get(k), type(empty)):
            e[k] = type(empty)()
    return e


def _open(task_id: str, day: dt.date, alive: set, note) -> bool:
    """Still his to do on that day: an open note not moved past it, or a taskless row that is
    still in the view."""
    st = note(task_id)
    if st is None:
        return task_id in alive
    if st.get("status") in ("done", "cancelled"):
        return False
    return not (st.get("scheduled") and st["scheduled"] > day.isoformat()
                and not (st.get("due") and st["due"] <= day.isoformat()))


def ensure(day: dt.date, alive: set, note, write: bool = True) -> dict:
    """The day's entry. When the day arrives, what the last committed day left open carries
    over, once ("rolled"): a plan made the evening before already has an entry by then."""
    d = _load()
    key = day.isoformat()
    e = _day(d, day)
    if e.get("rolled"):
        return e
    prev = sorted(k for k in d["days"] if k < key and (day - dt.date.fromisoformat(k)).days <= 7)
    if prev:
        p = _day(d, dt.date.fromisoformat(prev[-1]))
        for i in list(p["carried"]) + p["ids"]:
            if i in e["carried"] or i in e["ids"] or i in p["dropped"] or not _open(i, day, alive, note):
                continue
            e["carried"][i] = p["carried"].get(i) or prev[-1]
    e["rolled"] = True
    if write:
        d["days"][key] = e
        _save(d, day)
    return e


def change(day: dt.date, fn) -> dict:
    d = _load()
    e = _day(d, day)
    fn(e)
    d["days"][day.isoformat()] = e
    _save(d, day)
    return e


def commit(day: dt.date, ids: list[str]) -> None:
    """Create or Update plan: the list he confirmed for the day, in his order."""
    def f(e):
        e["ids"] = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
        e["dropped"] = [i for i in e["dropped"] if i not in e["ids"]]
    change(day, f)


def add(day: dt.date, task_id: str) -> None:
    def f(e):
        if task_id not in e["ids"]:
            e["ids"].append(task_id)
        e["dropped"] = [i for i in e["dropped"] if i != task_id]
        e["dismissed"] = [i for i in e["dismissed"] if i != task_id]
    change(day, f)


def drop(day: dt.date, task_id: str) -> None:
    """Out of the day's Do; the task itself stays open, back in the pool."""
    def f(e):
        e["ids"] = [i for i in e["ids"] if i != task_id]
        e["carried"].pop(task_id, None)
        if task_id not in e["dropped"]:
            e["dropped"].append(task_id)
    change(day, f)


def keep(day: dt.date, task_id: str) -> None:
    """Keep after 3 days carried: the count starts again today."""
    def f(e):
        if task_id in e["carried"]:
            e["carried"][task_id] = day.isoformat()
        e["kept"][task_id] = day.isoformat()
    change(day, f)


def dismiss(day: dt.date, task_id: str) -> None:
    def f(e):
        if task_id not in e["dismissed"]:
            e["dismissed"].append(task_id)
    change(day, f)


_WORD = re.compile(r"[a-zà-ÿ0-9]{4,}")
_STOP = {"with", "from", "that", "this", "your", "into", "about", "book", "send", "reply", "check",
         "make", "call", "plan", "batch", "block", "time", "today", "tomorrow", "before", "after",
         "prep", "class", "readings", "reading", "notes", "paper", "final", "work", "project", "leaving"}
_TASK = re.compile(r"Task:\s*obsidian://open\?[^\s]*?file=([^&\s]+)")


def _words(t: str) -> set:
    return {w for w in _WORD.findall(t.lower()) if w not in _STOP}


def block_for(title: str, events: list[dict]) -> dict | None:
    """The calendar block booked for a task, best evidence first: the block's Task: link names
    the task's note; else the block names the task in its title or description (Claude lists a
    batch's tasks there); else the block's title shares two of its words (one for a one-word
    task). Earliest wins within a level. Nov 2: word overlap with descriptions put a readings
    task and a planning task into another course's readings block, so overlap is title only."""
    t, tw = title.lower(), _words(title)
    best, rank = None, 9
    for e in events:
        if e.get("allday"):
            continue
        desc = e.get("desc", "") or ""
        files = {urllib.parse.unquote(m).rsplit("/", 1)[-1].lower() for m in _TASK.findall(desc)}
        hay = f"{e.get('title', '')}\n{desc}".lower()
        shared = tw & _words(e.get("title", ""))
        r = (0 if t in files else 1 if (len(t) >= 6 and t in hay)
             else 2 if (len(shared) >= 2 or (len(tw) == 1 and shared)) else 9)
        if r < rank or (r == rank and r < 9 and e["s"] < best["s"]):
            best, rank = e, r
    return {"s": best["s"], "f": best["f"], "title": best.get("title", "")} if best else None


def view(day: dt.date, rows_by_id: dict, closed: list[dict], dated: list[dict], top: list[dict],
         events: list[dict] | None, note, make_row, label, selected: list[str] = ()) -> dict:
    """What the Do tab shows for the day."""
    alive = set(rows_by_id)
    e = ensure(day, alive, note)
    closed_by = {r["id"]: r for r in closed}
    order = list(e["carried"]) + e["ids"] + [i for i in selected if isinstance(i, str)] + [r["id"] for r in dated]
    seen, open_rows, done_rows, moved = set(), [], [], []
    for n, i in enumerate(dict.fromkeys(order)):
        if i in e["dropped"] or i in seen:
            continue
        seen.add(i)
        st = note(i)
        if st is not None and st.get("status") in ("done", "cancelled"):
            r = closed_by.get(i) or make_row(i)
            if r:
                done_rows.append({**r, "do_state": st.get("status")})
            continue
        if st is not None and st.get("scheduled") and st["scheduled"] > day.isoformat() \
                and not (st.get("due") and st["due"] <= day.isoformat()):
            moved.append({"id": i, "title": (rows_by_id.get(i) or {}).get("title") or i,
                          "to": label(dt.date.fromisoformat(st["scheduled"][:10]))})
            continue
        r = rows_by_id.get(i) or make_row(i)
        if not r:
            continue
        r = dict(r)
        since = e["carried"].get(i)
        if since:
            r["carried_from"] = label(dt.date.fromisoformat(since))
            days = (day - dt.date.fromisoformat(since)).days
            r["carry_ask"] = days >= ASK_AFTER
        r["blk"] = block_for(r.get("title") or i, events or [])
        r["do_n"] = n
        open_rows.append(r)
    open_rows.sort(key=lambda r: (0 if r["blk"] else 1, r["blk"]["s"] if r["blk"] else 0,
                                  0 if r.get("carried_from") else 1, r["do_n"]))
    # Something new or urgent the list does not have yet comes as a suggestion, never inserted.
    suggest = []
    if seen:
        for r in top:
            if not r or r["id"] in seen or r["id"] in e["dismissed"]:
                continue
            due = r.get("due") and r["due"] <= day.isoformat()
            if due or r.get("urgent") or r.get("update") or r.get("load", 0) >= 3:
                why = ("Due " + label(day) if due else r.get("update") or
                       ("Urgent" if r.get("urgent") else "On your mind in the meditation"))
                suggest.append({**r, "suggest_why": re.sub(r"^Update \d{1,2}:\d{2}:\s*", "", str(why))[:140]})
            if len(suggest) >= SUGGEST_MAX:
                break
    return {"date": day.isoformat(), "label": label(day), "rows": open_rows, "done": done_rows,
            "moved": moved, "suggest": suggest, "picks": [r for r in top if r][:3] if not open_rows and not done_rows else [],
            "committed": bool(e["ids"])}
