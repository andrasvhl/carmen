"""Signals, what other modules read: the pre-run's rows (the evidence judge's proposals and drafts from
mail, WhatsApp and the meditation), with the Refresh's rows laid over them while they were built on
this pre-run, every field checked once on the way in. Also the loader for the plan-today skill's own
code (mailer, build_panel).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from runner import config

from ..adapters import notes
from .journal import meditation, mentions   # re-exported: Planning ranks by today's meditation and its mentions


PAGE = config.VAULT / "_system" / "claude" / "plan-today-prerun.md"

# The rows are model output, from the pre-run and from the quick update. On Oct 31 one wrote
# "lk": 1 and another an estimate as a bare number, and each took the whole view down (16 times
# that evening). So every row is checked here, once, on the way in: a field of the wrong type is
# fixed or dropped, a row with no name is dropped, and nothing downstream has to guard (Nov 1).
_STR_KEYS = ("n", "g", "s", "wd", "nx", "m", "upd", "wf", "p", "bk", "wb", "ov")


def _real_next(nx: str) -> str:
    """A panel row's nx as a step, or "" when it is a verdict ("Mark as done", "Nothing left")."""
    return "" if str(nx or "").strip().lower().startswith(("mark as done", "nothing left")) else str(nx or "")


_LIST_KEYS = ("ctx", "rw", "steps", "td")

_MSG_STR = ("via", "to", "subj", "body", "acct")


def _as_str(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _est(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{int(v)}m"
    return v if isinstance(v, str) else None


def clean_row(r) -> dict | None:
    if not isinstance(r, dict):
        return None
    r = dict(r)
    for k in _STR_KEYS:
        if k in r:
            v = _as_str(r[k])
            if v is None:
                r.pop(k)
            else:
                r[k] = v
    for k in ("e", "ne"):
        if k in r:
            v = _est(r[k])
            if v is None:
                r.pop(k)
            else:
                r[k] = v
    if "lk" in r and not (isinstance(r["lk"], str) and r["lk"]):
        # "lk": 1 meant "linked": keep the link when the title is a task note's name.
        if r["lk"] and isinstance(r.get("n"), str) and notes.note_name(r["n"]):
            r["lk"] = notes.note_name(r["n"])
        else:
            r.pop("lk")
    elif r.get("lk") and not notes.task_path(r["lk"]) and notes.note_name(r["lk"]):
        r["lk"] = notes.note_name(r["lk"])   # the note's file name, not the long title
    if not r.get("n"):
        if not r.get("lk"):
            return None
        r["n"] = r["lk"]
    for k in _LIST_KEYS:
        if k in r and not isinstance(r[k], list):
            r.pop(k)
    ch = []
    for a in r.get("ch") if isinstance(r.get("ch"), list) else []:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if "t" in a:
            a["t"] = _as_str(a["t"]) or ""
        if "msg" in a:
            msg = a["msg"]
            if isinstance(msg, dict) and msg:
                msg = {**msg, **{k: _as_str(msg[k]) or "" for k in _MSG_STR if k in msg}}
                if "slots" in msg and not isinstance(msg["slots"], list):
                    msg.pop("slots")
                a["msg"] = msg
            else:
                a.pop("msg")
        if a.get("msg") or a.get("t"):
            ch.append(a)
    if "ch" in r:
        r["ch"] = ch
    if "cp" in r:
        r["cp"] = [{**a, "t": _as_str(a.get("t")) or ""} for a in r["cp"]
                   if isinstance(a, dict) and _as_str(a.get("t"))] if isinstance(r["cp"], list) else []
    return r


def clean_rows(rows) -> list:
    """[[group, [row, ...]], ...] with every group a string and every row through clean_row.
    A dict of groups is accepted too; anything else is no rows."""
    if isinstance(rows, dict):
        rows = list(rows.items())
    out = []
    for pair in rows if isinstance(rows, list) else []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], list)):
            continue
        group = _as_str(pair[0])
        if group is None:
            continue
        items = [c for c in (clean_row(r) for r in pair[1]) if c]
        if items:
            out.append([group, items])
    return out


def load_prerun(path: Path = PAGE, overlay: bool = True) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:   # a half-written pre-run: no rows rather than no app
        data = {}
    data = data if isinstance(data, dict) else {}
    data["rows"] = clean_rows(data.get("rows"))
    data["notes"] = [n for n in data.get("notes") or [] if isinstance(n, str)] if isinstance(data.get("notes"), list) else []
    if not isinstance(data.get("journal"), dict):
        data["journal"] = {}
    gen = re.search(r"^generated:\s*(\S+)", text, re.M)
    data["generated"] = gen.group(1) if gen else ""
    # The quick update (update.py: messages since the pre-run and today's meditation) stands
    # in for the pre-run's rows while it was built on this very pre-run (Oct 31).
    if overlay:
        try:
            up = json.loads(UPDATE.read_text(encoding="utf-8"))
            rows = clean_rows(up.get("rows")) if isinstance(up, dict) else []
            if up.get("prerun") == data["generated"] and rows:
                notes = up.get("notes") if isinstance(up.get("notes"), list) else []
                data = {**data, "rows": rows,
                        "journal": up.get("journal") if isinstance(up.get("journal"), dict) and up["journal"] else data["journal"],
                        "updated": _as_str(up.get("at")) or "",
                        "notes": [n for n in notes if isinstance(n, str)] or data["notes"]}
        except (OSError, ValueError, AttributeError):
            pass
    return data


def _plan_today_mod(name: str):
    """A module from the plan-today skill folder, which the Mac panel runs from too."""
    import importlib
    import sys
    skill = str(config.VAULT / "_system" / "skills" / "plan-today")
    if skill not in sys.path:
        sys.path.insert(0, skill)
    return importlib.import_module(name)


UPDATE = config.STATE_DIR / "checkin_app" / "update.json"   # the Refresh's rows (update.py)
