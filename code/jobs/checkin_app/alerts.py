"""Carmen tells the owner when something breaks (Nov 6, refactor plan finding 3).

Until now an error went only into taps.jsonl, so 328 failed preparations on Nov 1 and 2 and a
crash on the empty Do screen went unseen. Now:

  - every logged error (log_event "error") and every alert() reaches his phone through
    runner.notify.send_push, at most once per `where` per hour (RATE_S), in a background
    thread so a tap never waits on Pushover;
  - health() is the one answer to "is Carmen working": the pre-run ran today, the last Refresh
    exited 0, prep is not failing, the view builds. /api/health serves it (localhost only), and
    the server's watch loop pushes when it turns red.

Push is quiet (-1) for errors and normal (0) for a red health check. Nothing here raises.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path

from runner import config

STATE = config.STATE_DIR / "checkin_app" / "alerts.json"
RATE_S = 3600
_lock = threading.Lock()
# Errors that are expected noise or already shown on screen: logged, never pushed.
QUIET = {"test", "row"}


def _load() -> dict:
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_name(f"{STATE.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE)
    except OSError:
        pass


def _send(title: str, message: str, priority: int) -> None:
    try:
        from runner import notify
        notify.send_push(title, message, priority=priority)
    except Exception:   # no keys (tests), Pushover down: the log still has it
        pass


def alert(where: str, detail: str, *, priority: int = -1, now: float | None = None) -> bool:
    """Push one line about `where`, unless the same `where` was pushed in the last hour.
    Returns whether it was sent."""
    if not where or where in QUIET:
        return False
    now = now or time.time()
    with _lock:
        d = _load()
        last = d.get(where, 0)
        if isinstance(last, (int, float)) and now - last < RATE_S:
            return False
        d = {k: v for k, v in d.items() if isinstance(v, (int, float)) and now - v < 7 * 86400}
        d[where] = now
        _save(d)
    threading.Thread(target=_send, args=(f"Carmen: {where}", str(detail)[:400], priority), daemon=True).start()
    return True


def health(today: dt.date | None = None, now: dt.datetime | None = None) -> dict:
    """{"ok": bool, "checks": {name: {"ok", "detail"}}}. Reads only."""
    from .signals.read import PAGE
    from .runs import RUN_RC
    from .adapters.taplog import LOG
    from .adapters.clock import TZ
    now = now or dt.datetime.now(TZ)
    today = today or now.date()
    checks = {}

    def put(name, ok, detail=""):
        checks[name] = {"ok": bool(ok), "detail": str(detail)[:200]}

    # The 08:00 pre-run wrote today's page (by 08:45, after its 20 min model call).
    try:
        import re
        gen = re.search(r"^generated:\s*(\S+)", PAGE.read_text(encoding="utf-8"), re.M)
        g = gen.group(1)[:10] if gen else ""
        put("prerun", g == today.isoformat() or now.time() < dt.time(8, 45), f"generated {g or 'never'}")
    except OSError as exc:
        put("prerun", False, f"page unreadable: {exc}")
    # The last Refresh or pre-run on demand exited 0.
    try:
        rc = RUN_RC.read_text().strip() if RUN_RC.exists() else ""
        put("last_run", rc in ("", "0"), f"exit {rc or 'none'}")
    except OSError as exc:
        put("last_run", False, str(exc))
    # Prep and other errors in the last hour, from the tap log's tail.
    fails = errors = 0
    try:
        cutoff = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        with LOG.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            for line in f.read().decode("utf-8", "replace").splitlines()[1:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("at", "") < cutoff:
                    continue
                fails += e.get("kind") == "prep_failed"
                errors += e.get("kind") == "error" and e.get("where") not in QUIET
    except OSError:
        pass
    put("prep", fails < 3, f"{fails} failed in the last hour")
    put("errors", errors < 5, f"{errors} errors in the last hour")
    return {"ok": all(c["ok"] for c in checks.values()), "at": now.isoformat(timespec="seconds"), "checks": checks}


def watch(view_ok: bool | None = None) -> dict:
    """One health pass for the server's loop: push what is red (rate-limited per check)."""
    h = health()
    if view_ok is False:
        h["checks"]["view"] = {"ok": False, "detail": "the view did not build"}
        h["ok"] = False
    for name, c in h["checks"].items():
        if not c["ok"]:
            alert(f"health {name}", c["detail"], priority=0)
    return h
