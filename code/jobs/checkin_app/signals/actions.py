"""Signals, its writes: the day's meditation entry, in the template's exact shape (owner decision, Oct 30: it
still fills the Obsidian page as before). The tap handlers join here when actions.py is split.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

from starlette.responses import JSONResponse

from .. import runs
from ..adapters import clock
from ..adapters.clock import TZ
from ..adapters.taplog import log_event
from ..adapters.taplog import signal
from .journal import entry as meditation_entry


def save_journal(day: dt.date, values: dict, now: dt.datetime | None = None) -> Path:
    """Write the day's entry in the template's exact shape. An existing entry keeps its
    frontmatter, title and any section the template does not know; only the template's
    sections get his new answers."""
    qs = meditation_entry.template_questions()
    p = meditation_entry.JOURNAL / f"{day.isoformat()}.md"
    now = now or dt.datetime.now(clock.TZ)
    bodies = {q["heading"]: meditation_entry._value_to_body(q, values.get(q["heading"])) for q in qs}
    if p.exists():
        text = p.read_text(encoding="utf-8")
        for head, new in bodies.items():
            pat = re.compile(rf"(^## {re.escape(head)}\s*$)(.*?)(?=^## |\Z)", re.M | re.S)
            if pat.search(text):
                text = pat.sub(lambda m: m.group(1) + new + "\n", text, count=1)
            else:
                text = text.rstrip() + f"\n\n## {head}\n{new}"
    else:
        title = f"{day.strftime('%A')}, {day.day} {day.strftime('%B %Y')}"
        text = (f"---\ntype: note\ncaptured: {now.strftime('%Y-%m-%dT%H:%M')}\n---\n\n# {title}\n\n" +
                "\n".join(f"## {q['heading']}\n{bodies[q['heading']]}" for q in qs))
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


def on_journal_save(t):
    body, dry = t.body, t.dry
    jday = dt.datetime.now(TZ).date()
    values = body.get("values") if isinstance(body.get("values"), dict) else {}
    filled = sum(1 for v in values.values() if (v if not isinstance(v, dict) else any(v.values())))
    log_event("journal", test=dry, filled=filled, update=bool(body.get("update")))
    if dry:
        return JSONResponse({"ok": True, "msg": "Test mode, nothing written: would save today's meditation"})
    save_journal(jday, values)
    started = runs.start_update() if body.get("update") else False
    return JSONResponse({"ok": True, "running": started,
                         "msg": "Meditation saved" + (", updating ToDo from it" if started else "")})


def on_meditation(t):
    # Refresh (Oct 31) runs in test mode too: the pre-run is the same one that runs
    # every morning, and test mode is about his taps, not about fresh data.
    started = runs.start_update()
    return JSONResponse({"ok": True, "running": True,
                         "msg": "Updating from new messages and your meditation, about a minute" if started
                         else "Already updating"})


# Client-only moments that still say something about Claude's suggestion.
def on_mark(t):
    body, dry, day_name, tab_name, row = t.body, t.dry, t.day_name, t.tab_name, t.row
    name = str(body.get("signal", ""))[:40]
    if name in ("claude_adjust", "edit_opened"):
        signal(name, row, day_name, test=dry, tab=tab_name)
    elif name == "reply_copied":   # Copy on a draft he sends himself (Oct 31), the text as copied
        sent = str(body.get("text", ""))[:2000]
        d = (row.get("draft") or {}).get("body", "")
        signal(name, row, day_name, test=dry, via=(row.get("draft") or {}).get("via", ""),
               edited=sent.strip() != d.strip(), **({"edit": {"draft": d, "sent": sent}} if sent.strip() != d.strip() else {}),
               wa=bool(body.get("wa")))
    return JSONResponse({"ok": True, "keep": True, "msg": ""})
