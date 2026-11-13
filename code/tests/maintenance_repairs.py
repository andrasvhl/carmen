#!/usr/bin/env python3
"""The repair menu does only what is listed, once per item per week.

    ~/automation/.venv/bin/python -m tests.maintenance_repairs

The maintenance brief fixes things before any model runs, in Python, from a
fixed menu (runner/repairs.py). The risk is a repair that makes things worse,
so the limits are the point, and each is asserted here:

  1. Only listed actions run. Every command issued is matched against the
     menu's shapes; a down service off the list (claude-rc) is never touched.
  2. One attempt per item per ISO week. A second pass the same week skips; the
     next week may try again.
  3. A dry run performs nothing and uses up no attempt.
  4. Each repair re-checks and reports fixed or not fixed honestly.
  5. Re-runs are bounded, skip excluded jobs, and never re-run a job whose
     failing check is critical (it already pushes every run).
  6. A git lock is cleared only when old and no git process is running.
"""
import json
import os
import pathlib
import re
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.VAULT = TMP / "vault"
(config.VAULT / ".git").mkdir(parents=True)
config.HOME = TMP / "home"
config.HOME.mkdir()
config.STATE_DIR = TMP / "state"
config.LOCK_DIR = config.STATE_DIR / "locks"
config.ensure_dirs()

from runner import repairs as R

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


# Every command the menu may issue. Anything else is a bug in the menu.
ALLOWED = [
    re.compile(r"^systemctl is-active [\w-]+\.service$"),
    re.compile(r"^systemctl is-enabled automation@\w+\.timer$"),
    re.compile(r"^sudo -n systemctl restart (obsidian-sync|wa-consumer|waha|obsidian-mcp)\.service$"),
    re.compile(r"^sudo -n systemctl start --no-block automation@\w+\.service$"),
    re.compile(r"^pgrep -x git$"),
]


class Box:
    """A fake systemd. `down` services stay down unless `heals` says otherwise;
    a started job writes `rerun_result` into its state file."""

    def __init__(self, down=(), heals=(), timers=(), git_running=False, rerun_result=None):
        self.down, self.heals = set(down), set(heals)
        self.timers, self.git_running = set(timers), git_running
        self.rerun_result = rerun_result or {}
        self.cmds = []

    def sh(self, cmd, timeout):
        line = " ".join(cmd)
        self.cmds.append(line)
        if cmd[:2] == ["systemctl", "is-active"]:
            return 0, "inactive" if cmd[2][:-8] in self.down else "active"
        if cmd[:2] == ["systemctl", "is-enabled"]:
            return 0, "enabled" if cmd[2][len("automation@"):-6] in self.timers else "disabled"
        if cmd[:4] == ["sudo", "-n", "systemctl", "restart"]:
            unit = cmd[4][:-8]
            if unit in self.heals:
                self.down.discard(unit)
            return 0, ""
        if cmd[:5] == ["sudo", "-n", "systemctl", "start", "--no-block"]:
            job = cmd[5][len("automation@"):-8]
            if job in self.rerun_result:
                (config.STATE_DIR / f"{job}.json").write_text(json.dumps(self.rerun_result[job]))
            return 0, ""
        if cmd[:2] == ["pgrep", "-x"]:
            return (0, "123") if self.git_running else (1, "")
        return 127, "unexpected"

    def off_menu(self):
        return [c for c in self.cmds if not any(p.match(c) for p in ALLOWED)]


def repairs(box, attempted=None, week="2026-W41", dry=False):
    return R.Repairs(attempted={} if attempted is None else attempted, week=week,
                     dry_run=dry, sh=box.sh, sleep=lambda s: None)


def state(job, status, checks=(), finished="2026-10-01T00:00:00+00:00", **kw):
    return {"job": job, "status": status, "finished": finished, "checks": list(checks), **kw}


# --- 1. a down service on the list is restarted once, and re-checked ---------
box = Box(down={"obsidian-sync", "claude-rc"}, heals={"obsidian-sync"})
rp = repairs(box)
acts = rp.run([])
check("a listed down service is restarted",
      "sudo -n systemctl restart obsidian-sync.service" in box.cmds, str(box.cmds))
check("...and reported fixed after the re-check",
      [(a.kind, a.target, a.outcome) for a in acts] == [("restart", "obsidian-sync", "fixed")],
      str(acts))
check("a down service NOT on the list is never restarted",
      not any("claude-rc" in c and "restart" in c for c in box.cmds), str(box.cmds))
check("nothing off the menu was run", box.off_menu() == [], str(box.off_menu()))
check("the menu is exactly the four services the plan names",
      R.SERVICES == ("obsidian-sync", "wa-consumer", "waha", "obsidian-mcp"), str(R.SERVICES))

box = Box(down={"waha"})
acts = repairs(box).run([])
check("a restart that does not bring it back is reported not fixed",
      acts and acts[0].outcome == "not fixed", str(acts))

# --- 2. one attempt per item per week ------------------------------------------
attempted = {}
box = Box(down={"waha"})
repairs(box, attempted).run([])
box2 = Box(down={"waha"})
acts = repairs(box2, attempted).run([])
check("a second pass in the same week does not restart again",
      not any("restart" in c for c in box2.cmds), str(box2.cmds))
check("...and says it skipped", acts and acts[0].outcome == "skipped", str(acts))
box3 = Box(down={"waha"})
repairs(box3, attempted, week="2026-W42").run([])
check("the next week may try again", any("restart waha" in c for c in box3.cmds), str(box3.cmds))

# --- 3. a dry run does nothing and uses up nothing ------------------------------
attempted = {}
box = Box(down={"waha"}, timers={"wiki"})
acts = repairs(box, attempted, dry=True).run([state("wiki", "error", note="boom")])
check("a dry run issues no restart and no start",
      not any(c.startswith("sudo") for c in box.cmds), str(box.cmds))
check("...reports what it would do", {a.outcome for a in acts} == {"would attempt"}, str(acts))
check("...and records no attempt", attempted == {}, str(attempted))

# --- 4/5. re-runs ----------------------------------------------------------------
NEW_OK = state("wiki", "ok", finished="2026-10-05T21:06:00+00:00")
box = Box(timers={"wiki", "build_brief", "status"}, rerun_result={"wiki": NEW_OK})
(config.STATE_DIR / "wiki.json").write_text(json.dumps(state("wiki", "error")))
acts = repairs(box).run([state("wiki", "error", note="KeyError: x"),
                         state("build_brief", "error"),
                         state("maintenance_brief", "error"),
                         state("selftest", "error")])
kinds = [(a.kind, a.target, a.outcome) for a in acts]
check("a crashed job on a timer is re-run once and reported fixed",
      ("rerun", "wiki", "fixed") in kinds, str(kinds))
check("excluded jobs are never re-run (build_brief, itself)",
      not any("build_brief" in c or "maintenance_brief" in c for c in box.cmds if "start" in c),
      str(box.cmds))
check("a job with no timer is not re-run", not any("selftest" in c and "start" in c for c in box.cmds))
check("re-runs go through systemd, so OnFailure still applies",
      "sudo -n systemctl start --no-block automation@wiki.service" in box.cmds)
check("nothing off the menu was run", box.off_menu() == [], str(box.off_menu()))

transient = state("status", "problem", checks=[
    {"name": "fetch", "key": "fetch", "ok": False, "tier": "important",
     "detail": "URLError: <urlopen error timed out>"}])
box = Box(timers={"status"}, rerun_result={"status": state(
    "status", "ok", finished="2026-10-05T21:07:00+00:00",
    checks=[{"name": "fetch", "key": "fetch", "ok": True}])})
acts = repairs(box).run([transient])
check("a check that failed on a transient error gets its job re-run",
      [(a.kind, a.outcome) for a in acts] == [("retry", "fixed")], str(acts))

persistent = state("status", "problem", checks=[
    {"name": "drift", "key": "drift", "ok": False, "tier": "important", "detail": "3 units differ"}])
box = Box(timers={"status"})
check("a non-transient failure is not retried", repairs(box).run([persistent]) == [])

crit = state("heartbeat", "problem", checks=[
    {"name": "x", "key": "x", "ok": False, "tier": "critical", "detail": "timed out"}])
box = Box(timers={"heartbeat"})
check("a job failing a critical check is not retried (it already pushes every run)",
      repairs(box).run([crit]) == [])

many = [state(f"job{i}", "error") for i in range(6)]
box = Box(timers={f"job{i}" for i in range(6)})
acts = repairs(box).run(many)
check(f"re-runs are capped at {R.MAX_RERUNS} per pass",
      sum(1 for c in box.cmds if "--no-block" in c) == R.MAX_RERUNS, str(box.cmds))
check("a re-run with no new result is reported not fixed",
      acts and all(a.outcome == "not fixed" for a in acts), str(acts))

dry_state = state("wiki", "error", dry_run=True)
box = Box(timers={"wiki"})
check("a crash recorded by a dry run is not re-run", repairs(box).run([dry_state]) == [])

# --- 6. stale git locks ------------------------------------------------------------
lock = config.VAULT / ".git" / "index.lock"
lock.write_text("")
box = Box()
check("a fresh lock is left alone", repairs(box).run([]) == [] and lock.exists())

old = time.time() - 3600
os.utime(lock, (old, old))
box = Box(git_running=True)
check("an old lock is left alone while git is running",
      repairs(box).run([]) == [] and lock.exists())

box = Box()
acts = repairs(box).run([])
check("an old lock with no git process is removed",
      [(a.kind, a.outcome) for a in acts] == [("unlock", "fixed")] and not lock.exists(), str(acts))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
