"""Done on a Do card means the step is done, not always the task (owner decision, Nov 3: "the main next
step basically was completed there already"). What comes next is read from the note:

  - the step was a message to someone (Reply to, Email, WhatsApp, Ask, ...): the answer is
    theirs, so the task waits on them: `waiting: true` and a Waiting line, and Do moves it into
    the Waiting fold until the wait ends;
  - else the next open step line (a Then: line, or a plain checkbox) becomes the Next step;
  - else nothing is left: the task is done.

Undo restores the note as it was; Not waiting takes the wait back out. Pure text in, text out:
the app writes the files and keeps the undo copy.
"""
from __future__ import annotations

import datetime as dt
import re

MSG_VERB = re.compile(r"^(?:reply|answer|respond|email|e-mail|mail|whatsapp|text|message|ask|offer|"
                      r"send|write|ping|dm|nudge|chase|remind|follow up with)\b", re.I)
_NAME = r"[A-ZÀ-Ý][\w'’.-]*(?:\s+[A-ZÀ-Ý][\w'’.-]*)*(?:\s*\([^)]*\))?"
WHO = re.compile(rf"^(?:follow up with|[\w-]+)\s+(?:back\s+)?(?:to\s+)?({_NAME}(?:\s*(?:,|and|&)\s*{_NAME})*)")
EST = re.compile(r"\s*\(\d+\s*min\)\s*$")
OPEN = re.compile(r"^(\s*)- \[ \] (?:(Next|Then|Waiting): )?(.*)$")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                                       "Oct", "Nov", "Dec"], 1)}


def who_of(step: str) -> str:
    """Whom a message step goes to, or "" when the step is not a message to a named person."""
    s = EST.sub("", step or "").strip()
    if not MSG_VERB.match(s):
        return ""
    m = WHO.match(s)
    return m.group(1).strip(" ,") if m else ""


def _split(text: str) -> tuple[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError("this note has no frontmatter, fix it in Obsidian")
    head, _, body = text.partition("\n---\n")
    return head, body


def _set(head: str, key: str, val: str | None) -> str:
    lines = [l for l in head.split("\n") if not l.startswith(f"{key}:")]
    if val is not None:
        lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _block(lines: list[str]) -> tuple[int, int] | None:
    for i, l in enumerate(lines):
        if l.strip() == "## Next action":
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return i, end
    return None


def _log(body: str, line: str) -> str:
    body = body.rstrip()
    if re.search(r"^## Log\s*$", body, re.M):
        return f"{body}\n{line}\n"
    return f"{body}\n\n## Log\n{line}\n"


def _promote(lines: list[str], a: int, b: int) -> str:
    """The first open Then: line, else the first plain open step, becomes Next. Its text, or ""."""
    for kind in ("Then", None):
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == kind and m.group(3).strip():
                lines[i] = f"{m.group(1)}- [ ] Next: {m.group(3).strip()}"
                return m.group(3).strip()
    return ""


_SKIP = {"I", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Jan", "Feb", "Mar", "Apr", "May",
         "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "WhatsApp", "Gmail", "Claude"}


def guess_who(step: str) -> str:
    """A name to prefill "Waiting on": whom a message goes to, else the first capitalised
    word after the verb ("Open Sam's chat" gives Sam). Only a prefill; he confirms it."""
    w = who_of(step)
    if w:
        return w
    for m in re.finditer(r"(?<=\s)([A-ZÀ-Ý][\w-]*?)(?:'s|’s)?\b", EST.sub("", step or "")):
        if m.group(1) not in _SKIP:
            return m.group(1)
    return ""


def set_waiting(text: str, who: str, label: str) -> str:
    """Waiting on someone before his own step is done (Nov 3, "Waiting on..." in the card):
    the open Next waits as a Then line, a Waiting line goes on top, waiting: true."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    if not blk:
        lines[0:0] = ["", "## Next action", ""]
        blk = (1, 3)
    a, b = blk
    first = None
    for i in range(a + 1, b):
        m = OPEN.match(lines[i])
        if m and first is None:
            first = i
        if m and m.group(2) == "Next":
            lines[i] = f"{m.group(1)}- [ ] Then: {m.group(3)}"
    at = first if first is not None else a + 1
    lines.insert(at, f"- [ ] Waiting: {who} ({label})")
    head = _set(head, "waiting", "true")
    return head + "\n---\n" + _log("\n".join(lines), f"- {label} · waiting on {who} · check-in app")


def step_done(text: str, today: dt.date, label: str, who: str | None = None) -> tuple[str, dict]:
    """The note after Done on its card, and {"state": waiting|next|done, "who", "step", "next",
    "guess"}. who, when given, is his answer to "Waiting instead": wait on that person."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    nxt = None
    if blk:
        for i in range(blk[0] + 1, blk[1]):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Next":
                nxt = (i, m)
                break
    wait = None
    if blk and not nxt:
        wait = next((m for m in (OPEN.match(lines[i]) for i in range(blk[0] + 1, blk[1]))
                     if m and m.group(2) == "Waiting"), None)
    if wait:      # no step of his own, already waiting: Done never closes it by accident
        head = _set(head, "waiting", "true")
        who = re.split(r",\s*(?:on|to)\b", wait.group(3), maxsplit=1)[0].strip()
        return head + "\n---\n" + body, {"state": "waiting", "who": who, "step": ""}
    if not nxt:   # nothing to tick and nothing waited on: the card's Done closes the task
        head = _set(_set(head, "status", "done"), "completedDate", today.isoformat())
        return head + "\n---\n" + _log(body, f"- {label} · done · check-in app"), {"state": "done", "step": ""}
    i, m = nxt
    step = EST.sub("", m.group(3)).strip()
    lines[i] = f"{m.group(1)}- [x] Next: {m.group(3)}"
    a, b = blk
    forced = who is not None
    who = who.strip() if forced else who_of(step)
    if who:
        line = (f"{who} ({label}, after: {step})" if forced
                else f"{who}, to reply to your message ({label}: {step})")
        lines.insert(i + 1, f"{m.group(1)}- [ ] Waiting: {line}")
        head = _set(head, "waiting", "true")
        out = {"state": "waiting", "who": who, "step": step}
        note = f"waiting on {who}"
    else:
        new = _promote(lines, a, b)
        if new:
            out = {"state": "next", "step": step, "next": new, "guess": guess_who(step)}
            note = f"next: {new}"
        else:
            head = _set(_set(head, "status", "done"), "completedDate", today.isoformat())
            out = {"state": "done", "step": step, "guess": guess_who(step)}
            note = "task done"
    body = _log("\n".join(lines), f"- {label} · step done: {step} · {note} · check-in app")
    return head + "\n---\n" + body, out


def not_waiting(text: str, label: str) -> str:
    """Take the wait back out: waiting off, the open Waiting line struck, the next step up."""
    head, body = _split(text)
    lines = body.split("\n")
    head = _set(head, "waiting", None)
    blk = _block(lines)
    if blk:
        a, b = blk
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Waiting":
                lines[i] = f"{m.group(1)}- ~~Waiting: {m.group(3)}~~ (not waiting, {label})"
                break
        if not any((OPEN.match(lines[j]) or [None, None, None])[2] == "Next" for j in range(a + 1, b)):
            _promote(lines, a, b)
    return head + "\n---\n" + _log("\n".join(lines), f"- {label} · not waiting · check-in app")


def edit_waiting(text: str, new: str, label: str) -> str:
    """His own words for what the task waits on: the open Waiting line, rewritten."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    for i in range(blk[0] + 1, blk[1]) if blk else []:
        m = OPEN.match(lines[i])
        if m and m.group(2) == "Waiting":
            if m.group(3).strip() == new:
                return text
            lines[i] = f"{m.group(1)}- [ ] Waiting: {new}"
            return head + "\n---\n" + _log("\n".join(lines), f"- {label} · waiting on: {new} · check-in app")
    return set_waiting(text, new, label)


def waiting_since(text: str, today: dt.date) -> dt.date | None:
    """The day the current wait began: the last Log line that started one."""
    found = None
    for m in re.finditer(r"^- ([A-Z][a-z]{2}) (\d{1,2}) · [^\n]*waiting on ", text, re.M):
        mon = MONTHS.get(m.group(1))
        if not mon:
            continue
        d = dt.date(today.year, mon, int(m.group(2)))
        if d > today:
            d = d.replace(year=today.year - 1)
        found = d
    return found
