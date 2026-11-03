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
"kept", "dated"}}}. A day is written once on first sight (the carry-over), then by his taps, and
when a dated task first shows ("dated", so a later move leaves a Moved line).
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
NUDGE_AFTER = 3        # waiting this many days: the Waiting fold offers a nudge
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
    for k, empty in (("ids", []), ("carried", {}), ("dropped", []), ("dismissed", []), ("kept", {}), ("dated", []),
                     ("chunks", {}), ("free_done", [])):
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


def commit(day: dt.date, ids: list[str], chunks: dict | None = None) -> None:
    """Create or Update plan: the list he confirmed for the day, in his order, and for each task
    today's chunk, the steps agreed for this sitting (Nov 3, the owner: "the next steps that were
    agreed to in one chunk should stay together")."""
    def f(e):
        e["ids"] = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
        e["dropped"] = [i for i in e["dropped"] if i not in e["ids"]]
        for i, steps in (chunks or {}).items():
            if i in e["ids"] and isinstance(steps, list):
                e["chunks"][i] = [str(s)[:600] for s in steps if str(s).strip()][:8]
    change(day, f)


def free_done(day: dt.date, row_id: str, title: str) -> None:
    """Done on a card with no task note (a message row): remembered for the day, so it shows
    in Done and Progress today instead of vanishing (Nov 3, the owner: "things that i move to
    done not show up in done")."""
    def f(e):
        if not any(x.get("id") == row_id for x in e["free_done"]):
            e["free_done"].append({"id": row_id, "title": (title or row_id)[:200]})
    change(day, f)


def free_done_list(day: dt.date) -> list[dict]:
    e = _load()["days"].get(day.isoformat())
    return [x for x in (e or {}).get("free_done", []) if isinstance(x, dict) and x.get("id")]


def more(day: dt.date, task_id: str, step: str) -> None:
    """Keep going: the task's next step joins today's chunk."""
    def f(e):
        c = e["chunks"].setdefault(task_id, [])
        if step and _norm(step) not in {_norm(x) for x in c}:
            c.append(step[:600])
        if task_id not in e["ids"] and task_id not in e["carried"]:
            e["ids"].append(task_id)
    change(day, f)


_EST = re.compile(r"\s*\((\d+)\s*min\)\s*$")


def _norm(s: str) -> str:
    s = re.sub(r"^(?:Next|Then):\s*", "", str(s or "").strip())
    s = re.sub(r"\s*\(due [^)]*\)", "", _EST.sub("", s))   # a deadline marker added later is the same step
    return " ".join(s.split()).lower()


def _est(s: str) -> int | None:
    m = _EST.search(str(s or ""))
    return int(m.group(1)) if m else None


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


_TASK = re.compile(r"Task:\s*obsidian://open\?[^\s]*?file=([^&\s]+)")


def block_for(task_id: str, tid: str, events: list[dict], ekey: str = "") -> dict | None:
    """The calendar block booked for a task, by key only (owner decision, Nov 3: "name matching is
    stupid for tasks"): a block names its tasks as `Task: <link> (t-xxxxxx)`, one line per
    task, so a batch holds several. The ID wins; a block written before IDs matches on the
    note its link names. An appointment the task is about (a doctor's visit, a Partiful
    invite) has no Task: line: the note's `event_key` names it instead. Nothing matches on words: a block without either belongs to no task.
    Earliest block wins."""
    best = None
    name = (task_id or "").lower()
    for e in events:
        if e.get("allday"):
            continue
        ids = set(e.get("task_ids") or [])
        files = {f.lower() for f in e.get("tasks") or []}
        desc = e.get("desc", "") or ""
        files |= {urllib.parse.unquote(m).rsplit("/", 1)[-1].lower() for m in _TASK.findall(desc)}
        if (tid and tid in ids) or (name and name in files) or (ekey and e.get("key") == ekey):
            if best is None or e["s"] < best["s"]:
                best = e
    return {"s": best["s"], "f": best["f"], "title": best.get("title", "")} if best else None


def view(day: dt.date, rows_by_id: dict, closed: list[dict], dated: list[dict], top: list[dict],
         events: list[dict] | None, note, make_row, label, selected: list[str] = ()) -> dict:
    """What the Do tab shows for the day."""
    alive = set(rows_by_id)
    e = ensure(day, alive, note)
    closed_by = {r["id"]: r for r in closed}
    # Every task Do showed today because of its date is remembered (Nov 3): one whose date
    # then moves keeps a "Moved to ... · Undo" line instead of vanishing (Skip in Replies once
    # moved the sports medicine task to tomorrow and Do dropped it without a trace).
    now_dated = [r["id"] for r in dated]
    order = list(e["carried"]) + e["ids"] + [i for i in selected if isinstance(i, str)] + now_dated + e["dated"]
    new_dated = [i for i in dict.fromkeys(now_dated) if i not in e["dated"]]
    if new_dated:
        change(day, lambda x: x["dated"].extend(i for i in new_dated if i not in x["dated"]))
    seen, open_rows, done_rows, moved, waiting = set(), [], [], [], []
    part_done, new_chunks = [], {}
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
        # Waiting (Nov 3): blocked on someone and nothing of his own open, so out of the active
        # list into the Waiting fold, until the pre-run sees the answer (wait_over).
        if st is not None and (st.get("waiting") or st.get("wait_line")) and not st.get("next"):
            if not r.get("wait_over"):
                ws = st.get("wait_since") or ""
                days = (day - dt.date.fromisoformat(ws)).days if ws else 0
                waiting.append({"id": i, "title": r.get("title") or i, "who": st.get("wait_line", ""),
                                "since": label(dt.date.fromisoformat(ws)) if ws else "",
                                "nudge": days >= NUDGE_AFTER})
                continue
            r["wait_back"] = True
        # Today's chunk (Nov 3): the steps agreed for today. A task planned without one has its
        # Next step at first sight as its chunk. When none of the chunk is open any more and a
        # step was done today, today's part is done: the card leaves for the Done fold, saying so,
        # with Keep going. A chunk whose steps were reworded (none open, nothing done) restarts.
        if st is not None and st.get("next"):
            opens = [_norm(st["next"])] + [_norm(s) for s in st.get("steps") or []]
            chunk = e["chunks"].get(i) or []
            left = [c for c in chunk if _norm(c) in opens]
            # No chunk recorded (planned before chunks, or added with +) but a step already done
            # today: that step was today's part.
            if not left and st.get("done_today"):
                part_done.append({**r, "do_state": "part", "part_next": _EST.sub("", st["next"]).strip(),
                                  "part_next_est": _est(st["next"])})
                continue
            if not left:
                chunk = left = [st["next"]]
                new_chunks[i] = chunk
            r["chunk"] = {"left": len(left), "total": len(chunk), "steps": left,
                          "min": sum(_est(c) or 0 for c in left), "unest": sum(_est(c) is None for c in left)}
        since = e["carried"].get(i)
        if since:
            r["carried_from"] = label(dt.date.fromisoformat(since))
            days = (day - dt.date.fromisoformat(since)).days
            r["carry_ask"] = days >= ASK_AFTER
        r["blk"] = block_for(i, (st or {}).get("task_id", ""), events or [], (st or {}).get("event_key", ""))
        r["do_n"] = n
        open_rows.append(r)
    # Done holds everything closed today, not only what was on this list (Nov 3): a task closed
    # in Plan, the chat or Obsidian, and a card with no note he tapped Done on.
    shown = {r["id"] for r in open_rows + done_rows + part_done + waiting + moved}
    for r in closed:
        if r.get("closed") == day.isoformat() and r["id"] not in shown:
            shown.add(r["id"])
            done_rows.append({**r, "do_state": "done"})
    for x in e["free_done"]:
        if isinstance(x, dict) and x.get("id") and x["id"] not in shown:
            shown.add(x["id"])
            done_rows.append({"id": x["id"], "task": False, "title": x.get("title") or x["id"], "group": "Done",
                              "prio": "Z", "guess": "done", "next": "Done today", "why": "", "meta": "",
                              "waiting": "", "est": "", "due": "", "anx": False, "urgent": False, "claude": [],
                              "prep": [], "draft": None, "later": False, "tab": "today",
                              "closed": day.isoformat(), "detail": {}, "do_state": "done"})
    if new_chunks:
        def keep(x):
            x["chunks"].update(new_chunks)
        change(day, keep)
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
            "moved": moved, "suggest": suggest, "waiting": waiting, "part_done": part_done,
            "picks": [r for r in top if r][:3] if not open_rows and not done_rows and not waiting and not part_done else [],
            "committed": bool(e["ids"])}
