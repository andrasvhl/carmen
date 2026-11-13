"""Every tap: POST /api/act, one action each. The dispatcher only: it owns no data.

`act(ctx, req, body)` logs the tap, gives a card its task note on its first live tap, and hands
the tap to the module that owns what it changes (Nov 6, the server split): Tasks, Planning,
Direction, Signals or Outbox, each in its own actions.py. The stages are those of the one act()
before the split: add first; then the card stage (before the row lookup, do_* by prefix); then the
row stage, with the view's row. Two taps start agents and stay here: answer/prepare (Claude's prep)
and talk (the task chat in Telegram).

ctx carries what a tap needs from the running app: card_id, current_view, find, source. ACTIONS
is every action handled; the contract test checks that every action the page posts is in it and
that test mode writes nothing.
"""
from __future__ import annotations

import datetime as dt
import types

from starlette.responses import JSONResponse

from runner import notify

from . import runs, talk
from .adapters import clock
from .adapters.clock import TZ, _label
from .adapters import notes
from .adapters.notes import WRITER, _log, _one_line, task_path
from .adapters.taplog import log_event, signal
from .direction import actions as direction_actions
from .outbox import actions as outbox_actions
from .planning import actions as plan_actions
from .signals import actions as signal_actions
from .tasks import actions as task_actions
from .tasks import cardnotes


def on_prep(t):
    """answer, prepare."""
    body, action, row_id, text_in, dry, day_name, row = t.body, t.action, t.row_id, t.text_in, t.dry, t.day_name, t.row
    # His answer to one of Claude's questions continues that task (Oct 30).
    from . import prep
    st = prep.load_state(row_id) or {"id": row_id, "answers": []}
    if action == "answer":
        q, ans = _one_line(str(body.get("q", "")))[:300], _one_line(text_in)[:500]
        if not ans:
            return JSONResponse({"error": "empty answer"}, status_code=400)
        signal("answered", row, day_name, test=dry, q=q, answer=ans)
        if dry:
            return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing sent to Claude"})
        st.setdefault("answers", []).append({"q": q, "answer": ans,
                                             "at": dt.datetime.now(TZ).isoformat(timespec="minutes")})
        p = task_path(row_id)
        if p:
            def answered(t0):
                hb, sp, bd = t0.partition("\n---\n")
                return f"{hb}{sp}{_log(bd, f'- {_label(clock.carmen_day())} · the owner answered Claude: {q} → {ans} · check-in app')}"
            t0 = p.read_text(encoding="utf-8")
            notes.write_from(p, t0, answered(t0), answered)
    elif dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing started"})
    st["pending"] = True
    st.pop("retry_after", None)   # his own tap overrides a failure's backoff
    st["asked"] = dt.datetime.now(TZ).isoformat(timespec="minutes")
    prep.save_state(row, st)
    started = runs.start_prep(task=row_id)
    return JSONResponse({"ok": True, "keep": True,
                         "msg": "Claude is on it, back here in 5 to 15 min" if started
                         else "Queued, Claude finishes another task first"})


def on_talk(t):
    text_in, dry, day_name, tab_name, row = t.text_in, t.dry, t.day_name, t.tab_name, t.row
    signal({"claude": "claude_go", "replies": "reply_talk"}.get(tab_name, "talk"), row, day_name,
           test=dry, note=_one_line(text_in))
    if dry:
        return JSONResponse({"ok": True, "msg": "Test mode, nothing sent to the chat", "keep": True})
    talk.remember_talk(row)
    notify.send_telegram(talk.talk_text(row, _one_line(text_in)), silent=False)
    return JSONResponse({"ok": True, "msg": "Sent to the chat", "close": True})


def act(ctx, req, body: dict):
    action, row_id = body.get("action"), body.get("id", "")
    action = action if isinstance(action, str) else ""
    WRITER.set(f"tap:{action or 'unknown'}")
    row_id = row_id if isinstance(row_id, str) else str(row_id)
    text_in = str(body.get("text", ""))[:2000]
    dry = bool(body.get("test"))
    log_event("tap", action=action, id=row_id, test=dry, src=ctx.source(req),
              tab=body.get("tab", ""), edited=bool(text_in))
    t = types.SimpleNamespace(ctx=ctx, req=req, body=body, action=action, row_id=row_id,
                              text_in=text_in, dry=dry)
    if action == "add":
        return task_actions.on_add(t)
    day_name = "tomorrow" if body.get("day") == "tomorrow" else "today"
    if body.get("tab") == "replies":   # Replies has no Today/Tomorrow: always the Do day (Nov 9)
        day_name = "today"
    base_day = clock.carmen_day() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
    tab_name = body.get("tab", "")
    # Every card is a task note (owner decision, Nov 4: "convert everything into a task node"): a card
    # without one yet gets it on this first tap, so every branch below works on a note.
    from .tasks import cardnotes
    was_touched = True
    if row_id and action not in ("confirm", "plan", "journal_save", "meditation"):
        row_id = ctx.card_id(row_id, dry)
        body["id"] = row_id
        if dry and not task_path(row_id) and action != "mark":
            return JSONResponse({"ok": True, "keep": True,
                                 "msg": "Test mode, nothing written: this card gets its task note on a live tap"})
        if not dry and action != "mark":
            was_touched = cardnotes.touch(row_id)
    t.day_name, t.base_day, t.tab_name, t.was_touched, t.row_id = day_name, base_day, tab_name, was_touched, row_id
    h = CARD.get(action) or (CARD["do_"] if action.startswith("do_") else None)
    if h:
        res = h(t)
        if res is not None:
            return res
    row = ctx.find(row_id)
    if not row:
        return JSONResponse({"error": "row not found, refresh"}, status_code=404)
    why = _one_line(str(body.get("why", "")))[:500]
    t.row, t.why = row, why
    h = ROW.get(action)
    if h:
        res = h(t)
        if res is not None:
            return res
    return JSONResponse({"error": "unknown action"}, status_code=400)


# Each action, the stage it runs in and its handler (Nov 6, the server split). Card stage: after a
# card gets its note, before the row lookup; row stage: with the view's row for it.
CARD = {
    "step_discard": task_actions.on_step_discard,
    "step_box": task_actions.on_step_box,           # Tasks v2
    "step_fine": task_actions.on_step_fine,         # Tasks v2, the review list
    "step_feedback": task_actions.on_step_feedback, # Tasks v2, the review list: the golden set
    "step_start": task_actions.on_step_start,       # Tasks v2
    "actual_check": task_actions.on_actual_check,   # Tasks v2
    "cleanup_apply": task_actions.on_cleanup,       # Tasks v2
    "cleanup_skip": task_actions.on_cleanup,        # Tasks v2
    "cleanup_keep": task_actions.on_cleanup,        # Tasks v2, an old task kept
    "cleanup_close": task_actions.on_cleanup,       # Tasks v2, an old task closed or not a task
    "subtick": task_actions.on_step_tick,
    "tick_line": task_actions.on_step_tick,
    "step_untick": task_actions.on_step_tick,      # Nov 9: one step open again, by its id
    "step_done": task_actions.on_step,
    "step_undo": task_actions.on_step,
    "task_finish": task_actions.on_step,
    "not_waiting": task_actions.on_step,
    "step_wait": task_actions.on_step,
    "set_waiting": task_actions.on_step,
    "edit_waiting": task_actions.on_step,
    "dod_confirm": task_actions.on_note_edit,
    "dod_set": task_actions.on_note_edit,
    "prop_accept": task_actions.on_note_edit,
    "prop_ignore": task_actions.on_note_edit,
    "steps_save": task_actions.on_note_edit,
    "trail_ok": task_actions.on_trail,             # Nov 9: the chat's step changes, kept
    "trail_undo": task_actions.on_trail,           # Nov 9: the chat's step changes, put back
    "remove_undo": task_actions.on_remove_undo,    # r8 F5: Undo on Not today's toast
    "do_": plan_actions.on_do,
    "confirm": plan_actions.on_confirm,
    "journal_save": signal_actions.on_journal_save,
    "meditation": signal_actions.on_meditation,
    "plan": plan_actions.on_plan,
}
ROW = {
    "mark": signal_actions.on_mark,
    "swap": plan_actions.on_swap,
    "select": plan_actions.on_select,
    "unselect": plan_actions.on_select,
    "answer": on_prep,
    "prepare": on_prep,
    "mailprep": outbox_actions.on_mail,
    "sendmail": outbox_actions.on_mail,
    "replied": outbox_actions.on_replied,
    "skipreply": outbox_actions.on_skipreply,
    "skipreply_undo": outbox_actions.on_skipreply_undo,
    "notone": plan_actions.on_notone,
    "notdone": task_actions.on_notdone,
    "talk": on_talk,
    "remove": task_actions.on_remove,
    "update": task_actions.on_update,
    "raise": direction_actions.on_raise,
    "noraise": direction_actions.on_raise,
    "auto_close_undo": task_actions.on_auto_close_undo,
    "reopen": task_actions.on_reopen,
    "done": task_actions.on_task,
    "tomorrow": task_actions.on_task,
    "next": task_actions.on_task,
    "note": task_actions.on_task,
    "schedule": task_actions.on_task,
}
# Every action the page may post (the page/server contract test reads this).
DO = ("do_add", "do_commit", "do_dismiss", "do_drop", "do_keep", "do_more", "do_unmove",
      "do_outcome", "do_chunk_feedback", "do_partclose", "do_moveok",
      "do_clear", "do_back")   # all CARD["do_"]; do_outcome, do_chunk_feedback: Tasks v2; do_clear, do_back: Nov 9
ACTIONS = frozenset({"add", *CARD, *ROW, *DO} - {"do_"})
