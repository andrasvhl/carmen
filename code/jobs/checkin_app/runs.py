"""Background runs, owning nothing: the pre-run and the Refresh (update.py) in one slot, Claude's task
prep (prep.py) in another; one of each at a time, a hung run ended past its limits, a finished child
reaped. They read modules and write through their actions.
"""
from __future__ import annotations

import os
import signal as signal_mod
import time
from pathlib import Path

from runner import config

from .adapters import taplog


# --- the meditation button ----------------------------------------------------------
# "From meditation" re-runs the plan-today pre-run now (stage 5's button, Oct 30):
# it reads the latest journal entry with mail, chats and calendar and rewrites
# the rows. One run at a time; its exit code says whether it worked.


RUN_DIR = config.STATE_DIR / "checkin_app"

RUN_PID, RUN_RC = RUN_DIR / "prerun.pid", RUN_DIR / "prerun.rc"

# Each background run, by a word in its command line, and how long it may take: the sum of
# its own timeouts plus slack. update.py: fetch 180 s + ask 600 s; the pre-run: ask 1200 s plus
# its fetches; prep: up to 3 tasks at 900 s each.
RUN_LIMITS = {"jobs.checkin_app.update": 20 * 60, "plan_today_prerun": 45 * 60, "jobs.checkin_app.prep": 60 * 60}


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _alive(pid: int, pidfile: Path | None = None) -> bool:
    # Runs are our children: reap a finished one, or it lingers as a zombie that
    # os.kill still reports alive and every later start is refused.
    if pid <= 0:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    # After a server restart the run is no longer our child, and its pid can be reused by
    # anything; only a process that is still one of our runs counts (Nov 1).
    cmd = _cmdline(pid)
    kind = next((k for k in RUN_LIMITS if k in cmd), None)
    if not kind:
        return False
    # A run past every timeout it has is hung: end it, so Refresh and Prepare work again.
    try:
        age = time.time() - pidfile.stat().st_mtime if pidfile else 0
    except OSError:
        age = 0
    if age > RUN_LIMITS[kind]:
        try:
            os.killpg(pid, signal_mod.SIGTERM)
        except OSError:
            pass
        taplog.log_event("error", where="run timeout", run=kind, minutes=round(age / 60))
        return False
    return True


def prerun_state() -> dict:
    try:
        pid = int(RUN_PID.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    rc = RUN_RC.read_text().strip() if RUN_RC.exists() else ""
    return {"running": bool(pid and _alive(pid, RUN_PID)), "last_ok": rc == "0" if rc else None}


def start_update() -> bool:
    """The quick update in the pre-run's slot: one at a time, same pid and exit-code files,
    so the page's polling and "Updating…" work unchanged."""
    import subprocess
    if prerun_state()["running"]:
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_RC.unlink(missing_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "update.log").open("a")
    py = home / ".venv" / "bin" / "python"
    proc = subprocess.Popen(
        ["sh", "-c", f'PYTHONPATH=. "{py}" -m jobs.checkin_app.update; echo $? > "{RUN_RC}"'],
        cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    RUN_PID.write_text(str(proc.pid))
    return True


def start_prerun() -> bool:
    import subprocess
    if prerun_state()["running"]:
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_RC.unlink(missing_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "prerun.log").open("w")
    proc = subprocess.Popen(
        ["sh", "-c", f'"{home}/bin/run-job" plan_today_prerun; echo $? > "{RUN_RC}"'],
        cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    RUN_PID.write_text(str(proc.pid))
    return True


# --- Claude prepares tasks (prep.py) ------------------------------------------------
PREP_PID, PREP_GEN = RUN_DIR / "prep.pid", RUN_DIR / "prep.generation"


def prep_running() -> bool:
    try:
        return _alive(int(PREP_PID.read_text().strip()), PREP_PID)
    except (OSError, ValueError):
        return False


def start_prep(task: str | None = None) -> bool:
    """One prep process at a time; a task-specific one waits for the next turn."""
    import subprocess
    if prep_running():
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "prep.log").open("a")
    cmd = [str(home / ".venv" / "bin" / "python"), "-m", "jobs.checkin_app.prep"] + (["--task", task] if task else [])
    proc = subprocess.Popen(cmd, cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PREP_PID.write_text(str(proc.pid))
    return True


def maybe_start_prep(generated: str) -> None:
    """Once per pre-run: prepare the next few tasks in the background."""
    try:
        from . import prep
        if generated and not (PREP_GEN.exists() and PREP_GEN.read_text().strip() == generated):
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            if start_prep():
                PREP_GEN.write_text(generated)
            return
        # An answer that arrived while Claude was busy is picked up as soon as it is free.
        # ... unless that task's last try failed and is backing off (Nov 6: no retry storms).
        if not prep_running() and any(isinstance(st := prep.load_state_file(f), dict) and st.get("pending")
                                      and prep.retry_due(st) for f in prep.STATE.glob("*.json")):
            start_prep()
    except Exception as exc:   # background work; the view never waits on or fails for it
        taplog.log_event("error", where="maybe_start_prep", detail=repr(exc)[:200])


def attach_prep(view: dict) -> None:
    """Each card that Claude prepared carries done / asks / then for the Claude tab."""
    from . import prep
    t = view["today"]
    for r in view["claude"] + ([t["one"]] if t["one"] else []) + t["needs"] + t["more"]:
        st = prep.load_state(r["id"])
        if isinstance(st, dict) and st:
            r["prepared"] = {k: st.get(k) for k in ("result", "answers", "pending", "at", "error", "asked")}
    view["prep_running"] = prep_running()
