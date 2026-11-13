"""Planning, its writes: his per-day overrides (selected, pulled, the one thing, not the one thing,
Skip in Replies). Today's commitment lives in do.json (do.py). Both become one record when Planning
moves its state (build item, Planning v2). The tap handlers join here when actions.py is split.
"""
from __future__ import annotations

import datetime as dt

from starlette.responses import JSONResponse

from runner import notify

from . import do   # re-exported: Tasks puts a task back on today's Do list through Planning (Not waiting)
from . import pushdown   # re-exported: Tasks records a push-down on Not today / Not important / Not now
from . import read as planning
from ..adapters import clock
from ..adapters import state
from ..adapters.clock import TZ
from ..adapters.notes import task_path
from ..adapters.taplog import log_event
from ..adapters.taplog import signal
from ..tasks import actions as task_actions
from ..tasks import read as tasks


def save_override(kind: str, day: dt.date, value: str) -> None:
    """One change to overrides.json, under its lock (state.update, Nov 6)."""
    keep = {(day - dt.timedelta(days=i)).isoformat() for i in range(-2, 7)}

    def f(raw):
        ov = planning._norm_overrides(raw)
        for k in ov:
            ov[k] = {d: v for d, v in ov[k].items() if d in keep}   # prune old days
        if kind in ("note", "note_sent"):   # his note for Claude, one per day (r8 F2)
            if value.strip():
                ov[kind][day.isoformat()] = value
            else:
                ov[kind].pop(day.isoformat(), None)
        elif kind == "one":
            ov["one"][day.isoformat()] = value
            ov["notone"][day.isoformat()] = [i for i in ov["notone"].get(day.isoformat(), []) if i != value]
        elif kind == "notone":
            if ov["one"].get(day.isoformat()) == value:
                ov["one"].pop(day.isoformat())
            ov["notone"].setdefault(day.isoformat(), [])
            if value not in ov["notone"][day.isoformat()]:
                ov["notone"][day.isoformat()].append(value)
        elif kind == "unselected":
            ov["selected"][day.isoformat()] = [i for i in ov["selected"].get(day.isoformat(), []) if i != value]
            ov["pulled"][day.isoformat()] = [i for i in ov["pulled"].get(day.isoformat(), []) if i != value]
        elif kind == "reply_unskip":   # Undo on a Skip in Replies (Nov 9)
            ov["reply_skip"][day.isoformat()] = [i for i in ov["reply_skip"].get(day.isoformat(), []) if i != value]
        else:   # hidden | selected | pulled | reply_skip: a list per day
            ov[kind].setdefault(day.isoformat(), [])
            if value not in ov[kind][day.isoformat()]:
                ov[kind][day.isoformat()].append(value)
        return ov
    state.update(planning.OVERRIDES, f, {})


def commit_seen(day: dt.date, ids: list[str]) -> dict:
    """Each task's Next and the steps due by the day, as they are at commit (Nov 11, M1)."""
    from . import do as dmod
    out = {}
    for i in ids:
        tp = task_path(i)
        if not tp:
            continue
        st = tasks.read_note(tp)
        out[i] = {"next": st.get("next_id", ""),
                  "due": [x["id"] for x in st.get("step_states") or [] if x.get("id") and dmod.due_by(x, day)]}
    return out


# D-8
def apply_booked(day: dt.date, blocks: list[dict], tasks_of_day: list[str] | None = None) -> dict:
    """Book landed (Nov 11, the owner: "the chunk on Do must match what Plan booked"): each booked
    block's step ids become its task's part for the day, a task with several blocks takes them
    all in block order. A block without ids leaves its task's part as it was and is logged.
    tasks_of_day: the tasks to look in for the ids, by default the day's commitment and selection.
    Returns {"tasks": {task: [ids]}, "no_ids": [titles], "unknown": [ids]}."""
    from . import do as dmod
    if tasks_of_day is None:
        sel = (planning.load_overrides().get("selected") or {}).get(day.isoformat(), [])
        tasks_of_day = list(dmod._day(dmod._load(), day)["ids"]) + [i for i in sel if isinstance(i, str)]
    owner, text = {}, {}
    for i in dict.fromkeys(tasks_of_day):
        tp = task_path(i)
        if not tp:
            continue
        for x in tasks.read_note(tp).get("step_states") or []:
            if x.get("id"):
                owner.setdefault(x["id"], i)
                text[x["id"]] = x["text"]
    per, placed, no_ids, unknown = {}, [], [], []
    for b in sorted(blocks, key=lambda b: b.get("s", 0)):
        sids = [s for s in b.get("sids") or [] if isinstance(s, str)]
        if not sids:
            no_ids.append(b.get("title", ""))
            continue
        by_task = {}
        for s in sids:
            if s in owner:
                by_task.setdefault(owner[s], []).append(s)
            else:
                unknown.append(s)
        for i, ss in by_task.items():
            v = per.setdefault(i, {"ids": [], "texts": []})
            for s in ss:
                if s not in v["ids"]:
                    v["ids"].append(s)
                    v["texts"].append(text[s])
            placed.append({"task": i, "s": b["s"], "f": b["f"], "title": b.get("title", ""), "sids": ss,
                           **({"key": b["key"]} if b.get("key") else {})})
    if per:
        dmod.book_chunks(day, per, placed)
    log_event("book_chunks", day=day.isoformat(), tasks={i: v["ids"] for i, v in per.items()},
              **({"no_ids": no_ids} if no_ids else {}), **({"unknown": unknown} if unknown else {}))
    return {"tasks": {i: v["ids"] for i, v in per.items()}, "no_ids": no_ids, "unknown": unknown}


def on_do(t):
    """do_*: do_add, do_commit, do_dismiss, do_drop, do_keep, do_more, do_unmove, do_outcome,
    do_chunk_feedback."""
    ctx, body, action, row_id, dry, day_name, base_day = t.ctx, t.body, t.action, t.row_id, t.dry, t.day_name, t.base_day
    # Do (Nov 1): his taps on the day's list. The list changes only here.
    from . import do as dmod
    dday = clock.do_day(dt.datetime.now(TZ))
    if action == "do_commit":   # Create or Update plan in Plan, for that day
        # Today is Do's day, which turns at 4:00 (Nov 6: a plan made at 01:30 went to the
        # calendar date, and Do, still on the evening's day, did not show it until 4:00).
        if day_name == "today":
            base_day = dday
        ids = [str(i) for i in body.get("ids")] if isinstance(body.get("ids"), list) else []
        if not dry:   # every planned card is a note, so its block can name its ID
            ids = [ctx.card_id(i) for i in ids]
            for i in ids:
                task_actions.cardnotes.touch(i)
        log_event("do_commit", day=base_day.isoformat(), n=len(ids), test=dry)
        if not dry:
            chunks = body.get("chunks") if isinstance(body.get("chunks"), dict) else None
            cids = body.get("chunk_ids") if isinstance(body.get("chunk_ids"), dict) else None
            ocs = body.get("outcomes") if isinstance(body.get("outcomes"), dict) else None
            dmod.commit(base_day, ids, chunks, cids, ocs)
            dmod.mark_commit(base_day, commit_seen(base_day, ids))
            # Planned for this day means this day (Nov 3: the race, added in Plan but still
            # scheduled Nov 4, showed on Do only as "Moved to Nov 4"): a later date comes
            # forward, as Add on Do already does. An earlier or missing date stays.
            for i in ids:
                tp = task_path(i)
                if tp and tasks.read_note(tp)["scheduled"] > base_day.isoformat():
                    task_actions.apply(tp, "schedule", base=base_day)
        return JSONResponse({"ok": True, "keep": True, "msg": ""})
    if action == "do_outcome" and row_id:   # Tasks v2 (Nov 6): his words for today's outcome
        signal("do_outcome", {"id": row_id, "title": row_id}, "today", test=dry, text=t.text_in[:140])
        if not dry:
            dmod.set_outcome(dday, row_id, t.text_in)
        return JSONResponse({"ok": True, "keep": True, "msg": "" if not dry else "Test mode, nothing changed"})
    if action == "do_chunk_feedback" and row_id:   # Nov 6: the chunk review (planning/chunkreview)
        from . import chunkreview
        verdict = str(body.get("verdict", "save"))
        signal("do_chunk_feedback", {"id": row_id, "title": row_id}, "today", test=dry, verdict=verdict)
        if dry:
            return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
        shown = body.get("shown") if isinstance(body.get("shown"), dict) else {}
        votes = body.get("votes") if isinstance(body.get("votes"), dict) else {}
        row = chunkreview.feedback(dday, row_id, shown, votes, str(body.get("outcome", "")), body.get("min"),
                                   str(body.get("note", "")), verdict)
        if row is None:
            return JSONResponse({"error": "nothing to save: vote, write the outcome, your minutes or a note"}, status_code=400)
        if row["his_outcome"]:   # his words become today's outcome, as Edit on the card does
            dmod.set_outcome(dday, row_id, row["his_outcome"])
        return JSONResponse({"ok": True, "keep": True, "msg": "Feedback saved"})
    if action in ("do_clear", "do_back"):
        # Clear and Back to today (Nov 9): membership only, one card or several (Clear N finished);
        # nothing in a note changes. Undo is the other one.
        ids = [str(x)[:300] for x in (body.get("ids") or [row_id]) if x][:50]
        if not ids:
            return JSONResponse({"error": "bad request"}, status_code=400)
        for x in ids:
            signal("do_cleared" if action == "do_clear" else "do_back_to_today", {"id": x, "title": x}, "today",
                   test=dry, source=str(body.get("from", ""))[:20], n=len(ids))
        if dry:
            return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
        (dmod.clear if action == "do_clear" else dmod.back)(dday, ids)
        return JSONResponse({"ok": True, "keep": True, "msg": "", "ids": ids})
    if action not in ("do_add", "do_drop", "do_keep", "do_dismiss", "do_unmove", "do_more",
                      "do_partclose", "do_moveok") or not row_id:
        return JSONResponse({"error": "bad request"}, status_code=400)
    signal({"do_add": "do_added", "do_drop": "do_dropped", "do_keep": "do_kept",
            "do_dismiss": "do_suggestion_dismissed", "do_unmove": "do_move_undone",
            "do_more": "do_kept_going", "do_partclose": "do_part_closed",
            "do_moveok": "do_claude_move_ok"}[action],
           {"id": row_id, "title": row_id}, "today", test=dry, source=str(body.get("from", ""))[:20])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
    p = task_path(row_id)
    if action == "do_add":
        dmod.add(dday, row_id)
        if p and tasks.read_note(p)["scheduled"] != dday.isoformat():
            task_actions.apply(p, "schedule", base=dday)
        return JSONResponse({"ok": True, "keep": True, "msg": "Added to today"})
    if action == "do_more":   # Keep going: the next step joins today's chunk
        st = tasks.read_note(p) if p else {}
        if not st.get("next"):
            return JSONResponse({"error": "this task has no next step"}, status_code=400)
        dmod.more(dday, row_id, st["next"], st.get("next_id", ""))
        return JSONResponse({"ok": True, "keep": True, "msg": ""})
    if action == "do_partclose":   # Done for today (Nov 9): only his tap sends the part to Done
        dmod.part_close(dday, row_id)
        return JSONResponse({"ok": True, "keep": True, "msg": "Done for today"})
    if action == "do_moveok":   # the move a chat made stands
        dmod.clear_claude_move(dday, row_id)
        return JSONResponse({"ok": True, "keep": True, "msg": "Moved"})
    if action == "do_unmove":
        if p:
            task_actions.apply(p, "schedule", base=dday)
        from_sel = dmod.moved_from_selection(dday, row_id)
        dmod.clear_claude_move(dday, row_id)
        dmod.mark_moved(dday, row_id, None)
        if from_sel:   # it came from the selection alone (r8 F1): back in the selection, as it was
            save_override("selected", dday, row_id)
        else:
            dmod.add(dday, row_id)
        # Back on today also takes it out of the next day's plan (Nov 10, one day per task).
        nxt = dday + dt.timedelta(days=1)
        save_override("unselected", nxt, row_id)
        _add_date(nxt, row_id, pop=True)
        dmod.uncommit(nxt, row_id)
        return JSONResponse({"ok": True, "keep": True, "msg": "Back on today"})
    getattr(dmod, action[3:])(dday, row_id)
    return JSONResponse({"ok": True, "keep": True,
                         "msg": {"do_drop": "Taken off today", "do_keep": "Kept", "do_dismiss": "Not today"}[action]})


def on_confirm(t):
    ctx, body, dry, day_name = t.ctx, t.body, t.dry, t.day_name
    # He confirms Claude's pre-selection before planning (signal 1).
    v = ctx.current_view()
    pre = [r["id"] for r in ([v[day_name]["one"]] if v[day_name]["one"] else []) + v[day_name]["needs"]]
    ids = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else [])]
    signal("selection_confirmed", None, day_name, test=dry, ids=ids,
           pre_selected=pre, kept=[i for i in ids if i in pre])
    return JSONResponse({"ok": True, "keep": True, "msg": "Selection confirmed"})


def on_plan(t):
    ctx, body, dry, base_day = t.ctx, t.body, t.dry, t.base_day
    ids, over = planning.plan_ids(body)
    ids = [ctx.card_id(i, dry) for i in ids]
    if not dry:
        for i in ids:
            task_actions.cardnotes.touch(i)
    v = ctx.current_view()
    rows = [r for i in ids for r in planning.all_rows(v) if r["id"] == i][:len(ids)]
    seen, picked = set(), []
    for r in rows:
        if r["id"] not in seen:
            seen.add(r["id"]); picked.append(r)
    plan_gap = planning.plan_gaps_note(over, [i for i in dict.fromkeys(ids) if i not in seen], "plan ids (act)")
    if not picked:
        return JSONResponse({"error": "nothing selected to plan"}, status_code=400)
    if dry:
        return JSONResponse({"ok": True, "keep": True,
                             "msg": f"Test mode, nothing sent: would plan {len(picked)} items"})
    notify.send_telegram(planning.plan_text(base_day, picked) + plan_gap, silent=False)
    return JSONResponse({"ok": True, "msg": "Sent to Claude in Telegram, reply go", "close": True})


def on_swap(t):
    body, row_id, dry, day_name, base_day, row = t.body, t.row_id, t.dry, t.day_name, t.base_day, t.row
    signal("one_swapped", row, day_name, test=dry, previous=body.get("previous", ""))
    if not dry:
        save_override("one", base_day, row_id)
        p = task_path(row_id)
        if p and tasks.read_note(p)["scheduled"] != base_day.isoformat():
            task_actions.apply(p, "schedule", base=base_day)
    return JSONResponse({"ok": True, "keep": dry,
                         "msg": ("Test mode, nothing changed: " if dry else "") + "Now the one thing"})


def on_select(t):
    """select, unselect."""
    body, action, row_id, dry, day_name, base_day, row = t.body, t.action, t.row_id, t.dry, t.day_name, t.base_day, t.row
    # Add (Oct 30): he actively takes Claude's suggestion into his selection.
    # from: one | needs (Claude's pre-selection) | waiting | done | more | suggest. Anything
    # but one or needs is a pull from below: what Claude missed (owner decision, Oct 30).
    src = str(body.get("from", ""))[:12]
    pulled = action == "select" and src in ("waiting", "done", "more", "suggest")
    signal("pulled_up" if pulled else ("selected" if action == "select" else "unselected"),
           row, day_name, test=dry, section=src, suggest=row.get("suggest") or "")
    if dry:
        return JSONResponse({"ok": True, "keep": True,
                             "msg": "Test mode, nothing changed: " + ("added" if action == "select" else "taken out")})
    save_override("selected" if action == "select" else "unselected", base_day, row_id)
    if pulled:
        save_override("pulled", base_day, row_id)
    # One day at a time (Nov 10, r3 S2 and r3b: the race was selected, then committed, for Monday
    # and Tuesday): selecting for one day takes the task out of the other day's selection and its
    # commitment on Do; Take out puts back the date, the selection and the commitment as they were.
    other = base_day + dt.timedelta(days=1 if day_name == "today" else -1)
    p = task_path(row_id)
    if action == "select":
        had_sel = row_id in planning.load_overrides()["selected"].get(other.isoformat(), [])
        save_override("unselected", other, row_id)
        live = tasks.read_note(p) if p else {}
        # Picked for Tomorrow while on today's Do: it stays in today's list, which then shows "Moved to
        # Wed · Undo" (Nov 10, approved Do change), never vanishing; Tomorrow's commitment is lifted.
        rec = {"sel": other.isoformat() if had_sel else "", "do": do.uncommit(other, row_id) if day_name == "today" else None,
               "prev": _add_date(other, row_id, pop=True)}   # the other day's own record, put back with it
        if p and live["scheduled"] != base_day.isoformat():
            rec["d"] = live["scheduled"]
            task_actions.apply(p, "schedule", base=base_day)   # the date in the note follows his choice
        if day_name == "tomorrow":   # on today's Do: it stays there as "Moved to Wed · Undo" (r7 2)
            do.mark_moved(other, row_id, base_day.isoformat(), sel=had_sel)
        if "d" in rec or rec["sel"] or rec["do"]:
            _add_date(base_day, row_id, rec)
    elif action == "unselect":
        if day_name == "tomorrow":
            do.mark_moved(other, row_id, None)
        rec = _add_date(base_day, row_id, pop=True)
        if rec:
            dated_here = p and tasks.read_note(p)["scheduled"] == base_day.isoformat()
            if rec.get("d") is not None and dated_here:
                task_actions.apply(p, "unplan", base=base_day, text_in=rec["d"])
                log_event("add_date_undone", id=row_id, day=base_day.isoformat(), back_to=rec["d"])
            if rec.get("sel"):   # back to the other day, as it was before this Add
                save_override("selected", other, row_id)
                if rec.get("prev"):
                    _add_date(other, row_id, rec["prev"])
            if rec.get("do"):
                do.recommit(other, row_id, rec["do"])
    return JSONResponse({"ok": True, "keep": True,
                         "msg": "Added" if action == "select" else "Taken out"})


ADD_DATES = planning.OVERRIDES.with_name("add_dates.json")


def _add_date(day: dt.date, row_id: str, rec: dict | None = None, pop: bool = False) -> dict | None:
    """What an Add for `day` changed, so Take out can undo it: {"d": the scheduled date before
    ("" for none), present only when Add changed it; "sel": the other day it took the task out of;
    "do": its commitment there}. pop: read and forget it (None when nothing was recorded)."""
    out = {}

    def f(raw):
        cur = raw if isinstance(raw, dict) else {}
        keep = {(day - dt.timedelta(days=i)).isoformat() for i in range(-2, 8)}
        cur = {d: v for d, v in cur.items() if d in keep and isinstance(v, dict)}
        k = day.isoformat()
        if pop:
            v = cur.get(k, {}).pop(row_id, None)
            out["v"] = {"d": v} if isinstance(v, str) else v if isinstance(v, dict) else None
        else:   # the latest Add wins: an older entry for the day is stale
            cur.setdefault(k, {})[row_id] = dict(rec or {})
        return cur
    state.update(ADD_DATES, f, {})
    return out.get("v")


def on_notone(t):
    row_id, dry, day_name, base_day, row, why = t.row_id, t.dry, t.day_name, t.base_day, t.row, t.why
    # "Remove from the one thing" (Oct 30): a ranking correction; the task stays in the list.
    signal("one_rejected", row, day_name, test=dry, why=why)
    if not dry:
        save_override("notone", base_day, row_id)
    return JSONResponse({"ok": True, "keep": dry,
                         "msg": ("Test mode, nothing changed: " if dry else "") + "No longer the one thing"})


_turning = {"on": False}


def day_turn(day: dt.date | None = None) -> None:
    """Slice 6b, from the server's tick: the day turn for left-over tasks, in a thread of its own
    (the estimator's model takes seconds per task; no tap waits on it)."""
    import threading
    from ..adapters import flags
    from . import do as dmod
    if not flags.on("day_turn") or _turning["on"]:
        return
    day = day or clock.do_day(dt.datetime.now(TZ))
    e = dmod._day(dmod._load(), day)
    if not e.get("rolled") or all(i in (e.get("turned") or []) or i in e["ids"] for i in e["carried"]):
        return
    _turning["on"] = True

    def run():
        try:
            n = dmod.turn(day, task_path, task_actions.reestimate, task_actions.piece, log_event)
            if n:
                log_event("day_turn", day=day.isoformat(), n=n)
        finally:
            _turning["on"] = False
    threading.Thread(target=run, daemon=True).start()
