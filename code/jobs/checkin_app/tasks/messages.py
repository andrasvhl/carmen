"""Messages are their own records in a task note, attached to its steps (owner decision, Nov 5: "replies
create a huge problem ... communication, outgoing replies, etc., should be stored as a separate
entity within the task ... Marking something as replied should not necessarily complete the
substep"; agreed model Nov 5).

  - A task holds steps. A step can be in a waiting state (a `Waiting:` line), with what ends it.
  - Messages, in or out, attach to a step by its ID, any number of them: the request, the nudge,
    their reply. Evidence from other sources (a Venmo mail, the calendar) ends a wait the same way.
  - Sending a message never closes a task. It ticks a step only when the step is that message
    ("Text Eli the 12:00 call time"); otherwise it is only recorded on the step.

The section, written by the app, readable and editable in Obsidian:

    ## Messages
    - Nov 4 · out · WhatsApp · to Dan, Omar, Jade · step ^s1a2b
      > Padel was $240, so $80 each. Venmo me when you can
"""
from __future__ import annotations

import datetime as dt
import re

from . import steps as st

HEAD = "## Messages"
MAX_BODY = 1200


_CHANNEL = {"whatsapp": "WhatsApp", "linkedin": "LinkedIn", "imessage": "iMessage", "sms": "SMS",
            "instagram": "Instagram", "messenger": "Messenger", "telegram": "Telegram"}

def _next(lines: list[str]):
    blk = st._block(lines)
    if not blk:
        return None
    for i in range(blk[0] + 1, blk[1]):
        m = st.OPEN.match(lines[i])
        if m and m.group(2) == "Next":
            return st.S.parse_line(lines[i])
    return None


def record(text: str, *, direction: str, who: str, via: str, body: str, step_id: str, label: str) -> str:
    """The note with one message entry added to its Messages section (made before ## Log)."""
    head, rest = st._split(text)
    quoted = "\n".join("  > " + ln for ln in (body or "").strip()[:MAX_BODY].splitlines() if ln.strip())
    entry = (f"- {label} · {direction} · {via or 'message'} · {'to' if direction == 'out' else 'from'} {who or 'someone'}"
             + (f" · step ^{step_id}" if step_id else "") + ("\n" + quoted if quoted else ""))
    if f"\n{HEAD}\n" in "\n" + rest + "\n":
        a = ("\n" + rest).index(f"\n{HEAD}\n") + len(HEAD) + 1
        nxt = re.search(r"\n## ", rest[a:])
        at = a + nxt.start() if nxt else len(rest)
        rest = rest[:at].rstrip("\n") + "\n" + entry + "\n" + rest[at:]
    else:
        log = rest.find("\n## Log")
        block = f"\n{HEAD}\n{entry}\n"
        rest = (rest[:log].rstrip("\n") + "\n" + block + rest[log:]) if log >= 0 else rest.rstrip("\n") + "\n" + block
    return head + "\n---\n" + rest


def sent_on_next(text: str) -> bool:
    """Whether the note's Messages already hold a message he sent on its current Next step
    (QA Nov 8, P1: "Propose Sat Nov 14 in The Corner" was Sent at 23:47 and offered again at 00:07,
    ready to send the group the same invite twice)."""
    try:
        lines = st._split(text)[1].split("\n")
    except ValueError:
        return False
    nxt = _next(lines)
    if not nxt or not nxt.id or f"\n{HEAD}\n" not in "\n" + text + "\n":
        return False
    sec = text.split(f"\n{HEAD}\n", 1)[1].split("\n## ", 1)[0]
    return any(ln.startswith("- ") and " · out · " in ln and ln.rstrip().endswith(f"step ^{nxt.id}")
               for ln in sec.split("\n"))


def is_message_step(step: str) -> bool:
    """A step that is nothing but sending a message: done once the message is sent."""
    return bool(st.MSG_VERB.match(st.S.strip_marks(step or "", due=False, ids=False)))


# D-33
def reply_sent(text: str, *, who: str, via: str, body: str, today: dt.date, label: str) -> tuple[str, dict]:
    """A reply he sent (Replied in Replies, or the mail Send): recorded on the Next step; the step
    is ticked only when it is that message (steps.step_done then decides what follows, and closes
    the task only when nothing else is open). Never closes the task otherwise."""
    lines = st._split(text)[1].split("\n")
    nxt = _next(lines)
    text = record(text, direction="out", who=who, via=via, body=body, step_id=nxt.id if nxt else "", label=label)
    head, rest = st._split(text)
    sent = f"sent mail to {who}" if via == "mail" else f"sent to {who or 'them'} ({_CHANNEL.get((via or '').lower(), via or 'message')})"
    text = head + "\n---\n" + st._log(rest, f"- {label} · {sent} · check-in app")   # Progress counts it
    half = st.message_half_done(text, label) if nxt else None
    if half:   # "Reply to Hana and put it in the calendar": the reply is done, the calendar is next
        return half[0], {**half[1], "ticked": half[1]["step"]}
    if nxt and is_message_step(nxt.text):
        # whom it went to, for the Waiting line when the step names nobody (a group chat by name)
        to = who if re.search(r"[A-Za-z]", who or "") and "@" not in (who or "") else ""
        text, res = st.step_done(text, today, label, to=to)
        return text, {**res, "ticked": nxt.text}
    return text, {"state": "recorded", "step": nxt.text if nxt else ""}
