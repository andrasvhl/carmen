"""Outbox, what goes out: a draft and his changes to it as the mail to send, through plan-today's
mailer.py (the same code the Mac panel sends with). Threads come from Signals (the pre-run's
drafts); a send is recorded on its step through Tasks (tasks/actions._reply_sent). The tap handlers
(send, replied, skip) join actions.py here when it is split.
"""
from __future__ import annotations

import datetime as dt
import re

from ..signals import read as signals


# --- sending mail from Replies --------------------------------------------------------
# Send on a mail draft (owner decision, Oct 31: act without switching apps). plan-today's mailer.py
# finds the thread and builds the reply, the same code the Mac's two-minute page sends with.
# Only his tap in the sheet sends; test mode reads the thread and stops before the send.


SLOT = re.compile(r"\[slot\]", re.I)


def _mailer():
    return signals._plan_today_mod("mailer")


def _since(at: str) -> float:
    try:
        return dt.datetime.fromisoformat(at).timestamp()
    except ValueError:
        return 0.0


def mail_of(row: dict, edit: dict) -> tuple[dict, dict]:
    """The mail to send: the draft with his changes. -> (msg, edits), edits = {field: {draft, sent}}."""
    d = row.get("draft") or {}
    msg = {k: str(edit[k] if k in edit else d.get(k, "")) for k in ("to", "subj", "body")}
    msg["acct"] = str(edit.get("acct") or d.get("acct") or "").strip().lower()
    if edit.get("thread_id"):
        msg["thread_id"] = str(edit["thread_id"])
    if edit.get("new_thread"):
        msg["new_thread"] = True
    edits = {k: {"draft": d.get(k, ""), "sent": msg[k]} for k in ("to", "subj", "body")
             if k in edit and msg[k].strip() != (d.get(k) or "").strip()}
    return msg, edits
