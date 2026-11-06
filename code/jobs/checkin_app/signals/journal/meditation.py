"""Signals, journal: today's meditation read live on every load, so an entry written during the day
counts at once; plan-today's own matcher (build_panel.py) ties each of the Two things to its task or
makes it a row.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from runner import config

from .. import read as signals
from ...adapters import notes
from ...adapters import taplog


MED = "From today's meditation"

JOURNAL_DIR = config.VAULT / "Areas" / "Personal" / "Meditation Journal"


def with_meditation(data: dict, today: dt.date, entry: Path | None = None) -> dict:
    """Today's meditation, read live on every load, so one written during the day counts
    at once (owner decision, Oct 31). The pre-run leaves this group empty. plan-today's own matcher
    (build_panel.py) ties each of the "Two things" to its task, or makes it a row of its own,
    exactly as the Mac panel does."""
    entry = entry or JOURNAL_DIR / f"{today.isoformat()}.md"
    if not entry.exists():
        return data
    try:
        bp = signals._plan_today_mod("build_panel")
        if (data.get("journal") or {}).get("sig") == bp.journal_sig(entry.read_text(encoding="utf-8")):
            return data
        items = bp.journal_items(str(entry))
        if not items:
            return data
        groups = {g: list(rows) for g, rows in data.get("rows", [])}
        # Every pre-run row is a task note since Nov 4 (cardnotes), so open_tasks has them all.
        rows, _ = bp.meditation_rows(items, groups, bp.open_tasks())
        for r in rows:   # a link to no note is no link; the first tap creates the note
            if r.get("lk") and not notes.task_path(r["lk"]) and notes.note_name(r["lk"]):
                r["lk"] = notes.note_name(r["lk"])
            if r.get("lk") and not notes.task_path(r["lk"]):
                r.pop("lk")
            r.pop("pend", None)
            # The matcher's rough row until the Refresh judges the entry: the page shows it as
            # being filled in (Nov 3, the owner: "I did not see that they are being updated").
            r["mp"] = 1
    except Exception as exc:   # the meditation is a layer on top; the view must never fail on it
        taplog.log_event("error", where="meditation", detail=str(exc)[:200])
        return data
    return {**data, "rows": [[MED, rows]] + [[g, groups[g]] for g, _ in data.get("rows", []) if g != MED]}
