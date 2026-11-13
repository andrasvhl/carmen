"""Outbox, its writes: Send on a mail draft (only his tap in the sheet sends; test mode reads the
thread and stops), Replied for a message he sent himself, Skip in Replies. A send is recorded on its
step through Tasks.
"""
from __future__ import annotations

import hashlib
import json

from starlette.responses import JSONResponse

from . import read as outbox
from runner import config
from ..adapters import notes
from ..adapters.notes import task_path
from ..adapters.taplog import log_event
from ..adapters.taplog import signal
from ..planning import actions as plan_actions
from ..tasks import actions as task_actions

REPLY_UNDO = config.STATE_DIR / "checkin_app" / "reply_undo.json"   # today's Skips, for Undo


def on_mail(t):
    """mailprep, sendmail."""
    body, action, row_id, dry, day_name, row, why = t.body, t.action, t.row_id, t.dry, t.day_name, t.row, t.why
    d = row.get("draft") or {}
    if d.get("via") != "mail":
        return JSONResponse({"error": "only mail drafts send from here; copy the rest"}, status_code=400)
    edit = body.get("mail") if isinstance(body.get("mail"), dict) else {}
    msg, edits = outbox.mail_of(row, edit)
    mailer = outbox._mailer()
    try:
        # The mailbox the conversation lives in wins over the draft's acct (Oct 31); his tap on From wins over both.
        why = "you"
        if not edit.get("acct"):
            msg["acct"], why = mailer.pick_mailbox(msg["subj"], msg["to"], prefer=msg["acct"])
        if action == "mailprep":
            # Opening the sheet: who it goes to and whether it is in the thread, nothing kept.
            res = mailer.prepare(msg, outbox._since(d.get("at", "")))
            mailer._pending.pop(res.get("id"), None)
            try:   # where the earlier mail with them is (Oct 31); the sheet works without it
                rel = mailer.related(res.get("to") or msg["to"], res.get("thread_id", ""))
            except Exception as exc:
                log_event("error", where="mail related", detail=str(exc)[:200])
                rel = []
            return JSONResponse({"ok": True, "keep": True, "msg": "",
                                 "mail": {**{k: res.get(k) for k in ("to", "cc", "subj", "thread", "thread_id",
                                                                      "held", "error")},
                                          "acct": res.get("acct") or msg["acct"], "acct_why": why,
                                          "history": res.get("history") or [], "related": rel}})
        if outbox.SLOT.search(msg["body"]) or (d.get("needs_slot") and msg["body"].strip() == (d.get("body") or "").strip()):
            return JSONResponse({"error": "replace [slot] with a time first"}, status_code=400)
        if msg["acct"] not in mailer.MAILBOXES:
            return JSONResponse({"error": "pick the mailbox to send from"}, status_code=400)
        res = mailer.prepare(msg, outbox._since(d.get("at", "")), force=bool(body.get("force")))
        if res.get("held"):   # someone wrote since the draft: show it, his second tap sends
            return JSONResponse({"ok": False, "keep": True, "held": res["held"],
                                 "msg": "New in the thread since the draft"})
        if not res.get("ok"):
            return JSONResponse({"error": res.get("error") or "could not build the mail"}, status_code=400)
        to = ", ".join(res["to"])
        facts = {"edited": bool(edits), "edit": edits, "acct": res["acct"], "to": res["to"],
                 "cc": res["cc"], "thread": res["thread"], "forced": bool(body.get("force"))}
        if dry:
            mailer._pending.pop(res["id"], None)
            signal("reply_sent", row, day_name, test=True, **facts)
            return JSONResponse({"ok": True, "keep": True,
                                 "msg": f"Test mode: would send to {to}" + (f", cc {', '.join(res['cc'])}" if res["cc"] else "")
                                        + (" in the thread" if res["thread"] else " as a new mail")})
        sent = mailer.commit(res["id"])
    except Exception as exc:   # Gmail down or a token gone: say so, never half-send
        log_event("error", where="sendmail", detail=str(exc)[:200])
        return JSONResponse({"error": f"could not send: {str(exc)[:120]}"}, status_code=502)
    if not sent.get("ok"):
        return JSONResponse({"error": sent.get("error") or "send failed"}, status_code=502)
    log_event("mail_sent", id=row_id, acct=sent["acct"], to=res["to"], cc=res["cc"],
              gmail_id=sent.get("gmail_id"), thread_id=sent.get("thread_id"))
    signal("reply_sent", row, day_name, test=False, **facts)
    p = task_path(row_id)
    if not p:   # sent already; only the note is gone (archived meanwhile)
        return JSONResponse({"ok": True, "msg": f"Sent to {to}"})
    # Nov 5: a sent mail is a message on the task's step, never the task closed by it
    # (the squash mail closed Propane and Brunch with it).
    res2 = task_actions._reply_sent(p, row, to, "mail", msg.get("body", ""))
    return JSONResponse({"ok": True, "msg": f"Sent to {to}", **res2})


def on_replied(t):
    row_id, text_in, dry, day_name, row = t.row_id, t.text_in, t.dry, t.day_name, t.row
    # Replied in Replies (Nov 5): the message is recorded on its step and leaves Replies;
    # the task itself stays open unless that message was its last step.
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card; refresh"}, status_code=400)
    d = row.get("draft") or {}
    sent_body = text_in.strip() or d.get("body", "")
    signal("reply_done_himself", row, day_name, test=dry, draft=sent_body[:400])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
    res2 = task_actions._reply_sent(p, row, d.get("to", ""), d.get("via", ""), sent_body)
    return JSONResponse({"ok": True, "msg": "Sent, kept on the task" if res2.get("state") != "done"
                         else "Sent, task done", **res2})


def _undo_load() -> dict:
    try:
        return json.loads(REPLY_UNDO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _undo_keep(row_id: str, entry: dict) -> None:
    day = entry["day"]
    u = {k: v for k, v in _undo_load().items() if v.get("day") == day}
    u[row_id] = entry
    REPLY_UNDO.parent.mkdir(parents=True, exist_ok=True)
    REPLY_UNDO.write_text(json.dumps(u, ensure_ascii=False), encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def on_skipreply(t):
    row_id, dry, day_name, base_day, was_touched, row = t.row_id, t.dry, t.day_name, t.base_day, t.was_touched, t.row
    # Skip has two choices (owner decision, Nov 9, QA decision 2), each saying what it did, each with Undo,
    # and neither moves a date (Nov 3: Skip moved the sports medicine task to tomorrow, off Do):
    #   today  Hide for today: out of Replies until the next Carmen day; the task is untouched.
    #   never  Not replying: the message step is struck and the next one comes up, or the task
    #          closes; a card Carmen made that he never touched is dropped and not suggested again.
    how = "never" if t.body.get("how") == "never" else "today"
    signal("reply_skipped", row, day_name, test=dry, how=how, draft=(row.get("draft") or {}).get("body", "")[:400])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed: "
                             + ("not replying" if how == "never" else "hidden for today")})
    p = task_path(row_id)
    entry = {"how": how, "day": base_day.isoformat()}
    plan_actions.save_override("reply_skip", base_day, row_id)   # out of Replies today either way
    if how == "today" or not p:
        _undo_keep(row_id, entry)
        return JSONResponse({"ok": True, "undo": True, "msg": "Hidden for today, back tomorrow"})
    before = p.read_text(encoding="utf-8")
    if task_actions.cardnotes.is_made(row_id) and not was_touched:
        task_actions.apply(p, "cancel", base=base_day, text_in="not replying, in Carmen")
        task_actions.cardnotes.closed(row_id, "check-in app (not replying)")
        entry.update(before=before, after=_sha(p.read_text(encoding="utf-8")), dropped=True)
        _undo_keep(row_id, entry)
        return JSONResponse({"ok": True, "undo": True, "msg": "Not replying. Card dropped"})
    res = task_actions.not_replying(p, base_day)
    if res is None:   # the task's step is not the message: nothing to strike, so it is only hidden
        entry["how"] = "today"
        _undo_keep(row_id, entry)
        return JSONResponse({"ok": True, "undo": True,
                             "msg": "Hidden for today. Its next step is not the message, so the task is unchanged"})
    if res["state"] == "closed":
        task_actions.cardnotes.closed(row_id, "check-in app (not replying)")
    entry.update(before=before, after=_sha(p.read_text(encoding="utf-8")), dropped=res["state"] == "closed")
    _undo_keep(row_id, entry)
    msg = ("Not replying. Task closed" if res["state"] == "closed"
           else f"Not replying. Next: {res['next']}" if res.get("next") else "Not replying. Step struck")
    return JSONResponse({"ok": True, "undo": True, "msg": msg, **res})


def on_skipreply_undo(t):
    """Undo on either Skip: the reply is back in Replies, and the note as it was, if nothing changed it since."""
    row_id, dry, day_name, row = t.row_id, t.dry, t.day_name, t.row
    u = _undo_load().get(row_id)
    if not u:
        return JSONResponse({"error": "nothing to undo for this reply"}, status_code=409)
    p = task_path(row_id)
    if u.get("before") and p:
        cur = p.read_text(encoding="utf-8")
        if _sha(cur) != u.get("after"):
            return JSONResponse({"error": "the note changed since; undo it in Obsidian"}, status_code=409)
    signal("reply_unskipped", row, day_name, test=dry, how=u.get("how"))
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
    if u.get("before") and p:
        notes.write_from(p, cur, u["before"], lambda now: u["before"] if _sha(now) == u.get("after") else notes.refuse())
    if u.get("dropped"):
        task_actions.cardnotes.undrop(row_id)
    import datetime as dt
    plan_actions.save_override("reply_unskip", dt.date.fromisoformat(u["day"]), row_id)
    left = {k: v for k, v in _undo_load().items() if k != row_id}
    REPLY_UNDO.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True, "msg": "Undone, back in Replies"})
