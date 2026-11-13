"""Notes adapter: reading and writing vault notes, no business rules. task_path finds a task note in
Inbox/; every task note write compares and swaps (write_from, @cas: the text a change was computed
from must still be the note, else the change is computed once more, else refused) and saves through
tools/steps.write (step lines in the one form, the step history, atomically); and the small format
helpers (frontmatter keys, the Log section, the Next action block).
"""
from __future__ import annotations

import contextvars
import re
from pathlib import Path

from runner import config


def task_path(name: str) -> Path | None:
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        return None
    p = config.INBOX / f"{name}.md"
    return p if p.is_file() else None


def note_name(name: str) -> str | None:
    """The task note a title or link names: exactly, or as `vt todos --add` saved it (Nov 4: a
    title over 80 characters, or with a colon, is saved under a shorter or changed file name, so
    the meditation rows for the ECON plan and the CS 351Z report lost their link)."""
    if task_path(name):
        return name
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        from tools.todos import sanitize_filename
    except Exception:
        return None
    s = sanitize_filename(name)
    return s if s != name and task_path(s) else None


def _task_file(name: str) -> Path | None:
    for folder in (config.INBOX, config.VAULT / "Archive" / "Inbox"):
        p = folder / f"{name}.md"
        if "/" not in name and p.is_file():
            return p
    return None


def _fm_of(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for ln in (m.group(1) if m else "").splitlines():
        k, sep, v = ln.partition(":")
        if sep and not ln.startswith((" ", "-")):
            out[k.strip()] = v.strip().strip("'\"")
    out["_body"] = text[m.end():] if m else text
    return out


def _set_fm(head: str, key: str, value: str) -> str:
    pat = re.compile(rf"^{key}:.*$", re.M)
    return pat.sub(lambda _: f"{key}: {value}", head, count=1) if pat.search(head) else f"{head}\n{key}: {value}"


# Who is writing (Nov 4, the step history): "tap:<action>" inside a tap, else the default.
WRITER = contextvars.ContextVar("writer", default="check-in app")


# --- compare-and-swap on every task note write (Nov 6, build item bullet; the owner approved) -------
# A tap, prep or a chat computes its change from the note as it read it; Obsidian, a chat or the
# pre-run may have written the note since. Every write therefore passes the text it was computed
# from: unchanged, it is written; changed, the change is computed once more on what is there now;
# changed again, it is refused with CHANGED and nothing is written. Undo already checks by hash.
# Two ways in: write_from() where the caller holds the text, and @cas around a writer function
# that reads with read_text() and writes with _write(). The pre-run has its own check.
# The window between the compare and the replace stays (no lock is shared with Obsidian).

CHANGED = "the note changed; refresh"
BEFORE_CHECK = None   # tests only: called with the path right before each compare


class NoteChanged(Exception):
    """A task note changed underneath twice: refuse, write nothing (the server answers 409)."""

    def __init__(self, msg: str = CHANGED):
        super().__init__(msg)


class _Again(Exception):
    pass


def refuse():
    """For a recompute that finds its change no longer applies."""
    raise NoteChanged()


_BASES = contextvars.ContextVar("note_bases", default=None)   # inside @cas: {path: text read}


def _current(p: Path) -> str | None:
    if BEFORE_CHECK:
        BEFORE_CHECK(p)
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _store(p: Path, text: str, source: str | None = None, **kw) -> str:
    """Save a task note: its step lines in the one form (tools/steps.py: fields, IDs, a tick
    dated), what changed in its steps logged to the step history, written atomically. Returns
    the text as written."""
    from tools import steps as S
    from . import clock
    kw.setdefault("today", clock.carmen_day())   # a tick at 00:30 is the evening's (one day model)
    return S.write(p, text, source or WRITER.get(), **kw)


def read_text(p: Path) -> str:
    """A task note's text, remembered inside @cas as the base its write is checked against."""
    text = p.read_text(encoding="utf-8")
    bases = _BASES.get()
    if bases is not None:
        bases[str(p)] = text
    return text


def cas(fn):
    """A writer that reads with read_text and writes with _write, pure until it writes: run once
    more if its note changed underneath, then refuse."""
    import functools

    @functools.wraps(fn)
    def run(*a, **k):
        for again in (False, True):
            token = _BASES.set({})
            try:
                return fn(*a, **k)
            except _Again:
                if again:
                    raise NoteChanged()
            finally:
                _BASES.reset(token)
    return run


def _write(p: Path, text: str, source: str | None = None) -> str:
    """Write a note inside @cas, against the text read_text gave for it."""
    bases = _BASES.get()
    if bases is None or str(p) not in bases:
        raise RuntimeError(f"{p.name}: a task note write without the text it was computed from "
                           "(use notes.write_from or @notes.cas)")
    if _current(p) != bases[str(p)]:
        raise _Again()
    out = _store(p, text, source)
    bases[str(p)] = out
    return out


def write_from(p: Path, base: str, text: str, recompute, source: str | None = None, **kw) -> str:
    """Write text, computed from base, if the note is still base; else write recompute(now) once,
    if the note holds still meanwhile; else raise NoteChanged. recompute may raise NoteChanged
    itself. A `before` for the step history follows the text the change was computed from."""
    cur = _current(p)
    if cur != base:
        if cur is None:
            raise NoteChanged()
        text = recompute(cur)
        if _current(p) != cur:
            raise NoteChanged()
        if "before" in kw:
            kw["before"] = cur
    return _store(p, text, source, **kw)


def _next_block(text: str) -> tuple[int, int]:
    m = re.search(r"^## Next action[ \t]*\n", text, re.M)
    if not m:
        raise ValueError("this task has no Next action block")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return m.end(), m.end() + (nxt.start() if nxt else len(text) - m.end())


def _with_log(p: Path, text: str, line: str) -> None:   # inside @cas
    head, sep, body = text.partition("\n---\n")
    _write(p, f"{head}{sep}{_log(body, line)}" if sep else _log(text, line))


def _log(body: str, line: str) -> str:
    body = body.rstrip()
    if re.search(r"^## Log\s*$", body, re.M):
        return f"{body}\n{line}\n"
    return f"{body}\n\n## Log\n{line}\n"


def _one_line(s: str) -> str:
    return " ".join((s or "").split())
