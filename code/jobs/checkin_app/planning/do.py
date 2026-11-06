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

from ..adapters import state
from ..adapters import clock

STATE = config.STATE_DIR / "checkin_app" / "do.json"
ASK_AFTER = 3          # carried this many days in a row: Keep, Move or Drop
NUDGE_AFTER = 3        # waiting this many days: the Waiting fold offers a nudge
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
                     ("chunks", {}), ("chunk_ids", {}), ("claude_ticks", {})):
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
    return not (st.get("scheduled") and st["scheduled"] > day.isoformat()
                and not (st.get("due") and st["due"] <= day.isoformat()))


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
        for s in steps:
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
    finds ("Find out what SHC clearance needs" gives "Know what SHC clearance needs"), any other
    step is that step done."""
    if not steps:
        return ""
    t = re.sub(r"\s*\((?:timebox )?\d+\s*min\)\s*$", "", _norm_keep_case(steps[-1]))
    m = _FIND.match(t)
    if m:
        return "Know " + m.group(1)
    return t + (" done" if len(steps) == 1 else ", and the steps before it")


def _norm_keep_case(s: str) -> str:
    s = re.sub(r"^(?:Next|Then):\s*", "", str(s or "").strip())
    return " ".join(re.sub(r"\s*\(due [^)]*\)", "", s).split())


def _mins(x: dict) -> int | None:
    """A step's planned minutes: its estimate, else its timebox (step_states with v2 fields)."""
    if x.get("min") is not None:
        return x["min"]
    if x.get("box"):
        return x["box"]
    m = re.search(r"\((?:timebox )?(\d+)\s*min\)\s*$", x.get("text", ""))
    return int(m.group(1)) if m else None


def more(day: dt.date, task_id: str, step: str, sid: str = "") -> None:
    """Keep going: the task's next step joins today's chunk, by its id."""
    def f(e):
        c = e["chunks"].setdefault(task_id, [])
        if step and _norm(step) not in {_norm(x) for x in c}:
            c.append(step[:600])
        if sid:
            have = e["chunk_ids"].get(task_id)
            if isinstance(have, list):
                if sid not in have:
                    have.append(sid)
            else:
                e["chunk_ids"].pop(task_id, None)   # first resolved from the texts, then by id
        else:
            e["chunk_ids"].pop(task_id, None)
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


def _plan(steps: list[dict], met) -> list[dict]:
    """Today's committed steps with their state, for the Do card (Tasks v2): done, open or dropped."""
    return [{"id": x.get("id", ""), "text": x.get("text", ""),
             "state": "done" if met(x) else ("dropped" if x.get("state") == "struck" else "open")} for x in steps]


def view(day: dt.date, rows_by_id: dict, closed: list[dict], dated: list[dict], top: list[dict],
         events: list[dict] | None, note, make_row, label, selected: list[str] = ()) -> dict:
    """What the Do tab shows for the day. `dated` (tasks scheduled or due that day) no longer
    joins the list (Nov 6, the owner: "Left over yes, but the others should come via plan i think
    since i need to account for their time when budgeting"): Do holds what he committed or added
    and what an earlier day left open; a dated task waits in Plan's selection, and one due today
    comes as a New for today suggestion. The parameter stays for the callers' positions."""
    alive = set(rows_by_id)
    e = ensure(day, alive, note)
    from ..adapters import flags   # Tasks v2 (Nov 6)
    frozen, plan_on = flags.on("frozen_chunk"), flags.on("do_plan")
    closed_by = {r["id"]: r for r in closed}
    # The plan he committed today orders the list (Nov 4: "the plan that was created is not
    # reflected in the ordering"): his ids first, then what is only carried. Before any commit
    # today, the carried tasks lead as before.
    order = (e["ids"] + list(e["carried"]) if e["ids"] else list(e["carried"])) \
        + [i for i in selected if isinstance(i, str)]
    seen, open_rows, done_rows, moved, waiting, left_open = set(), [], [], [], [], []
    # Left open (owner decision, Nov 4: "tasks remaining from yesterday if not explicitly added should then
    # go into a card below"): once he committed the day, a task only carried over (not in his plan,
    # not added, no block today) waits in the Left open fold for Add or Drop.
    picked = set(e["ids"]) | {i for i in selected if isinstance(i, str)}
    part_done, new_chunks, new_chunk_ids = [], {}, {}
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
        # Next step at first sight as its chunk. When none of the chunk is open any more and one
        # of its steps shows as done (ticked, or a "step done:" line), today's part is done: the
        # card leaves for the Done fold, saying so, with Keep going. A chunk whose steps were only
        # reworded or replaced, by a chat for example, restarts with the new Next step (Nov 4:
        # the India task left Do as done after a chat rewrote its Next step).
        by_sid = {x["id"]: x for x in (st or {}).get("step_states") or [] if x.get("id")}
        # Every step a chat ticked today shows on the card as "Claude did: ... · Review" (Nov 5),
        # also once Keep going moved the day's part past it.
        if e["claude_ticks"].get(i):
            r["claude_did"] = [x["text"] for x in e["claude_ticks"][i]]
        if st is not None and st.get("next") and by_sid:
            # By step id (Nov 4, the owner: "differentiate between next steps that are marked as done
            # and next steps that were simply changed"): the chunk is the ids of today's steps,
            # resolved once from the texts Plan sent. A reworded step keeps its id and stays in
            # the chunk; the part is done only when one of its steps is done (ticked); a chunk
            # whose steps are all gone (struck or replaced, none done) restarts with the Next step.
            chunk = e["chunks"].get(i) or []
            cids = e["chunk_ids"].get(i)
            if cids is None and chunk:
                cids = list(dict.fromkeys(x["id"] for c in chunk for x in by_sid.values()
                                          if _norm(x["text"]) == _norm(c)))
                new_chunk_ids[i] = cids
            known = [by_sid[x] for x in cids or [] if x in by_sid]
            left_s = [x for x in known if x["state"] == "open"]
            ct = {x["id"] for x in e["claude_ticks"].get(i, [])}
            if frozen and cids:
                # Frozen at commit (Tasks v2, Nov 6): the chunk is the steps committed, nothing joins
                # it but Keep going or a re-plan, and it never restarts with a new Next. A timeboxed
                # step is done once its findings are written as steps (found_by), whoever ticked it:
                # the SHC case, where a met outcome read as unfinished.
                found_from = {x.get("found_by") for x in by_sid.values() if x.get("found_by")}
                met = lambda x: x["state"] == "done" or bool(x.get("box") and x["id"] in found_from)   # noqa: E731
                left_s = [x for x in known if x["state"] == "open" and not met(x)]
                his = any(met(x) and (x["id"] not in ct or bool(x.get("box") and x["id"] in found_from)) for x in known)
                if not left_s and (his or not any(met(x) for x in known)):
                    pr = {**r, "do_state": "part", "part_next": _EST.sub("", st["next"]).strip(),
                          "part_next_est": _est(st["next"]), "part_changed": not his}
                    if plan_on:
                        pr["plan"] = _plan(known, met)
                        pr["outcome"] = e.get("outcomes", {}).get(i) or suggest_outcome([x["text"] for x in known])
                    part_done.append(pr)
                    continue
            elif not left_s and any(x["state"] == "done" and x["id"] not in ct for x in known):
                part_done.append({**r, "do_state": "part", "part_next": _EST.sub("", st["next"]).strip(),
                                  "part_next_est": _est(st["next"])})
                continue
            if not left_s and not cids and st.get("done_today"):
                part_done.append({**r, "do_state": "part", "part_next": _EST.sub("", st["next"]).strip(),
                                  "part_next_est": _est(st["next"])})
                continue
            if not left_s and not (frozen and cids):   # the old restart, gone with the frozen chunk
                nid = st.get("next_id") or ""
                left_s = [by_sid[nid]] if nid in by_sid else [{"text": st["next"]}]
                new_chunks[i], new_chunk_ids[i] = [st["next"]], [nid] if nid else []
                # Carried into a new day (slice 6b): the fresh piece the day turn proposed.
                prop = [x for x in (e.get("proposed") or {}).get(i, []) if x in by_sid and by_sid[x]["state"] == "open"]
                if prop and not cids and flags.on("day_turn"):
                    left_s = [by_sid[x] for x in prop]
                    new_chunks[i], new_chunk_ids[i] = [by_sid[x]["text"] for x in prop], prop
                cids = new_chunk_ids[i]
                if flags.on("day_turn") and i in e["carried"] and i not in e["ids"] \
                        and i not in (e.get("turned") or []):
                    new_chunks.pop(i, None)   # not frozen before the day turn has proposed its piece
                    new_chunk_ids.pop(i, None)
            left = [x["text"] for x in left_s]
            r["chunk"] = {"left": len(left), "total": max(len(cids or []), len(left)), "steps": left,
                          "ids": [x.get("id", "") for x in left_s],
                          "min": sum(_est(c) or 0 for c in left), "unest": sum(_est(c) is None for c in left)}
            if frozen:   # the outcome line, and the minutes counting a timebox (Tasks v2)
                allk = [by_sid[x] for x in cids or [] if x in by_sid] or left_s
                r["chunk"]["min"] = sum(_mins(x) or 0 for x in left_s)
                r["chunk"]["unest"] = sum(_mins(x) is None for x in left_s)
                r["chunk"]["planned_min"] = sum(_mins(x) or 0 for x in allk)
                r["chunk"]["outcome"] = e.get("outcomes", {}).get(i) or suggest_outcome([x["text"] for x in allk])
                r["chunk"]["outcome_his"] = bool(e.get("outcomes", {}).get(i))
            if plan_on:   # today's plan stays visible with its ticks; what was found today apart
                found_from = {x.get("found_by") for x in by_sid.values() if x.get("found_by")}
                allk = [by_sid[x] for x in cids or [] if x in by_sid] or left_s
                r["chunk"]["plan"] = _plan(allk, lambda x: x.get("state") == "done"
                                           or bool(x.get("box") and x.get("id") in found_from))
                inc = {x.get("id") for x in allk}
                r["found_today"] = [{"id": x["id"], "text": x["text"]} for x in by_sid.values()
                                    if x.get("found_by") in inc and x["id"] not in inc and x["state"] == "open"]
        elif st is not None and st.get("next"):
            # Steps without ids (refactor step 1, Nov 5): every writer adds ids, so this is only a
            # note no writer has touched since. Its Next step is today's part; nothing is matched by words,
            # so nothing counts as done until the note has ids.
            r["chunk"] = {"left": 1, "total": 1, "steps": [st["next"]], "ids": [""],
                          "min": _est(st["next"]) or 0, "unest": int(_est(st["next"]) is None)}
        since = e["carried"].get(i)
        if since:
            r["carried_from"] = label(dt.date.fromisoformat(since))
            days = (day - dt.date.fromisoformat(since)).days
            r["carry_ask"] = days >= ASK_AFTER
        r["blk"] = block_for(i, (st or {}).get("task_id", ""), events or [], (st or {}).get("event_key", ""))
        r["do_n"] = n
        if since and e["ids"] and i not in picked and not r["blk"] and not r.get("wait_back"):
            left_open.append(r)
            continue
        open_rows.append(r)
    # Done holds everything closed today, not only what was on this list (Nov 3): a task closed
    # in Plan, the chat or Obsidian. Every card is a note since Nov 4 (cardnotes).
    shown = {r["id"] for r in open_rows + done_rows + part_done + waiting + moved + left_open}
    for r in closed:
        if r.get("closed") == day.isoformat() and r["id"] not in shown:
            shown.add(r["id"])
            done_rows.append({**r, "do_state": "done"})
    if (new_chunks or new_chunk_ids) and state.persisting():
        def keep(x):
            x["chunks"].update(new_chunks)
            x["chunk_ids"].update(new_chunk_ids)
        change(day, keep)
    open_rows.sort(key=lambda r: (0 if r["blk"] else 1, r["blk"]["s"] if r["blk"] else 0,
                                  0 if e["ids"] or r.get("carried_from") else 1, r["do_n"]))
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
            "moved": moved, "suggest": suggest, "waiting": waiting, "part_done": part_done, "left_open": left_open,
            "picks": [r for r in top if r][:3] if not open_rows and not done_rows and not waiting and not part_done
            and not left_open else [],
            "committed": bool(e["ids"])}
