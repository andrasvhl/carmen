"""Done on a Do card means the step is done, not always the task (owner decision, Nov 3: "the main next
step basically was completed there already"). What comes next is read from the note:

  - the step was a message to someone that asks for an answer (Ask, Offer, Chase, or "ask",
    "confirm", "?" in it; Nov 4): the answer is theirs, so the task waits on them: `waiting: true`
    and a Waiting line, and Do moves it into the Waiting fold until the wait ends. A message that
    only tells (Text Charly that ..., Send Nadia the receipts) never waits;
  - else the next open step line (a Then: line, or a plain checkbox) becomes the Next step;
  - else nothing is left: the task is done.

Undo restores the note as it was; Not waiting takes the wait back out. Pure text in, text out:
the app writes the files and keeps the undo copy.
"""
from __future__ import annotations

import datetime as dt
import re

from tools import steps as S   # the line format: fields, IDs, history (Nov 4)

MSG_VERB = re.compile(r"^(?:reply|answer|respond|email|e-mail|mail|whatsapp|text|message|ask|offer|"
                      r"send|write|ping|dm|nudge|chase|remind|follow up with)\b", re.I)
_NAME = r"[A-ZÀ-Ý][\w'’.-]*(?:\s+[A-ZÀ-Ý][\w'’.-]*)*(?:\s*\([^)]*\))?"
WHO = re.compile(rf"^(?:follow up with|[\w-]+)\s+(?:back\s+)?(?:to\s+)?({_NAME}(?:\s*(?:,|and|&)\s*{_NAME})*)")
EST = re.compile(r"\s*\(\d+\s*min\)\s*$")
OPEN = re.compile(r"^(\s*)- \[ \] (?:(Next|Then|Waiting): )?(.*)$")
MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
                                       "Oct", "Nov", "Dec"], 1)}


# A message waits on its answer only when it asks for one (owner decision, Nov 4: Charly's "Text Charly that
# you're going to India instead of China" went to Waiting: "it was more just to tell him"). Telling,
# sending a receipt or thanking is done once sent; asking, offering or chasing waits.
ASK_VERB = re.compile(r"^(?:ask|offer|invite|propose|request|chase|nudge|remind|follow up with|check with)\b", re.I)
ASK_WORD = re.compile(r"\?|\b(?:ask|asking|asks|what time|if (?:he|she|they|it)|confirm|"
                      r"find out|check (?:if|whether|with)|propose|proposing|offer|offering|invite|inviting|"
                      r"request|requesting|let me know|get back|agree on|pick a|for (?:a|the) (?:time|date|slot)|"
                      r"to (?:approve|sign|decide|choose)|for (?:his|her|their) (?:ok|okay|approval|answer|reply|input))\b", re.I)


def _words(step: str) -> str:
    """A step's words: no estimate, no dates, no ID (tools/steps.py)."""
    return S.clean(step or "")


def asks(step: str) -> bool:
    """Whether a message step asks for an answer, so the task should wait on it."""
    s = _words(step)
    return bool(ASK_VERB.match(s) or ASK_WORD.search(s))


def who_of(step: str) -> str:
    """Whom a message step goes to, or "" when the step is not a message to a named person."""
    s = _words(step)
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
                lines[i] = f"{m.group(1)}- [ ] Next: {m.group(3).strip()}"   # fields and ID move with it
                return S.parse_line(lines[i]).display()
    return ""


_SKIP = {"I", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Jan", "Feb", "Mar", "Apr", "May",
         "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec", "WhatsApp", "Gmail", "Claude"}


def guess_who(step: str) -> str:
    """A name to prefill "Waiting on": whom a message goes to, else the first capitalised
    word after the verb ("Open Sam's chat" gives Sam). Only a prefill; he confirms it."""
    w = who_of(step)
    if w:
        return w
    for m in re.finditer(r"(?<=\s)([A-ZÀ-Ý][\w-]*?)(?:'s|’s)?\b", _words(step)):
        if m.group(1) not in _SKIP:
            return m.group(1)
    return ""


FOR = " · for: "   # a step in its waiting state: "<who / until> · for: <the step>" (Nov 5)


def set_waiting(text: str, who: str, label: str) -> str:
    """Waiting is a state of a step, not of the task (owner decision, Nov 5: "the wait is tied to a
    substep"): the open Next becomes a Waiting line that keeps its text, fields and id
    ("Jose, Mathi, Juanita · for: Get the padel money back"), and his next own step comes up, so
    the task leaves for the Waiting fold only when nothing of his is left. waiting: true."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    if not blk:
        lines[0:0] = ["", "## Next action", ""]
        blk = (1, 3)
    a, b = blk
    done = False
    for i in range(a + 1, b):
        m = OPEN.match(lines[i])
        if m and m.group(2) == "Next":
            stp = S.parse_line(lines[i])
            stp.kind, stp.text = "Waiting", f"{who}{FOR}{stp.text}"
            lines[i] = S.format_line(stp)
            done = True
            break
    if not done:   # no step of his open: the wait itself goes on top
        first = next((i for i in range(a + 1, b) if OPEN.match(lines[i])), a + 1)
        lines.insert(first, f"- [ ] Waiting: {who} ({label})")
        b += 1
    _promote(lines, a, b)
    head = _set(head, "waiting", "true")
    return head + "\n---\n" + _log("\n".join(lines), f"- {label} · waiting on {who} · check-in app")


def step_done(text: str, today: dt.date, label: str, who: str | None = None,
              wait_over: bool = False) -> tuple[str, dict]:
    """The note after Done on its card, and {"state": waiting|next|done, "who", "step", "next",
    "guess"}. who, when given, is his answer to "Waiting instead": wait on that person.
    wait_over: the card said "Wait over", so Done ends the wait (ticks the Waiting line) and the
    next open step comes up, or the task closes when none is left (Nov 4, Laura's dinner)."""
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
    if wait and wait_over:
        a, b = blk
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Waiting":
                lines[i] = f"{m.group(1)}- [x] Waiting: {m.group(3)}"
                break
        head = _set(head, "waiting", None)
        who = re.split(r",\s*(?:on|to)\b", _words(wait.group(3)), maxsplit=1)[0].strip()
        new = _promote(lines, a, b)
        if new:
            return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · wait over ({who}), next: {new} · check-in app"),
                    {"state": "next", "step": "", "next": new, "guess": guess_who(new)})
        head = _set(_set(head, "status", "done"), "completedDate", today.isoformat())
        return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · wait over ({who}) · done · check-in app"),
                {"state": "done", "step": ""})
    if wait:      # no step of his own, already waiting: Done never closes it by accident
        head = _set(head, "waiting", "true")
        who = re.split(r",\s*(?:on|to)\b", _words(wait.group(3)), maxsplit=1)[0].strip()
        return head + "\n---\n" + body, {"state": "waiting", "who": who, "step": ""}
    if not nxt:   # nothing to tick and nothing waited on: the card's Done closes the task
        head = _set(_set(head, "status", "done"), "completedDate", today.isoformat())
        return head + "\n---\n" + _log(body, f"- {label} · done · check-in app"), {"state": "done", "step": ""}
    i, m = nxt
    step = _words(m.group(3))
    lines[i] = f"{m.group(1)}- [x] Next: {m.group(3)}"   # its ✅ date is added when the note is written
    a, b = blk
    forced = who is not None
    who = who.strip() if forced else (who_of(step) if asks(step) else "")
    if who:
        line = (f"{who} ({label}, after: {step})" if forced
                else f"{who}, to reply to your message ({label}: {step})")
        lines.insert(i + 1, f"{m.group(1)}- [ ] Waiting: {line}")
        head = _set(head, "waiting", "true")
        new = _promote(lines, a, b + 1)   # Nov 5: the wait is the step's; his next step comes up
        out = ({"state": "next", "step": step, "next": new, "who": who, "guess": guess_who(new)} if new
               else {"state": "waiting", "who": who, "step": step})
        note = f"waiting on {who}" + (f", next: {new}" if new else "")
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


def discard_step(text: str, want: str, label: str) -> tuple[str, dict] | None:
    """The × on a step in the Start panel (owner decision, Nov 4: "a small x on hover on substeps where i
    can discard them"): the open step line struck, never deleted, with "(discarded, <label>)"; a
    discarded Next brings up the next Then or open step. None when no open line says want."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    key = _words(want)
    if not blk or not key:
        return None
    a, b = blk
    for i in range(a + 1, b):
        m = OPEN.match(lines[i])
        if m and _words(m.group(3)) == key:
            kind = m.group(2) or ""
            st = S.parse_line(lines[i])
            st.state, st.note = "struck", f"(discarded, {label})"
            lines[i] = S.format_line(st)
            new = _promote(lines, a, b) if kind == "Next" else ""
            return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · step discarded: {key} · check-in app"),
                    {"kind": kind or "step", "next": new})
    return None


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
                st = S.parse_line(lines[i])
                if FOR in st.text:   # the step itself was waiting: it is his Next again
                    for j in range(a + 1, b):
                        mj = OPEN.match(lines[j])
                        if mj and mj.group(2) == "Next":
                            lines[j] = f"{mj.group(1)}- [ ] Then: {mj.group(3)}"
                    st.kind, st.text = "Next", st.text.split(FOR, 1)[1]
                else:
                    st.state, st.note = "struck", f"(not waiting, {label})"
                lines[i] = S.format_line(st)
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
            st = S.parse_line(lines[i])
            if FOR in st.text:
                new = new.split(FOR, 1)[0] + FOR + st.text.split(FOR, 1)[1]
            if st.text == new:
                return text
            st.text = new
            lines[i] = S.format_line(st)
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


def switch_box(text: str, sid: str, label: str, box: int | None = None) -> tuple[str, dict] | None:
    """Known or not known yet (Tasks v2, Nov 6): a tap on the step's time switches it. A known
    step gets a timebox (30 min unless box says 15 or 45) instead of its estimate; a timeboxed
    step gets an estimate from the estimator. None when no open step has that id."""
    from . import estimates
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    for i in range(blk[0] + 1, blk[1]) if blk else []:
        st = S.parse_line(lines[i])
        if not st or st.id != sid or st.state != "open" or st.kind == "Waiting":
            continue
        if st.box:
            kind, m, _ = estimates.estimate("", [{"text": st.text}])[0]
            st.est, st.box = estimates.fmt(m if kind == "est" else estimates.known(st.text)), None
            what = {"known": True, "est": st.est}
            line = f"- {label} · step known now: {st.text} ({st.est}) · check-in app"
        else:
            st.box, st.est = (box if box in estimates.BOXES else 30), ""
            what = {"known": False, "box": st.box}
            line = f"- {label} · step not known yet: {st.text} (up to {st.box} min) · check-in app"
        lines[i] = S.format_line(st)
        return head + "\n---\n" + _log("\n".join(lines), line), what
    return None
