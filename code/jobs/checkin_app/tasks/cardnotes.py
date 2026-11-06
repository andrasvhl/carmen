"""Every Carmen card is a task note (owner decision, Nov 4: "convert everything into a task node, and I
think we have to do this now because it's creating so many bugs").

Before Nov 4 a row from mail, WhatsApp or the meditation had no note behind it, and every tap
needed its own path: Done hid it by an override, blocks named it by an `Item:` title line, and
Undo, Progress and slotting broke on it. Now:

  - The pre-run and Refresh (update.py) create a note for every new row they write
    (`attach`), with `tools.todos.add_task`, so it carries a task_id and the schema.
  - A row still without one (the live meditation matcher's rough rows, which run on every page
    load and must not write) gets its note on his first tap (`create`, from the app).
  - `state/checkin_app/made.json` remembers what Carmen created: title, day, the raw row, and
    whether he ever touched it. The next pre-run cancels a note he never touched and that it
    does not carry (`sweep`), so a candidate sits in Triage for a day at most.
  - A Carmen note closed or cancelled goes into plan-today's 14-day dropped memory with its raw
    row, so the same message is not proposed again until the person writes again.

Design: side-session note carmen-every-card-a-note-design-nov4.md (approved Nov 4).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
from pathlib import Path

from runner import config

MADE = config.STATE_DIR / "checkin_app" / "made.json"
KEEP_DAYS = 30
SRC = {"pre-run": "the morning pre-run", "update": "a Refresh", "tap": "a card in Carmen",
       "migration": "a card in Carmen (Nov 4 switch)"}


@contextlib.contextmanager
def _locked():
    """One writer at a time across processes: the app, the pre-run and Refresh all create."""
    MADE.parent.mkdir(parents=True, exist_ok=True)
    with open(MADE.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def load() -> dict:
    try:
        d = json.loads(MADE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, dict)} if isinstance(d, dict) else {}


def _save(d: dict) -> None:
    from ..adapters.clock import TZ
    cut = (dt.datetime.now(TZ).date() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    d = {k: v for k, v in d.items() if str(v.get("day", "")) >= cut}
    from ..adapters import state
    state._write(MADE, d)   # under _locked(); a tmp name of its own (Nov 6)


def _title(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:120]


def _open(stem: str) -> bool:
    from .read import read_note
    from ..adapters.notes import task_path
    p = task_path(stem)
    return bool(p) and read_note(p).get("status") not in ("done", "cancelled")


def stem_for(title: str, made: dict | None = None, open_only: bool = False) -> str | None:
    """The note Carmen made for this card title, if it still exists (newest first). open_only:
    never a closed one (a new message after a Skip is a new card, review Nov 4)."""
    from ..adapters.notes import task_path
    t = _title(title)
    if not t:
        return None
    hits = [(e.get("day", ""), stem) for stem, e in (made if made is not None else load()).items()
            if e.get("title") == t and task_path(stem)]
    for _, stem in sorted(hits, reverse=True):
        if not open_only or _open(stem):
            return stem
    return None


def existing(title: str, made: dict | None = None) -> str | None:
    """An open note this card title already has: one Carmen made for it, or one of that name
    (exactly, or as add_task shortened it, but then only if no other title owns that note)."""
    from ..adapters.notes import note_name
    made = made if made is not None else load()
    s = stem_for(title, made, open_only=True)
    if s:
        return s
    n = note_name(_title(title))
    if n and _open(n) and (n == _title(title) or n not in made or made[n].get("title") == _title(title)):
        return n
    return None


def _body(r: dict, src: str, today: dt.date) -> str:
    from ..adapters.clock import _label
    lines = [f"Created by Carmen from {SRC.get(src, src)} on {_label(today)}."]
    if r.get("s"):
        lines.append(str(r["s"]).strip())
    for c in r.get("ctx") or []:
        if isinstance(c, str) and c.strip():
            lines.append(f"- {c.strip()}")
        elif isinstance(c, dict):
            head = " · ".join(str(c.get(k) or "") for k in ("who", "t") if c.get(k))
            if head:
                lines.append(f"- {head}")
            for q in c.get("q") or []:
                if isinstance(q, str) and q.strip():
                    lines.append(f"  > {q.strip()[:400]}")
    return "\n".join(lines)


def _raw(r: dict) -> dict:
    """What the dropped memory needs to recognise the same message later (dropped.keys)."""
    return {k: r[k] for k in ("n", "s", "nx", "m", "ctx", "ch", "g") if k in r}


def create(r: dict, src: str, today: dt.date | None = None) -> Path | None:
    """The note behind a card (raw row fields n, nx, ne, s, ctx), created once. An existing note
    of that exact title, or one Carmen already made for it, is reused, never duplicated."""
    from tools.todos import add_task
    from ..adapters.clock import TZ, _label
    from ..adapters.notes import _log, task_path, write_from
    from ..signals.read import _real_next
    title = _title(r.get("n"))
    if not title:
        return None
    today = today or dt.datetime.now(TZ).date()
    with _locked():
        made = load()
        hit = existing(title, made)
        if hit:
            return task_path(hit)
        nx = _title(_real_next(r.get("nx", "")))[:200] or title
        mm = re.search(r"\d+", str(r.get("ne") or ""))
        if mm and not re.search(r"\(\d+\s*min\)\s*$", nx):
            nx += f" ({mm.group(0)} min)"
        p = Path(add_task(title, False, None, "", "", None, next_action=nx, body=_body(r, src, today)))
        def created(text):
            head, sep, body = text.partition("\n---\n")
            return head + sep + _log(body, f"- {_label(today)} · created by Carmen ({src}) · check-in app")
        text = p.read_text(encoding="utf-8")
        write_from(p, text, created(text), created)
        made[p.stem] = {"title": title, "day": today.isoformat(), "src": src, "raw": _raw(r),
                        "touched": src == "tap"}   # made by a tap: touched by that tap
        _save(made)
        return p


def attach(rows, src: str, dry: bool = False, today: dt.date | None = None,
           skip_groups: tuple = ()) -> dict:
    """Give every row without a note one, in place, on [[group, [row, ...]], ...]. A row the
    dropped memory hides (he dropped that message within 14 days and nobody wrote since) is
    taken out instead; one with a newer message says so. Returns counts for the job's log."""
    from ..adapters.clock import _label
    from ..signals.read import _plan_today_mod
    from ..adapters.notes import note_name, task_path
    from ..adapters.taplog import log_event
    try:
        dmod = _plan_today_mod("dropped")
        gone = dmod.load()
    except Exception:   # the memory is a nicety
        dmod, gone = None, []
    out = {"created": 0, "linked": 0, "hidden": 0, "failed": 0}
    for pair in rows:
        group, items = pair[0], pair[1]
        if group in skip_groups:
            continue
        keep = []
        for r in items:
            if not isinstance(r, dict) or (r.get("lk") and task_path(r["lk"])):
                keep.append(r)
                continue
            if r.get("lk") and note_name(r["lk"]):   # the link names the note by its long title
                r["lk"] = note_name(r["lk"])
                keep.append(r)
                continue
            r.pop("lk", None)
            if dmod and gone and "meditation" not in str(group).lower():
                rec = dmod.check(r, gone)
                if rec and rec.get("action") == "hide":
                    out["hidden"] += 1
                    continue
                if rec and rec.get("action") == "show":
                    on = str(rec.get("on", ""))
                    r["m"] = "You dropped this" + (f" on {_label(dt.date.fromisoformat(on))}"
                                                   if re.match(r"^\d{4}-\d{2}-\d{2}$", on) else "") + "; new message since"
            if dry:
                keep.append(r)
                continue
            existed = bool(existing(r.get("n", "")))
            try:
                p = create(r, src, today)
            except Exception as exc:   # one bad row stays as it was; the rest still get notes
                log_event("error", where=f"cardnotes.attach {src}", row=str(r.get("n"))[:120], detail=repr(exc)[:200])
                p = None
            if p:
                r["lk"] = p.stem
                r.pop("nw", None)
                out["linked" if existed else "created"] += 1
            else:
                out["failed"] += 1
            keep.append(r)
        pair[1][:] = keep
    return out


def touch(stem: str) -> bool:
    """He did something with this card. Returns whether it had been touched before."""
    with _locked():
        made = load()
        e = made.get(stem)
        if not e:
            return True
        was = bool(e.get("touched"))
        if not was:
            e["touched"] = True
            _save(made)
        return was


def is_made(stem: str) -> bool:
    return stem in load()


def closed(stem: str, via: str, today: dt.date | None = None) -> None:
    """A Carmen note closed or cancelled: into the dropped memory, once, so the same message does
    not come back as a new card (what hiding a note-less row used to do)."""
    from ..adapters.clock import TZ
    from ..signals.read import _plan_today_mod
    from ..adapters.taplog import log_event
    today = today or dt.datetime.now(TZ).date()
    with _locked():
        made = load()
        e = made.get(stem)
        if not e or e.get("dropped"):
            return
        try:
            _plan_today_mod("dropped").record([{**(e.get("raw") or {}), "n": e.get("title", stem)}],
                                              run=f"checkin-app-{today.isoformat()}", via=via)
        except Exception as exc:
            log_event("error", where="cardnotes.closed", detail=repr(exc)[:200])
            return
        e["dropped"] = today.isoformat()
        _save(made)


def sweep(carried: set, today: dt.date, dry: bool = False, busy: set = frozenset()) -> list[str]:
    """The pre-run's clean-up: a note Carmen made before today that he never touched, that is
    still inbox with no date, that is on no Do list (busy) and that this run does not carry, is
    cancelled ("not picked up") and remembered as dropped. Returns the stems cancelled."""
    from ..adapters.clock import _label
    from ..adapters.notes import NoteChanged, _log, _set_fm, task_path, write_from
    from .read import read_note
    out = []
    for stem, e in load().items():
        p = task_path(stem)
        if (not p or e.get("touched") or str(e.get("day", "")) >= today.isoformat()
                or stem in carried or stem in busy):
            continue
        st = read_note(p)
        if st.get("status") != "inbox" or st.get("scheduled") or st.get("due"):
            continue
        # Worked on anywhere (a Telegram chat, Obsidian, prep): any Log line but the creation one.
        log = p.read_text(encoding="utf-8").partition("\n## Log")[2]
        if any(l.startswith("- ") and "created by Carmen" not in l for l in log.splitlines()):
            continue
        if dry:
            out.append(stem)
            continue
        with _locked():   # a tap during the pre-run wins
            e2 = load().get(stem) or {}
            if e2.get("touched"):
                continue
            def cancelled(text):
                head, sep, body = text.partition("\n---\n")
                head = _set_fm(_set_fm(head, "status", "cancelled"), "completedDate", today.isoformat())
                return head + sep + _log(body, f"- {_label(today)} · not picked up, closed by Carmen · check-in app")
            text = p.read_text(encoding="utf-8")
            try:
                write_from(p, text, cancelled(text), cancelled)
            except NoteChanged:   # worked on right now: not a candidate after all
                continue
        out.append(stem)
        closed(stem, "check-in app (not picked up)", today)
    return out
