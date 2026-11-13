"""The tap log (STATE_DIR/checkin_app/taps.jsonl): every open, tap, signal and error, one JSON line
each. An error also reaches his phone through alerts.py. Shared by every module; it owns no other
data.
"""
from __future__ import annotations

import datetime as dt
import json

from runner import config

from . import clock


# --- the tap log ------------------------------------------------------------------
# Replaces the asked retros (the owner stopped answering them after Oct 15): every
# open and every tap, so the weekly summary can say how the check-in is used.


LOG = config.STATE_DIR / "checkin_app" / "taps.jsonl"


def signal(name: str, row: dict | None, day: str = "today", **extra) -> None:
    """One disagreement or agreement with Claude, with what Claude had suggested,
    so the weekly summary and the pre-run can learn from it (adopted Oct 30)."""
    claude = {}
    if row:
        claude = {k: row.get(k) for k in ("guess", "group", "prio", "unsure", "tab", "next", "est")}
    log_event("signal", signal=name, day=day, id=(row or {}).get("id", ""),
              title=(row or {}).get("title", ""), claude=claude, **extra)


def log_event(kind: str, **fields) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": dt.datetime.now(clock.TZ).isoformat(timespec="seconds"),
                                "kind": kind, **fields}, ensure_ascii=False) + "\n")
    except OSError:
        pass   # the log must never break a tap
    if kind == "error":   # and his phone hears of it, once an hour per place (alerts.py, Nov 6)
        try:
            from .. import alerts
            alerts.alert(str(fields.get("where") or "error"), str(fields.get("detail") or "")[:300])
        except Exception:
            pass
