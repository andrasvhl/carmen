"""Planning, its writes: his per-day overrides (selected, pulled, the one thing, not the one thing,
Skip in Replies). Today's commitment lives in do.json (do.py). Both become one record when Planning
moves its state (build item, Planning v2). The tap handlers join here when actions.py is split.
"""
from __future__ import annotations

import datetime as dt

from starlette.responses import JSONResponse

from runner import notify

from . import do   # re-exported: Tasks puts a task back on today's Do list through Planning (Not waiting)
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
        if kind == "one":
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
        else:   # hidden | selected | pulled | reply_skip: a list per day
            ov[kind].setdefault(day.isoformat(), [])
            if value not in ov[kind][day.isoformat()]:
                ov[kind][day.isoformat()].append(value)
        return ov
    state.update(planning.OVERRIDES, f, {})


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
    if action not in ("do_add", "do_drop", "do_keep", "do_dismiss", "do_unmove", "do_more") or not row_id:
        return JSONResponse({"error": "bad request"}, status_code=400)
    signal({"do_add": "do_added", "do_drop": "do_dropped", "do_keep": "do_kept",
            "do_dismiss": "do_suggestion_dismissed", "do_unmove": "do_move_undone",
            "do_more": "do_kept_going"}[action],
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
    if action == "do_unmove":
        if p:
            task_actions.apply(p, "schedule", base=dday)
        dmod.add(dday, row_id)
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
    p = task_path(row_id)
    if action == "select" and p:
        live = tasks.read_note(p)
        if live["scheduled"] != base_day.isoformat():
            task_actions.apply(p, "schedule", base=base_day)   # the date in the note follows his choice
    return JSONResponse({"ok": True, "keep": True,
                         "msg": "Added" if action == "select" else "Taken out"})


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
