"""The fixed menu of safe repairs the maintenance brief may attempt.

THE MODEL DOES NOT FIX ANYTHING. Unattended AI jobs here are read-only by
design (scheduled-execution.md gap 1, runner/ask.py: no Write, no Bash), so
"fix what it can" is this list of deterministic, reversible actions in Python,
run before any model turn:

    restart   a known service that is down, once, then re-check
    rerun     a job whose last run crashed, once
    retry     a job whose last run failed a check only on a transient error
    unlock    a git lock file left behind by a dead git process

Anything not on that list is reported, never attempted. The list grows only by
a code change, and each entry is one attempt per item per ISO week, so a repair
that makes things worse can do it once.

Two choices that look like gaps and are not:

- The runner's own job locks are not on the menu. They are flock() locks, which
  the kernel releases when the holding process dies, so a dead job cannot leave
  one that blocks anything. The lock a dead process really strands is git's
  `index.lock`: a killed `git commit` leaves it, and every vault snapshot after
  that fails until someone deletes it.
- `claude-rc` is not on the restart list. Restarting it ends every Remote
  Control session, which cannot be reopened from the phone; that is a decision.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

# Restartable services. Each restarts cleanly and loses nothing. tg-bridge joins
# once that bridge is deployed: add it here, nothing else changes.
SERVICES = ("obsidian-sync", "wa-consumer", "waha", "obsidian-mcp")

# Never re-run by the brief. Itself, obviously. build_brief runs at 14:00 on the
# same Monday, so a re-run of last week's crash would duplicate today's run.
RERUN_EXCLUDE = frozenset({"maintenance_brief", "build_brief"})

# Bounds the brief's own runtime: each re-run can take RERUN_WAIT_S.
MAX_RERUNS = 3
RERUN_WAIT_S = 180
RECHECK_AFTER_S = 30
LOCK_MIN_AGE_S = 600

# A check that failed for one of these reasons may pass on a second try. Matched
# on the check's detail, which is where every job puts the exception text.
TRANSIENT = re.compile(
    r"timed? ?out|timeout|temporar(?:y|ily) (?:failure|unavailable)|"
    r"connection (?:refused|reset|aborted)|network is unreachable|"
    r"name resolution|nodename nor servname|URLError|RemoteDisconnected|"
    r"HTTP 50[234]|could not get the git lock", re.I)

FIXED, NOT_FIXED, SKIPPED, WOULD = "fixed", "not fixed", "skipped", "would attempt"


def stale_lock_paths() -> list[Path]:
    """Resolved per call so a moved vault (tests, VAULT_PATH) is honoured."""
    return [config.VAULT / ".git" / "index.lock",
            config.VAULT / ".git" / "HEAD.lock",
            config.HOME / "automation" / ".git" / "index.lock"]


@dataclass
class Action:
    kind: str       # restart | rerun | retry | unlock
    target: str     # a unit, a job, or a path
    why: str        # what was wrong
    outcome: str = ""
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.target}"


def _sh(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, f"{type(exc).__name__}: {exc}"


class Repairs:
    """One pass over the menu. `attempted` is {key: iso_week} from last time,
    updated in place; the caller persists it (Job.remember, which a dry run
    refuses, so a rehearsal never uses up a week's attempt)."""

    def __init__(self, *, attempted: dict, week: str, dry_run: bool,
                 sh=_sh, sleep=time.sleep):
        self.attempted = attempted
        self.week = week
        self.dry_run = dry_run
        self.sh = sh
        self.sleep = sleep
        self.actions: list[Action] = []
        self._reruns = 0

    def run(self, states: list[dict]) -> list[Action]:
        self._restart_services()
        self._unlock()
        self._rerun_jobs(states)
        return self.actions

    # ---- gate --------------------------------------------------------------

    def _may(self, action: Action) -> bool:
        """Record the action and say whether to perform it."""
        self.actions.append(action)
        if self.attempted.get(action.key) == self.week:
            action.outcome = SKIPPED
            action.detail = "already attempted this week; one attempt per item per week"
            return False
        if self.dry_run:
            action.outcome = WOULD
            action.detail = "dry run, nothing done"
            return False
        self.attempted[action.key] = self.week
        return True

    # ---- the menu ----------------------------------------------------------

    def _active(self, unit: str) -> bool:
        return self.sh(["systemctl", "is-active", f"{unit}.service"], 30)[1] == "active"

    def _restart_services(self) -> None:
        for unit in SERVICES:
            if self._active(unit):
                continue
            a = Action("restart", unit, why=f"{unit} was not active")
            if not self._may(a):
                continue
            rc, out = self.sh(["sudo", "-n", "systemctl", "restart", f"{unit}.service"], 120)
            self.sleep(RECHECK_AFTER_S)
            if self._active(unit):
                a.outcome, a.detail = FIXED, f"restarted; active again after {RECHECK_AFTER_S}s"
            else:
                a.outcome = NOT_FIXED
                a.detail = (f"restart exit {rc}" + (f" ({out[:160]})" if out else "")
                            + f"; still not active after {RECHECK_AFTER_S}s")

    def _git_running(self) -> bool:
        return self.sh(["pgrep", "-x", "git"], 15)[0] == 0

    def _unlock(self) -> None:
        for path in stale_lock_paths():
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                continue
            if age < LOCK_MIN_AGE_S:
                continue
            if self._git_running():
                # A live git may own it. Not stale by our definition; next week.
                continue
            a = Action("unlock", str(path),
                       why=f"{path.name} {age / 60:.0f} min old with no git process running")
            if not self._may(a):
                continue
            try:
                path.unlink()
                a.outcome, a.detail = FIXED, "removed"
            except OSError as exc:
                a.outcome, a.detail = NOT_FIXED, f"{type(exc).__name__}: {exc}"

    def _has_timer(self, job: str) -> bool:
        return self.sh(["systemctl", "is-enabled", f"automation@{job}.timer"], 25)[1] == "enabled"

    def _rerun_jobs(self, states: list[dict]) -> None:
        for s in sorted(states, key=lambda x: str(x.get("job", ""))):
            job = s.get("job")
            if not isinstance(job, str) or job in RERUN_EXCLUDE or s.get("dry_run"):
                continue
            if s.get("status") == "error":
                kind, why, watch = "rerun", f"last run crashed: {s.get('note', '')[:160]}", []
            elif s.get("status") == "problem":
                failing = [c for c in s.get("checks", []) if not c.get("ok")]
                # A job with a failing critical check already pushes on every
                # run; a re-run would only push again.
                if any(c.get("tier") == "critical" for c in failing):
                    continue
                watch = [c for c in failing if TRANSIENT.search(c.get("detail") or "")]
                if not watch:
                    continue
                kind = "retry"
                why = "failed on a transient error: " + "; ".join(
                    f"{c['name']}: {c.get('detail', '')[:100]}" for c in watch[:2])
            else:
                continue
            if self._reruns >= MAX_RERUNS or not self._has_timer(job):
                continue
            a = Action(kind, job, why=why)
            if not self._may(a):
                continue
            self._reruns += 1
            new = self._rerun(job, s.get("finished"))
            if new is None:
                a.outcome = NOT_FIXED
                a.detail = f"started, no new result within {RERUN_WAIT_S}s"
            elif kind == "rerun":
                ok = new.get("status") in ("ok", "problem")
                a.outcome = FIXED if ok else NOT_FIXED
                a.detail = f"re-ran, status {new.get('status')}"
            else:
                keys = {c.get("key") for c in watch}
                still = [c for c in new.get("checks", []) if not c.get("ok") and c.get("key") in keys]
                a.outcome = NOT_FIXED if still else FIXED
                a.detail = ("re-ran; " + (f"{len(still)} check(s) still failing" if still
                                          else "the transient check(s) pass now"))

    def _rerun(self, job: str, before) -> dict | None:
        """Start the job through systemd (so OnFailure= still applies), then
        wait for its state file to change. None if it did not finish in time."""
        self.sh(["sudo", "-n", "systemctl", "start", "--no-block", f"automation@{job}.service"], 30)
        path = config.STATE_DIR / f"{job}.json"
        waited = 0
        while waited <= RERUN_WAIT_S:
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if state.get("finished") != before and state.get("status") != "skipped":
                    return state
            except (OSError, ValueError):
                pass
            self.sleep(10)
            waited += 10
        return None
