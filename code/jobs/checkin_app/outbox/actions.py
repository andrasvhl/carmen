"""Outbox, its writes: Send on a mail draft (only his tap in the sheet sends; test mode reads the
thread and stops), Replied for a message he sent himself, Skip in Replies. A send is recorded on its
step through Tasks.
"""
from __future__ import annotations

from starlette.responses import JSONResponse

from . import read as outbox
from ..adapters.notes import task_path
from ..adapters.taplog import log_event
from ..adapters.taplog import signal
from ..planning import actions as plan_actions
from ..tasks import actions as task_actions


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
    # (the padel mail closed Propane and Brunch with it).
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


def on_skipreply(t):
    row_id, dry, day_name, base_day, was_touched, row = t.row_id, t.dry, t.day_name, t.base_day, t.was_touched, t.row
    # Skip on a reply (Oct 30: "similar skip should be stored for replies"): the reply
    # leaves Replies for today; a taskless one also goes into the 14-day drop memory, so
    # the next pre-run does not draft it again unless the person writes. A task is never
    # moved by it (Nov 3: Skip moved the sports medicine task to tomorrow, off Do).
    signal("reply_skipped", row, day_name, test=dry, draft=(row.get("draft") or {}).get("body", "")[:400])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed: skipped"})
    p = task_path(row_id)
    if p and task_actions.cardnotes.is_made(row_id) and not was_touched:
        # A message Carmen made a card for and he never touched: Skip drops it, as Skip on
        # a note-less reply did (14 days, unless the person writes again).
        task_actions.apply(p, "cancel", base=base_day, text_in="reply skipped in Carmen")
        task_actions.cardnotes.closed(row_id, "check-in app (reply skipped)")
        return JSONResponse({"ok": True, "msg": "Skipped"})
    plan_actions.save_override("reply_skip", base_day, row_id)
    return JSONResponse({"ok": True, "msg": "Skipped in Replies for today"})
