"""The task chat around a card (Talk), owning nothing: the context a chat gets for its task, and a
snapshot before it, so what the chat then changed in the note is logged once (talk_delta).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from runner import config

from .adapters import notes
from .adapters import state
from .adapters import taplog
from .tasks import read as tasks_read


TALK_PENDING = config.STATE_DIR / "checkin_app" / "talk_pending.json"


def _snapshot(p: Path) -> dict:
    s = tasks_read.read_note(p)
    m = re.search(r"^priority:\s*(\S+)", p.read_text(encoding="utf-8").partition("\n---\n")[0], re.M)
    s["priority"] = m.group(1) if m else ""
    return s


def remember_talk(row: dict) -> None:
    """Before a Talk: snapshot the note, so the change Claude makes in chat is logged."""
    p = notes.task_path(row["id"]) if row.get("task") else None
    if not p:
        return
    snap = {"at": time.time(), "before": _snapshot(p), "title": row.get("title", "")}
    state.update(TALK_PENDING, lambda d: d.__setitem__(row["id"], snap), {}, indent=None)


def check_talk_deltas() -> None:
    """On every view: log what changed in a talked-about note, once, then forget it.
    Unchanged snapshots expire after 3 days."""
    try:
        pend = json.loads(TALK_PENDING.read_text())
    except (OSError, ValueError):
        return
    keep = {}
    for tid, e in pend.items():
        p = notes.task_path(tid)
        after = _snapshot(p) if p else {"status": "missing"}
        delta = {k: [e["before"].get(k), after.get(k)] for k in after if e["before"].get(k) != after.get(k)}
        if delta:
            taplog.log_event("signal", signal="talk_delta", id=tid, title=e.get("title", ""), delta=delta,
                      minutes=round((time.time() - e["at"]) / 60))
        elif time.time() - e["at"] < 3 * 86400:
            keep[tid] = e
    if keep != pend:   # drop only what this pass logged or expired; a Talk in between stays
        gone = set(pend) - set(keep)
        state.update(TALK_PENDING, lambda d: {k: v for k, v in d.items() if k not in gone}, {}, indent=None)


def talk_text(row: dict, note: str = "", ending: bool = True) -> str:
    parts = [f"💬 {row['title']}"]
    if note:
        parts.append(f"Your note: {note}")
    if row.get("next"):
        parts.append(f"Next: {row['next']}")
    if row.get("waiting"):
        parts.append(f"Waiting for: {row['waiting']}")
    if row.get("why"):
        parts.append(row["why"])
    if row.get("claude"):
        parts.append("Claude could: " + "; ".join(row["claude"]))
    if row.get("task"):
        parts.append(f"Task note: Inbox/{row['id']}.md")
        # A step's workspace (Nov 4): links go under the step, the step stays a short goal.
        parts.append("Steps: one short goal per line. A link a step needs (a page, a form, a section "
                     "of a note) goes on an indented line right under that step, at most three, only "
                     "for the Next step and today's steps: \"    - [label](https://...)\" or "
                     "\"    - [[Note#Section|label]]\".")
        # Nov 5 (a trip prep task): the chat listed what was open and ticked the step, and the
        # task left Do as done before he had read the list, which was the point of the task.
        parts.append("Ticking steps: a step whose result he has to see (research, list, compare, check, "
                     "draft) is not done when you have done your part. Do not tick it: reword it in "
                     "place, same ^id, as the review he still has to do (\"Review Claude's list of what is "
                     "open and confirm it (5 min)\"), with what you prepared in the note. Tick a step only "
                     "when nothing in it is left for him to read or decide, or when he says it is done.")
    if ending:   # Telegram only; the app's chat sends this as context under his own message
        parts.append("Reply \"go\" to have me apply your note to the task, or reply to talk it through."
                     if note else "Reply to this message to talk it through, or tell me to do it.")
    return "\n\n".join(parts)
