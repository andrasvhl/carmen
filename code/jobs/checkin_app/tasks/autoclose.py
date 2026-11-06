"""A task that is plainly over closes by itself (owner decision, Nov 4: "Laura here should have been done
since dinner I had it, it would have been too late anyways. so that one should have been
automatically done"; design approved Nov 4).

The pre-run (08:00) and every Refresh pass their rows here. A row closes only when all hold:
  1. the guess is done with high confidence: g "done", no lc, and evidence (a why and a quote);
  2. it is time-bound and past: its scheduled or due date is before today, and its title or DoD
     names that time ("tonight", "today", a weekday, a date, a clock time, a meal);
  3. nothing of his own is open beyond the Next step the evidence covers (Waiting lines aside).
Then the note is closed with a Log line, a snapshot is kept for Undo (3 days), and Do's Done fold
shows "Closed for you · Undo". Never silent, and a task he reopened this way never closes again
by itself. Pure text in, text out, except run(), which reads and writes the notes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path

from . import steps as st
from ..adapters import notes
from tools import steps as S

STATE = Path(os.path.expanduser("~/.local/state/automation/checkin_app/autoclose.json"))
KEEP_DAYS = 3
TIMED = re.compile(r"\b(?:tonight|today|tomorrow|this (?:morning|afternoon|evening)|lunch|dinner|breakfast|brunch|"
                   r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|(?:mon|tue|wed|thu|fri|sat|sun)\b|"
                   r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}\b|\d{1,2}[:.-]\d{2}\b)", re.I)


def _fm(head: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*['\"]?([^'\"\n]*)", head, re.M)
    return m.group(1).strip() if m else ""


def evidence(r: dict) -> str:
    """The row's why with its first quote, or "" when the row carries no evidence."""
    why = str(r.get("s") or "").strip()
    quotes = [q for c in (r.get("ctx") or []) if isinstance(c, dict) for q in (c.get("q") or []) if str(q).strip()]
    return (why + (f" ({quotes[0]})" if quotes else "")) if why and quotes else ""


def judge(text: str, r: dict, today: dt.date) -> str:
    """Why this note may close now, or "" when it may not."""
    if r.get("g") != "done" or r.get("lc"):
        return ""
    ev = evidence(r)
    if not ev:
        return ""
    head, body = st._split(text)
    if _fm(head, "status") in ("done", "cancelled") or _fm(head, "type") != "action":
        return ""
    dates = [d[:10] for d in (_fm(head, "scheduled"), _fm(head, "due")) if re.match(r"\d{4}-\d{2}-\d{2}", d)]
    if not dates or min(dates) >= today.isoformat():
        return ""
    lines = body.split("\n")
    blk = st._block(lines)
    dod = next((l for l in lines if l.startswith("DoD")), "")
    title = str(r.get("lk") or r.get("n") or "")
    if not TIMED.search(title + " " + dod):
        return ""
    if blk:
        opens = [m for m in (st.OPEN.match(lines[i]) for i in range(blk[0] + 1, blk[1])) if m]
        if sum(1 for m in opens if m.group(2) == "Next") > 1 or any(m.group(2) not in ("Next", "Waiting") for m in opens):
            return ""
    return ev[:300]


def close(text: str, why: str, today: dt.date, via: str) -> str:
    """The note closed: open Next and Waiting ticked, waiting off, done today, a Log line."""
    head, body = st._split(text)
    lines = body.split("\n")
    blk = st._block(lines)
    if blk:
        for i in range(blk[0] + 1, blk[1]):
            m = st.OPEN.match(lines[i])
            if m and m.group(2) in ("Next", "Waiting"):
                lines[i] = f"{m.group(1)}- [x] {m.group(2)}: {m.group(3)}"
    head = st._set(st._set(st._set(head, "waiting", None), "status", "done"), "completedDate", today.isoformat())
    label = today.strftime("%b %-d")
    return head + "\n---\n" + st._log("\n".join(lines), f"- {label} · closed by Claude: {why} · {via}")


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict, today: dt.date) -> None:
    keep = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    d = {k: v for k, v in d.items() if v.get("undone") or v.get("at", "") >= keep}
    from ..adapters import state
    state.write(STATE, d)   # locked, a tmp name of its own (Nov 6)


def info(stem: str) -> dict | None:
    """What auto-close did to this note, for the Done fold, or None."""
    e = _load().get(stem)
    return e if e and not e.get("undone") else None


def run(groups, today: dt.date, via: str, task_path, log_event=None, dry: bool = False,
        skip: set | None = None) -> list[dict]:
    """Close every row's note that qualifies. groups: [[group, rows], ...]. Returns what closed.
    skip: note names changed while the judgment was made, never closed (Nov 6)."""
    store, out = _load(), []
    seen = set()
    for _, rows in groups or []:
        for r in rows or []:
            key = isinstance(r, dict) and (r.get("lk") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            p = task_path(key)
            if not p or (store.get(p.stem) or {}).get("undone") or (skip and p.stem in skip):
                continue
            text = p.read_text(encoding="utf-8")
            why = judge(text, r, today)
            if not why:
                continue
            new = close(text, why, today, via)
            out.append({"id": key, "why": why})
            if dry:
                continue
            held = {"before": text}

            def again(cur):   # changed while judged: closed only if it still qualifies
                if not judge(cur, r, today):
                    notes.refuse()
                held["before"] = cur
                return close(cur, why, today, via)
            try:
                new = notes.write_from(p, text, new, again, f"autoclose:{via}", before=text)
            except notes.NoteChanged:
                out.pop()
                continue
            store[p.stem] = {"before": held["before"], "after": hashlib.sha256(new.encode()).hexdigest(),
                             "at": today.isoformat(), "why": why, "via": via}
            if log_event:
                log_event("signal", signal="auto_closed", id=key, title=key, why=why, via=via)
    if out and not dry:
        _save(store, today)
    return out


def undo(p: Path, today: dt.date) -> str:
    """Undo from the Done fold: the note as it was, if unchanged since. "" or why it cannot."""
    store = _load()
    e = store.get(p.stem)
    if not e or e.get("undone"):
        return "nothing to undo"
    cur = p.read_text(encoding="utf-8")
    if hashlib.sha256(cur.encode()).hexdigest() != e.get("after"):
        return "the note changed since; reopen it instead"
    label = today.strftime("%b %-d")
    head, body = st._split(e["before"])
    try:
        notes.write_from(p, cur, head + "\n---\n" + st._log(body, f"- {label} · not done after all: auto-close undone (the owner) · check-in app"),
                         lambda now: notes.refuse(), "autoclose:undo")
    except notes.NoteChanged:
        return "the note changed since; reopen it instead"
    store[p.stem] = {"undone": True, "at": today.isoformat()}
    _save(store, today)
    return ""
