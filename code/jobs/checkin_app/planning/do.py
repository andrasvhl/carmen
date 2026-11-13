"""Do: the day he executes from (owner decision, Nov 1: "we need to split ToDo into Select and then
the actual to do from where you execute ... ToDo switches sometimes, whereas we need like an
execution page").

One list per day, his commitment for it, which no re-ranking touches:
  - what he committed in Plan (Commit), selected for the day there, or added on Do,
  - what an earlier day left open, only once he adds it from Left open (one carry rule, Nov 9).
A task scheduled or due that day joins only through Plan (Nov 6); one due comes as a New for today
suggestion. Ordered by the calendar block booked for it, then his order. Today's part is frozen at
commit (D-7): Book rewrites it (D-8), and a new Next or a step newly due that day joins with an
"Added after your plan" line. A finished card (its committed steps met) stays in place, lying flat
with Clear, until he clears it; nothing leaves the list but his Clear. A task whose date moved past
the day leaves with a "Moved to ..." line. The day turns at 4:00, so a late evening is still that day.

State: checkin_app/do.json, {"days": {"YYYY-MM-DD": {"ids", "carried", "dropped", "dismissed",
"kept", "dated"}}}. A day is written once on first sight (the carry-over), then by his taps.
"dated" is no longer written (Nov 6: a dated task joins Do only through Plan); a committed task
whose date moves past the day still leaves a Moved line.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse

from runner import config
from tools import steps as S   # the one place for step-line facts (Nov 11)

from ..adapters import state
from ..adapters import clock

STATE = config.STATE_DIR / "checkin_app" / "do.json"
ASK_AFTER = 3          # carried this many days in a row: Keep, Move or Drop
# D-38
from tools.backsched import NUDGE_AFTER   # noqa: E402 - one owner, tools/backsched


def _nudge(st: dict, days: int) -> bool:
    """The Waiting fold's Nudge (D-38): a wait's chase when the backward pass has one (flag
    backsched: due by its slack or the party's usual time), else after NUDGE_AFTER days."""
    cs = [w["chase_due"] for w in st.get("waits") or [] if "chase_due" in w]
    return any(cs) if cs else days >= NUDGE_AFTER
KEEP_DAYS = 14         # days of history kept in do.json
SUGGEST_MAX = 3


def _load() -> dict:
    d = state.read(STATE, {})
    if not isinstance(d.get("days"), dict):
        return {"days": {}}
    return d


def _prune(d: dict, day: dt.date) -> dict:
    """Keep KEEP_DAYS of history before the real current day, and every later day. Pruning
    around the day a write named (a test committing to day +40) used to delete today."""
    from zoneinfo import ZoneInfo
    cut = (clock.do_day(dt.datetime.now(ZoneInfo("America/Los_Angeles"))) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    days = d.get("days") if isinstance(d.get("days"), dict) else {}
    d["days"] = {k: v for k, v in days.items() if k >= cut}
    return d


def _update(day: dt.date, fn) -> dict:
    """The one way do.json changes (state.update: locked across processes, Nov 6)."""
    def f(d):
        if not isinstance(d.get("days"), dict):
            d.clear()
            d["days"] = {}
        fn(d)
        return _prune(d, day)
    return state.update(STATE, f, {})


def _day(d: dict, day: dt.date) -> dict:
    e = d["days"].get(day.isoformat())
    if not isinstance(e, dict):
        e = {}
    for k, empty in (("ids", []), ("carried", {}), ("dropped", []), ("dismissed", []), ("kept", {}), ("dated", []),
                     ("chunks", {}), ("chunk_ids", {}), ("claude_ticks", {}), ("part_closed", []),
                     ("claude_moved", {}), ("extra", {}), ("his_ticks", {}), ("claude_changes", {}), ("cleared", []),
                     ("moved_off", {}), ("block_steps", []), ("added_after", {}), ("at_commit", {})):
        if not isinstance(e.get(k), type(empty)):
            e[k] = type(empty)()
    return e


def _open(task_id: str, day: dt.date, alive: set, note) -> bool:
    """Still his to do on that day: an open note not moved past it. A card whose note is gone
    (archived) is still in the view only through a pre-run row, so the view decides."""
    st = note(task_id)
    if st is None:
        return task_id in alive
    if st.get("status") in ("done", "cancelled"):
        return False
    from ..tasks import read as _tasks
    return not _tasks.is_later(st, day)   # one "later" rule (audit A9)


def _today_left(p: dict, i: str, note) -> bool:
    """One carry rule (Nov 9, the four-role review): a task carries to the next day only while one
    of the steps it had for that day is still open. A finished part is not "left open"."""
    cids = (p.get("chunk_ids") or {}).get(i)
    st = note(i)
    if not cids or st is None:
        return True
    by = {x["id"]: x for x in st.get("step_states") or [] if x.get("id")}
    return any(x in by and by[x]["state"] == "open" and not _waits(by[x]) for x in cids)


def _rolled(d: dict, day: dt.date, alive: set, note) -> tuple[dict, bool]:
    """The day's entry with the carry-over applied, and whether this call applied it."""
    key = day.isoformat()
    e = _day(d, day)
    if e.get("rolled"):
        return e, False
    prev = sorted(k for k in d["days"] if k < key and (day - dt.date.fromisoformat(k)).days <= 7)
    if prev:
        p = _day(d, dt.date.fromisoformat(prev[-1]))
        for i in list(p["carried"]) + p["ids"]:
            if i in e["carried"] or i in e["ids"] or i in p["dropped"] or not _open(i, day, alive, note):
                continue
            if not _today_left(p, i, note):   # its day's steps were all met: nothing is left open (Nov 9)
                continue
            e["carried"][i] = p["carried"].get(i) or prev[-1]
    e["rolled"] = True
    return e, True


def ensure(day: dt.date, alive: set, note, write: bool = True) -> dict:
    """The day's entry. When the day arrives, what the last committed day left open carries
    over, once ("rolled"): a plan made the evening before already has an entry by then.
    Saved only while persisting (a tap or the server's tick, never a page load), and never from
    a vault whose Inbox shows no notes: carrying over from nothing would mark the day rolled
    with nothing carried, for good (Nov 6: the deploy gate's test did exactly that)."""
    e, new = _rolled(_load(), day, alive, note)
    if new and write and state.persisting() and next(config.INBOX.glob("*.md"), None) is not None:
        def f(d):
            e2, _ = _rolled(d, day, alive, note)
            d["days"][day.isoformat()] = e2
        _update(day, f)
    return e


def turn(day: dt.date, path_of, reestimate, piece, log=None) -> int:
    """The day turn for left-over tasks (Tasks v2 slice 6b, the owner Nov 6): each task carried into
    the day gets its remaining steps estimated again and a fresh proposed piece, instead of
    yesterday's steps and its Next at first sight. Once per task and day ("turned"); the piece is
    what Do takes as today's chunk when he does not plan the task himself. Returns how many."""
    e = _day(_load(), day)
    if not e.get("rolled"):
        return 0
    todo = [i for i in e["carried"] if i not in (e.get("turned") or []) and i not in e["ids"]]
    done, props = [], {}
    for i in todo:
        p = path_of(i)
        if p is None:
            done.append(i)
            continue
        try:
            reestimate(p)
            props[i] = piece(p)
        except Exception as exc:   # noqa: BLE001 - one task's failure never stops the rest
            if log:
                log("error", where="day turn", detail=f"{i}: {exc!r}"[:200])
        done.append(i)
    if done:
        def f(x):
            x.setdefault("turned", []).extend(i for i in done if i not in x.get("turned", []))
            x.setdefault("proposed", {}).update({k: v for k, v in props.items() if v})
        change(day, f)
    return len(done)


def claude_ticked(task_id: str, steps: list[dict], day: dt.date | None = None) -> None:
    """Steps a chat ticked (Nov 5, the owner: a task "moved to immediately done without me having
    reviewed the actual information"). Only his own Done, or a sent message that is the step,
    ends today's part; a step Claude ticked leaves the card on Do with a Review line."""
    from zoneinfo import ZoneInfo
    day = day or clock.do_day(dt.datetime.now(ZoneInfo("America/Los_Angeles")))

    def fn(e):
        have = e["claude_ticks"].setdefault(task_id, [])
        his = set(e["his_ticks"].get(task_id) or [])
        for s in steps:
            if s.get("id") and s["id"] in his:   # his tap, made while the turn ran
                continue
            if (s["id"] or s["text"]) not in {x["id"] or x["text"] for x in have}:
                have.append({"id": s["id"], "text": s["text"]})
    change(day, fn)


def change(day: dt.date, fn) -> dict:
    """Change the day's entry under do.json's lock (his taps, chat ticks)."""
    out = {}

    def f(d):
        e = _day(d, day)
        fn(e)
        d["days"][day.isoformat()] = e
        out["e"] = e
    _update(day, f)
    return out["e"]


def commit(day: dt.date, ids: list[str], chunks: dict | None = None, chunk_ids: dict | None = None,
           outcomes: dict | None = None) -> None:
    """Create or Update plan: the list he confirmed for the day, in his order, and for each task
    today's chunk, the steps agreed for this sitting (Nov 3, the owner: "the next steps that were
    agreed to in one chunk should stay together")."""
    def f(e):
        e["ids"] = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
        e["dropped"] = [i for i in e["dropped"] if i not in e["ids"]]
        for i, steps in (chunks or {}).items():
            if i in e["ids"] and isinstance(steps, list):
                e["chunks"][i] = [str(s)[:600] for s in steps if str(s).strip()][:8]
                e["chunk_ids"].pop(i, None)   # resolved to step ids on the next view
        # Today's chunk by step id (refactor step 1, Nov 5): the page sends the ids, so nothing
        # is resolved from words. The texts above stay for a page loaded before this.
        for i, sids in (chunk_ids or {}).items():
            if i in e["ids"] and isinstance(sids, list):
                keep = [str(s)[:12] for s in sids if isinstance(s, str) and re.fullmatch(r"s[0-9a-f]{4,8}", s)][:8]
                if keep:
                    e["chunk_ids"][i] = keep
        # Each chunk's outcome line (Tasks v2, Nov 6): suggested, his to edit, kept with the commitment.
        for i, oc in (outcomes or {}).items():
            if i in e["ids"] and isinstance(oc, str) and oc.strip():
                e.setdefault("outcomes", {})[i] = " ".join(oc.split())[:140]
    change(day, f)


def mark_commit(day: dt.date, seen: dict) -> None:
    """What each committed task looked like at commit (Nov 11, M1): {task: {"next": id, "due": [ids
    due by that day]}}, so a new Next or a step newly due that day joins the chunk later, said."""
    def f(e):
        for i, v in (seen or {}).items():
            if i in e["ids"] and isinstance(v, dict):
                e["at_commit"][i] = {"next": str(v.get("next") or ""), "due": [str(x) for x in v.get("due") or []][:20]}
                e["added_after"].pop(i, None)
    change(day, f)


def book_chunks(day: dt.date, per_task: dict, blocks: list[dict]) -> None:
    """Booking is the final word (Nov 11, the owner: the chunk on Do must match what Plan booked).
    per_task: {task: {"ids": [step ids in block order], "texts": [...]}} from the booked blocks;
    blocks: [{"task", "s", "f", "title", "sids"}], the block each step is booked in, for the card's
    time. A task booked for the day joins the commitment; what was added after the plan and is now
    booked is part of the plan."""
    def f(e):
        for i, v in per_task.items():
            if i not in e["ids"]:
                e["ids"].append(i)
            e["chunk_ids"][i] = list(v["ids"])[:8]
            e["chunks"][i] = [str(t)[:600] for t in v["texts"]][:8]
            e["added_after"].pop(i, None)
        keep = [b for b in e["block_steps"] if b.get("task") not in per_task]
        e["block_steps"] = keep + [dict(b) for b in blocks]
    change(day, f)


def due_by(x: dict, day: dt.date) -> bool:
    """A step due on or before the day, from its fields or the "(due Nov 11)" in its text."""
    d = x.get("due") or ""
    if not d:
        m = S.DUE_OLD.search(x.get("text") or "")
        d = S.month_day(m.group(1), m.group(2), day) if m else ""
    return bool(d) and d <= day.isoformat()


# D-7
def _after_plan(e: dict, i: str, st: dict | None, cids: list, day: dt.date) -> list[str]:
    """Steps that came up after the commit and belong to today (Nov 11, M1): the Next when it is
    a new one, and any step newly due by the day, open and free, not already in the chunk. Only
    for a task whose commit was recorded (mark_commit)."""
    seen = e["at_commit"].get(i)
    if not seen or st is None or st.get("status") in ("done", "cancelled"):
        return []
    by = {x.get("id"): x for x in st.get("step_states") or [] if x.get("id")}
    # Only while the part is still open: his own Done promotes the next step, and a finished part
    # stays finished (Nov 9: Keep going works beyond the plan, it never reopens Today).
    if not any(by.get(c, {}).get("state") == "open" and not _waits(by.get(c, {})) for c in cids):
        return []
    old_next_open = by.get(seen.get("next"), {}).get("state") == "open"
    blk = set(st.get("blocked") or [])
    out = []
    for x in st.get("step_states") or []:
        sid = x.get("id")
        if not sid or sid in cids or x.get("state") != "open" or x.get("kind") == "Waiting" or sid in blk:
            continue
        new_next = sid == st.get("next_id") and sid != seen.get("next") and old_next_open
        new_due = due_by(x, day) and sid not in seen.get("due", [])
        if new_next or new_due:
            out.append(sid)
    return out


# D-8
def live_blocks(e: dict, i: str, events: list[dict] | None) -> list[dict]:
    """The task's booked blocks with their live times (Nov 11, the owner on the per-step times: "i just
    want to make sure this is robust"): a block stored with its event key takes the event's time
    now, so a block he dragged in Google Calendar moves on the card; one he deleted drops out (its
    steps stay in the chunk). Without a readable calendar, or for a block booked before keys were
    stored, the time saved at Book."""
    out = []
    for b in e["block_steps"]:
        if b.get("task") != i:
            continue
        key = b.get("key")
        if key and events is not None:
            ev = next((x for x in events if x.get("key") == key and not x.get("allday")), None)
            if ev is None:
                continue   # deleted in the calendar
            b = {**b, "s": ev["s"], "f": ev["f"]}
        out.append(b)
    return sorted(out, key=lambda b: b.get("s", 0))


def booked_parts(e: dict, i: str, by_sid: dict, events: list[dict] | None = None) -> dict:
    """{step id: minutes} for a step booked alone in a block shorter than its estimate: today's
    minutes for it are the block's length, a part (Nov 11, M1: the 2 h the e-bike step booked in a 1 h
    block counted 120 min on the card and 2 h in the bar)."""
    out = {}
    for b in live_blocks(e, i, events):
        sids = b.get("sids") or []
        if len(sids) == 1 and sids[0] in by_sid:
            m, length = _mins(by_sid[sids[0]]), b["f"] - b["s"]
            if m is not None and 0 < length < m:
                out[sids[0]] = out.get(sids[0], 0) + length
    return out


def booked_minutes(day: dt.date, note, events: list[dict] | None = None) -> dict:
    """{task: today's booked minutes} for tasks booked with step ids: each committed step's
    estimate, a step booked as a part counting its block (booked_parts). For Plan's budget bar."""
    e = _day(_load(), day)
    out = {}
    for i in {b.get("task") for b in e["block_steps"] if b.get("task")}:
        st = note(i)
        by = {x["id"]: x for x in (st or {}).get("step_states") or [] if x.get("id")}
        parts = booked_parts(e, i, by, events)
        ids = [x for x in e["chunk_ids"].get(i) or [] if x in by and by[x]["state"] == "open"]
        if ids:
            out[i] = sum(parts.get(x, _mins(by[x]) or 0) for x in ids)
    return out


def _booked_blk(e: dict, i: str, known: list[dict], met, events: list[dict] | None = None) -> dict | None:
    """The card's time (Nov 11, M1): the booked block that holds the first open step of today's
    chunk; when all are met, the last one. None when no block was booked with step ids for it.
    Live times (live_blocks)."""
    bs = live_blocks(e, i, events)
    if not bs:
        return None
    first = next((x.get("id") for x in known if x.get("state") == "open" and not met(x)), None)
    hit = next((b for b in bs if first and first in (b.get("sids") or [])), None) or (bs[-1] if first is None else None)
    return {"s": hit["s"], "f": hit["f"], "title": hit.get("title", "")} if hit else None


def uncommit(day: dt.date, task_id: str) -> dict | None:
    """One day at a time (Nov 10, r3b): selecting a task for the other day takes it out of this
    day's commitment. Returns what was there, for recommit when he takes it out again."""
    if task_id not in _day(_load(), day)["ids"]:
        return None
    out = {}

    def f(e):
        if task_id in e["ids"]:
            out["v"] = {"pos": e["ids"].index(task_id), "chunks": e["chunks"].get(task_id),
                        "chunk_ids": e["chunk_ids"].get(task_id)}
            e["ids"].remove(task_id)
            e["chunks"].pop(task_id, None)
            e["chunk_ids"].pop(task_id, None)
    change(day, f)
    return out.get("v")


def mark_moved(day: dt.date, task_id: str, to: str | None, sel: bool = False) -> None:
    """He picked a task on this day's list for another day (Nov 10, r7): it stays on the list as
    "Moved to Wed · Undo", even when it is due that day. to=None clears it (Take out, Undo).
    `sel`: it was on the list from the day's selection only, not committed (r8 F1)."""
    e0 = _day(_load(), day)
    if (to is None and task_id not in e0["moved_off"]) or (to is not None and task_id not in e0["ids"] and not sel):
        return

    def f(e):
        if to is None:
            e["moved_off"].pop(task_id, None)
        elif task_id in e["ids"] or sel:
            e["moved_off"][task_id] = to
    change(day, f)


def committed(day: dt.date, task_id: str) -> bool:
    """On the day's committed list (not only selected)."""
    return task_id in _day(_load(), day)["ids"]


def moved_from_selection(day: dt.date, task_id: str) -> bool:
    """Moved off the day's list while it was there from the selection alone (r8 F1)."""
    e = _day(_load(), day)
    return task_id in e["moved_off"] and task_id not in e["ids"]


def recommit(day: dt.date, task_id: str, saved: dict) -> None:
    """Put back what uncommit took out, in its place."""
    def f(e):
        if task_id in e["ids"]:
            return
        e["ids"].insert(min(int(saved.get("pos") or 0), len(e["ids"])), task_id)
        if saved.get("chunks"):
            e["chunks"][task_id] = saved["chunks"]
        if saved.get("chunk_ids"):
            e["chunk_ids"][task_id] = saved["chunk_ids"]
    change(day, f)


def set_outcome(day: dt.date, task_id: str, text: str) -> None:
    """His own words for today's outcome of a task; empty takes the suggestion back."""
    def f(e):
        oc = e.setdefault("outcomes", {})
        if text.strip():
            oc[task_id] = " ".join(text.split())[:140]
        else:
            oc.pop(task_id, None)
    change(day, f)


_FIND = re.compile(r"^(?:find out|figure out|work out|check|look up|research)\s+(.+)$", re.I)


def suggest_outcome(steps: list[str]) -> str:
    """The outcome a chunk reaches, from its last step: a finding-out step is knowing what it
    finds ("Find out what HR clearance needs" gives "Know what HR clearance needs"), any other
    step is that step done."""
    if not steps:
        return ""
    t = S.strip_marks(_norm_keep_case(steps[-1]), due=False, ids=False)
    m = _FIND.match(t)
    if m:
        return "Know " + m.group(1)
    return t + (" done" if len(steps) == 1 else ", and the steps before it")


def _norm_keep_case(s: str) -> str:
    s = re.sub(r"^(?:Next|Then):\s*", "", str(s or "").strip())
    return " ".join(S.DUE_OLD.sub("", s).split())


def _mins(x: dict) -> int | None:
    """A step's planned minutes: its estimate, else its timebox (step_states with v2 fields)."""
    if x.get("min") is not None:
        return x["min"]
    if x.get("box"):
        return x["box"]
    return S.est_min(x.get("text", ""))


def more(day: dt.date, task_id: str, step: str, sid: str = "") -> None:
    """Keep going (Nov 9, the owner: "Today should keep the tasks that were required for today and i
    simply have the option to continue with next steps"): the next step is worked beyond the plan.
    It goes to the day's `extra`, never into the committed chunk, so Today stays what he committed
    and a finished Today stays finished. A note without step ids keeps the old join."""
    def f(e):
        e["part_closed"] = [x for x in e["part_closed"] if x != task_id]   # a new piece asks again
        if sid:
            have = e["extra"].setdefault(task_id, [])
            if sid not in have and sid not in (e["chunk_ids"].get(task_id) or []):
                have.append(sid)
        else:
            c = e["chunks"].setdefault(task_id, [])
            if step and _norm(step) not in {_norm(x) for x in c}:
                c.append(step[:600])
            e["chunk_ids"].pop(task_id, None)
        if task_id not in e["ids"] and task_id not in e["carried"]:
            e["ids"].append(task_id)
    change(day, f)


def his_ticked(day: dt.date, task_id: str, sids: list[str]) -> bool:
    """Steps his own tap ticked (Nov 9): a chat turn running at the same moment saw the flip and
    logged it as Claude's, so today's part never closed. Returns whether any of them lies beyond
    today's committed chunk (the `beyond_plan` flag on the tap's signal)."""
    sids = [x for x in sids if x]
    if not sids:
        return False
    out = {"beyond": False}

    def f(e):
        have = e["his_ticks"].setdefault(task_id, [])
        have.extend(x for x in sids if x not in have)
        e["claude_ticks"][task_id] = [x for x in e["claude_ticks"].get(task_id, []) if x.get("id") not in sids]
        if not e["claude_ticks"][task_id]:
            e["claude_ticks"].pop(task_id)
        cids = e["chunk_ids"].get(task_id)
        out["beyond"] = bool(cids) and any(x not in cids for x in sids)
    change(day, f)
    return out["beyond"]


def unticked(day: dt.date, task_id: str, sid: str) -> None:
    """A step he unticked is neither his tick nor Claude's any more."""
    def f(e):
        if task_id in e["his_ticks"]:
            e["his_ticks"][task_id] = [x for x in e["his_ticks"][task_id] if x != sid]
        if task_id in e["claude_ticks"]:
            e["claude_ticks"][task_id] = [x for x in e["claude_ticks"][task_id] if x.get("id") != sid]
    change(day, f)


def committed(day: dt.date, task_id: str) -> list[str]:
    """Today's committed step ids for a task (read only), for the chat's protection rule."""
    e = ((state.read(STATE, {}) or {}).get("days") or {}).get(day.isoformat()) or {}
    return list((e.get("chunk_ids") or {}).get(task_id) or [])


def claude_changed(task_id: str, items: list[dict], block: str, day: dt.date | None = None) -> None:
    """What a chat turn changed in the steps (Nov 9, the owner: visible in the open card, not on the
    Do card): "Claude changed N steps · Review / Undo". `block` is the Next action block before
    the day's first such turn, which Undo puts back."""
    from zoneinfo import ZoneInfo
    day = day or clock.do_day(dt.datetime.now(ZoneInfo("America/Los_Angeles")))

    def f(e):
        c = e["claude_changes"].get(task_id) or {"items": [], "block": block}
        c["items"] = (c["items"] + items)[-30:]
        c["at"] = dt.datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M")
        e["claude_changes"][task_id] = c
    change(day, f)


def clear_claude_changes(day: dt.date, task_id: str) -> dict | None:
    """OK or Undo on the trail: it goes; returns what was there."""
    out = {}

    def f(e):
        out["c"] = e["claude_changes"].pop(task_id, None)
    change(day, f)
    return out.get("c")




def _norm(s: str) -> str:
    s = re.sub(r"^(?:Next|Then):\s*", "", str(s or "").strip())
    s = S.strip_marks(s, ids=False)   # a deadline marker added later is the same step
    return " ".join(s.split()).lower()


def _est(s: str) -> int | None:
    return S.est_min(str(s or ""))


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


# D-32
def clear(day: dt.date, ids: list[str]) -> None:
    """Clear (Nov 9, the owner: "Basically it means im not working on this today anymore right? And
    this should not mark any tasks as done"): the cards leave today's list for the Cleared fold.
    Membership only: nothing in the note changes, no step is ticked or unticked."""
    def f(e):
        e["cleared"].extend(x for x in ids if x not in e["cleared"])
    change(day, f)


def back(day: dt.date, ids: list[str]) -> None:
    """Back to today: a cleared card returns to its place on the list, its steps as they are."""
    def f(e):
        e["cleared"] = [x for x in e["cleared"] if x not in ids]
        e["part_closed"] = [x for x in e["part_closed"] if x not in ids]
        for x in ids:
            if x not in e["ids"]:
                e["ids"].append(x)
    change(day, f)


def part_close(day: dt.date, task_id: str) -> None:
    """His tap that takes a finished card off today (the do_partclose action; it counts as Clear).
    Nov 9, the owner: a card whose chunk is all ticked "automatically is moved out of today which is
    super confusing. This should take a confirmation click". Since the card model (Nov 9 to 10) the
    finished card lies flat with Clear until he taps it; there is no Done for today question."""
    def f(e):
        if task_id not in e["part_closed"]:
            e["part_closed"].append(task_id)
    change(day, f)


# D-31
def claude_moved_to(task_id: str, old: str, new: str, day: dt.date | None = None) -> bool:
    """A chat moved the task's date past today (Nov 9: the HR chat moved it to Nov 10 and the card
    left Do). That only proposes: the card stays with "Claude moved this to ... · OK · Keep today".
    Returns whether it was recorded."""
    from zoneinfo import ZoneInfo
    day = day or clock.do_day(dt.datetime.now(ZoneInfo("America/Los_Angeles")))
    today, new, old = day.isoformat(), str(new or "")[:10], str(old or "")[:10]
    if not new or new <= today or (old and old > today):
        return False

    def f(e):
        e["claude_moved"][task_id] = new
    change(day, f)
    return True


def clear_claude_move(day: dt.date, task_id: str) -> None:
    """OK (the move stands, the card leaves with its Moved line) or Keep today."""
    def f(e):
        e["claude_moved"].pop(task_id, None)
    change(day, f)


_TASK = re.compile(r"Task:\s*obsidian://open\?[^\s]*?file=([^&\s]+)")


# D-24
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


def _waits(x: dict) -> bool:
    """A committed step a chat turned into a Waiting line (Nov 9, the FNV case: "Claim a section
    from Carl and Mira" became "Waiting: Carl and Mira ..." under the same id). His part
    is done, the message went out: for today's chunk it counts as met, shown as waiting."""
    return x.get("kind") == "Waiting" and x.get("state") == "open"


def _plan(steps: list[dict], met, claude: set | None = None) -> list[dict]:
    """Today's committed steps with their state, for the Do card (Tasks v2): done, open or dropped.
    With `claude` (the ids a chat ticked), a done step says whether Claude did it."""
    return [{"id": x.get("id", ""), "text": x.get("text", ""),
             "state": "waiting" if _waits(x) else "done" if met(x) else ("dropped" if x.get("state") == "struck" else "open"),
             **({"claude": True} if claude and met(x) and x.get("id") in claude else {})} for x in steps]


# D-23
def view(day: dt.date, rows_by_id: dict, closed: list[dict], dated: list[dict], top: list[dict],
         events: list[dict] | None, note, make_row, label, selected: list[str] = (),
         elsewhere: set | frozenset = frozenset()) -> dict:
    """What the Do tab shows for the day. `dated` (tasks scheduled or due that day) no longer
    joins the list (Nov 6, the owner: "Left over yes, but the others should come via plan i think
    since i need to account for their time when budgeting"): Do holds what he committed or added
    and what an earlier day left open; a dated task waits in Plan's selection, and one due today
    comes as a New for today suggestion. The parameter stays for the callers' positions.
    `elsewhere`: ids selected for a later day, never offered as New for today (r8 F1)."""
    alive = set(rows_by_id)
    e = ensure(day, alive, note)
    from ..adapters import flags   # Tasks v2 (Nov 6)
    frozen, plan_on = flags.on("frozen_chunk"), flags.on("do_plan")
    closed_by = {r["id"]: r for r in closed}
    # The plan he committed today orders the list (Nov 4: "the plan that was created is not
    # reflected in the ordering"): his ids first, then what is only carried. Before any commit
    # today, the carried tasks lead as before.
    order = (e["ids"] + list(e["carried"]) if e["ids"] else list(e["carried"])) \
        + [i for i in selected if isinstance(i, str)] + list(e["moved_off"])
    seen, open_rows, done_rows, moved, waiting, left_open = set(), [], [], [], [], []
    # Left open (Nov 9, the four-role review): a task only carried over is never on the list. It
    # waits in the Left open fold (and Plan's) until he adds it, so Do shows exactly what he
    # committed ("Today must be what he committed", owner decision, Nov 9).
    # A task moved off the list from the selection alone keeps its Moved line too (r8 F1).
    picked = set(e["ids"]) | {i for i in selected if isinstance(i, str)}
    moved_only = set(e["moved_off"]) - picked
    picked |= moved_only
    part_done, cleared, new_chunks, new_chunk_ids, new_after = [], [], {}, {}, {}
    # Clear (Nov 9) is the only way off today's list: his tap. part_closed (Done for today, before)
    # counts as cleared.
    gone = set(e["cleared"]) | set(e["part_closed"])
    for n, i in enumerate(dict.fromkeys(order)):
        if i in e["dropped"] or i in seen:
            continue
        seen.add(i)
        st = note(i)
        is_closed = st is not None and st.get("status") in ("done", "cancelled")
        if i in moved_only and (is_closed or st is None or (st.get("scheduled") or "")[:10] != e["moved_off"][i]):
            continue   # the move no longer stands; it was never committed here
        if i not in picked:
            # Only carried: the Left open fold (or Waiting, or Done when it closed meanwhile).
            if is_closed:
                r = closed_by.get(i) or make_row(i)
                if r:
                    done_rows.append({**r, "do_state": st.get("status")})
                continue
            r = rows_by_id.get(i) or make_row(i)
            if not r:
                continue
            r = dict(r)
            # Waiting by the one rule (Nov 10): no free step of his left, the same as Plan's group.
            if st is not None and st.get("task_waiting") and not r.get("wait_over"):
                ws = st.get("wait_since") or ""
                days = (day - dt.date.fromisoformat(ws)).days if ws else 0
                waiting.append({"id": i, "title": r.get("title") or i, "who": st.get("wait_line", ""),
                                "since": label(dt.date.fromisoformat(ws)) if ws else "",
                                "nudge": _nudge(st, days)})
                continue
            since = e["carried"].get(i)
            if since:
                r["carried_from"] = label(dt.date.fromisoformat(since))
                r["carry_ask"] = (day - dt.date.fromisoformat(since)).days >= ASK_AFTER
            r["do_n"] = n
            left_open.append(r)
            continue
        cm = None
        mo = e["moved_off"].get(i)
        if mo and not is_closed and st is not None and (st.get("scheduled") or "")[:10] == mo:
            # Picked for another day (Nov 10): a Moved line with Undo, due that day or not.
            moved.append({"id": i, "title": (rows_by_id.get(i) or {}).get("title") or i,
                          "to": label(dt.date.fromisoformat(mo))})
            continue
        from ..tasks import read as _tasks
        if not is_closed and st is not None and _tasks.is_later(st, day):
            # A move a chat made stays on the list (Nov 9: membership is his); his own move leaves
            # with a Moved line and Undo.
            if e["claude_moved"].get(i) == st["scheduled"][:10]:
                cm = label(dt.date.fromisoformat(st["scheduled"][:10]))
            else:
                moved.append({"id": i, "title": (rows_by_id.get(i) or {}).get("title") or i,
                              "to": label(dt.date.fromisoformat(st["scheduled"][:10]))})
                continue
        r = (closed_by.get(i) if is_closed else None) or rows_by_id.get(i) or make_row(i)
        if not r:
            continue
        r = dict(r)
        if cm:
            r["claude_moved"] = cm
        by_sid = {x["id"]: x for x in (st or {}).get("step_states") or [] if x.get("id")}
        if e["claude_ticks"].get(i):
            r["claude_did"] = [x["text"] for x in e["claude_ticks"][i]]
        if (e["claude_changes"].get(i) or {}).get("items"):   # the step trail, open card only (Nov 9)
            c = e["claude_changes"][i]
            r["claude_changes"] = {"items": c["items"], "at": c.get("at", "")}
        waits_only = st is not None and bool(st.get("task_waiting"))
        if waits_only and r.get("wait_over"):
            r["wait_back"] = True
        fin = None   # finished for today: {"kind": closed | waiting | next | none, ...}
        ct = {x["id"] for x in e["claude_ticks"].get(i, [])} - set(e["his_ticks"].get(i) or [])
        found_from = {x.get("found_by") for x in by_sid.values() if x.get("found_by")}
        # A timeboxed step whose findings are written counts as met with the frozen chunk only (Tasks v2).
        met = lambda x: x.get("state") == "done" or _waits(x) or bool(frozen and x.get("box") and x.get("id") in found_from)   # noqa: E731
        cids = e["chunk_ids"].get(i)
        chunk = e["chunks"].get(i) or []
        # D-8
        # Before Book, a task he added in Plan shows Plan's piece, live (owner decision, Nov 12: "now due
        # and plan disagree on todays chunk (havent clicked book yet though)"): never a part Do
        # stored earlier in the day, nor the note's Next line in file order.
        if i not in e["ids"] and st is not None and by_sid and flags.on("step_order"):
            from ..tasks import read as _tread
            live = [x for x in (_tread.order_now(st) or {}).get("piece") or [] if x in by_sid]
            if live:
                cids, chunk = live, [by_sid[x]["text"] for x in live]
        if cids is None and chunk and by_sid:
            cids = list(dict.fromkeys(x["id"] for c in chunk for x in by_sid.values()
                                      if _norm(x["text"]) == _norm(c)))
            new_chunk_ids[i] = cids
        late = _after_plan(e, i, st, list(cids or []), day) if cids else []
        if late:   # came up after the plan and belongs to today: joins, and the card says so (never silent)
            cids = list(cids) + late
            new_chunk_ids[i] = cids
            new_chunks[i] = [by_sid[x]["text"] for x in cids if x in by_sid]
            new_after[i] = list(dict.fromkeys((e["added_after"].get(i) or []) + late))
        known = [by_sid[x] for x in cids or [] if x in by_sid]
        today_iso = day.isoformat()
        # Beyond the plan: any step ticked today that was not committed. A count and data for the
        # estimator; it never joins, reopens or holds Today (Nov 9: "+ Today" is cut).
        ext = [x for x in by_sid.values() if cids and x["id"] not in cids
               and x["state"] == "done" and x.get("done") == today_iso]
        if known or is_closed or waits_only:
            left_s = [x for x in known if x["state"] == "open" and not met(x)]
            if not left_s and (is_closed or any(met(x) for x in known) or (waits_only and not known)):
                kind = "closed" if is_closed else "waiting" if (waits_only or any(_waits(x) for x in known)) \
                    else "next" if st and st.get("next") else "none"
                fin = {"kind": kind, "who": (st or {}).get("wait_line", "") if kind == "waiting" else ""}
        elif st is not None and st.get("next") and by_sid:
            left_s = []
        else:
            left_s = []
        if fin is None and not cids and st is not None and st.get("next") and st.get("done_today") and not is_closed:
            # Planned without a chunk and a step done today (Nov 3): today's part is done.
            fin = {"kind": "next", "who": ""}
        if fin is None and st is not None and st.get("next") and not left_s:
            # Nothing of Today open and nothing met: planned without a chunk, or every committed
            # step discarded or replaced. A frozen chunk says "No step left for today" and never
            # reads finished; without the flag the Next step at first sight is today's part.
            if frozen and cids:
                left_s = []
                r["no_step_left"] = True
            else:
                nid = st.get("next_id") or ""
                left_s = [by_sid[nid]] if nid in by_sid else [{"text": st["next"]}]
                new_chunks[i], new_chunk_ids[i] = [st["next"]], [nid] if nid else []
                prop = [x for x in (e.get("proposed") or {}).get(i, []) if x in by_sid and by_sid[x]["state"] == "open"]
                if prop and not cids and flags.on("day_turn"):
                    left_s = [by_sid[x] for x in prop]
                    new_chunks[i], new_chunk_ids[i] = [by_sid[x]["text"] for x in prop], prop
                cids = new_chunk_ids[i]
                known = [by_sid[x] for x in cids or [] if x in by_sid] or left_s
        left = [x["text"] for x in left_s]
        allk = known or left_s
        bparts = booked_parts(e, i, by_sid, events)   # a step booked as a part counts its block (Nov 11)

        def _m(x):
            return bparts[x["id"]] if x.get("id") in bparts else (_mins(x) if frozen else _est(x.get("text")))
        r["chunk"] = {"left": len(left), "total": max(len(allk), len(left)), "steps": left,
                      "ids": [x.get("id", "") for x in left_s],
                      "min": sum(_m(x) or 0 for x in left_s),
                      "unest": sum(_m(x) is None for x in left_s)}
        if bparts:
            r["chunk"]["part_min"] = bparts
        if frozen:
            r["chunk"]["planned_min"] = sum((bparts.get(x.get("id")) or _mins(x) or 0) for x in allk)
            r["chunk"]["outcome"] = e.get("outcomes", {}).get(i) or suggest_outcome([x.get("text", "") for x in allk])
            r["chunk"]["outcome_his"] = bool(e.get("outcomes", {}).get(i))
        aft = [by_sid[x]["text"] for x in (new_after.get(i) or e["added_after"].get(i) or []) if x in by_sid]
        if aft:
            r["chunk"]["added_after"] = aft
        if plan_on:
            r["chunk"]["plan"] = _plan(allk, met, ct)
            r["chunk"]["extra"] = _plan(ext, met, ct)
            inc = {x.get("id") for x in allk}
            r["found_today"] = [{"id": x["id"], "text": x["text"]} for x in by_sid.values()
                                if x.get("found_by") in inc and x["id"] not in inc and x["state"] == "open"]
        since = e["carried"].get(i)
        if since:
            r["carried_from"] = label(dt.date.fromisoformat(since))
            r["carry_ask"] = (day - dt.date.fromisoformat(since)).days >= ASK_AFTER
        # The block that holds today's open steps (Nov 11, M1), never another block of the task;
        # the Task: line match only for a task booked without step ids.
        bb = _booked_blk(e, i, allk, met, events)
        r["blk"] = bb if bb is not None or any(b.get("task") == i for b in e["block_steps"]) else \
            block_for(i, (st or {}).get("task_id", ""), events or [], (st or {}).get("event_key", ""))
        if plan_on and r["chunk"].get("plan"):
            at = {sid: b for b in live_blocks(e, i, events) for sid in b.get("sids") or []}
            if len({(b["s"], b["f"]) for b in at.values()}) > 1:   # several blocks: each step says when
                for x in r["chunk"]["plan"]:
                    if x.get("id") in at:
                        x["blk"] = {"s": at[x["id"]]["s"], "f": at[x["id"]]["f"]}
        r["do_n"] = n
        if fin is not None:
            nx = (st or {}).get("next") or ""
            r.update({"do_state": "part", "fin": fin, "part_next": S.strip_marks(nx, due=False, ids=False),
                      "part_next_est": _est(nx), "part_ask": False, "part_changed": False,
                      "outcome": r["chunk"].get("outcome"),
                      "plan": r["chunk"].get("plan", []), "extra": r["chunk"].get("extra", [])})
        if i in gone:
            # Cleared (his tap): the Cleared fold, with the card's true state.
            plan = r["chunk"].get("plan") or []
            r["cleared_state"] = {"closed": is_closed, "done": sum(x["state"] in ("done", "waiting") for x in plan),
                                  "waiting": sum(x["state"] == "waiting" for x in plan),
                                  "total": len(plan), "finished": fin is not None}
            cleared.append(r)
        elif fin is not None:
            part_done.append(r)
        else:
            open_rows.append(r)
    # Done holds tasks closed today that were never on this list (closed in Plan, a chat or
    # Obsidian); a task closed while on the list stays there, finished, until he clears it.
    shown = {r["id"] for r in open_rows + done_rows + part_done + cleared + waiting + moved + left_open}
    for r in closed:
        if r.get("closed") == day.isoformat() and r["id"] not in shown:
            shown.add(r["id"])
            done_rows.append({**r, "do_state": "done"})
    if (new_chunks or new_chunk_ids or new_after) and state.persisting():
        def keep(x):
            x["chunks"].update(new_chunks)
            x["chunk_ids"].update(new_chunk_ids)
            x["added_after"].update(new_after)
        change(day, keep)
    open_rows.sort(key=lambda r: (0 if r["blk"] else 1, r["blk"]["s"] if r["blk"] else 0,
                                  0 if e["ids"] or r.get("carried_from") else 1, r["do_n"]))
    # Something new or urgent the list does not have yet comes as a suggestion, never inserted.
    suggest = []
    if seen:
        for r in top:
            if not r or r["id"] in seen or r["id"] in e["dismissed"] or r["id"] in elsewhere:
                continue
            due = r.get("due") and r["due"] <= day.isoformat()
            if due or r.get("urgent") or r.get("update") or r.get("load", 0) >= 3:
                why = ("Due " + label(day) if due else r.get("update") or
                       ("Urgent" if r.get("urgent") else "On your mind in the meditation"))
                suggest.append({**r, "suggest_why": re.sub(r"^Update \d{1,2}:\d{2}:\s*", "", str(why))[:140]})
            if len(suggest) >= SUGGEST_MAX:
                break
    return {"date": day.isoformat(), "label": label(day), "rows": open_rows, "done": done_rows,
            "moved": moved, "suggest": suggest, "waiting": waiting, "part_done": part_done, "left_open": left_open,
            "cleared": cleared,
            "picks": [r for r in top if r][:3] if not open_rows and not done_rows and not waiting and not part_done
            and not left_open and not cleared else [],
            "committed": bool(e["ids"])}
