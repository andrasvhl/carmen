#!/usr/bin/env python3
"""Two runner signals that could never change: crash recovery and RC idle time.

    ~/automation/.venv/bin/python -m tests.runner_recovery

  1. A crash counts "in a row" only without a clean run since the last one.
     The crash being counted had already written status=error over the state
     file, so no recovery was ever seen: carmen-deploy crashed Nov 1, 5, 6 and
     10, cleanly in between, and each was pushed as critical (Nov 10).

  2. A Remote Control session's idle time comes from inbound phone events only.
     The CLI logs SSE reconnects every few minutes on every session, so every
     session read "idle 0.0d" and rc_reap never closed one (another install,
     7 sessions, 1.8GB on a 4GB box, Nov 10).
"""
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.STATE_DIR = TMP
config.VAULT = TMP / "vault"
config.INBOX = config.VAULT / "Inbox"
config.INBOX.mkdir(parents=True)

from runner import alert, rc_sessions
from runner.job import Job

fails = []
UTC = timezone.utc


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def job_run(name, crash=False):
    try:
        with Job(name, description=name, dry_run=True):
            if crash:
                raise RuntimeError("boom")
    except RuntimeError:
        pass


print("Crash counter, a Job")
t0 = datetime.now(UTC)
job_run("jobx", crash=True)
check("first crash counts 1", alert.consecutive_crashes("jobx", t0) == 1)
job_run("jobx")
job_run("jobx", crash=True)   # writes status=error before the alert reads the file
n = alert.consecutive_crashes("jobx", datetime.now(UTC))
check("a clean run in between resets it, though the crash wrote status=error", n == 1, f"got {n}")
job_run("jobx", crash=True)
n = alert.consecutive_crashes("jobx", datetime.now(UTC))
check("two crashes with nothing between count 2", n == 2, f"got {n}")
st = json.loads((TMP / "jobx.json").read_text())
check("the error write keeps last_success", st["status"] == "error" and st.get("last_success"))

# A state file from before last_success existed, then one error write.
(TMP / "jobold.json").write_text(json.dumps({"status": "ok", "finished": (t0 - timedelta(days=3)).isoformat()}))
job_run("jobold", crash=True)
check("an old clean `finished` becomes last_success",
      json.loads((TMP / "jobold.json").read_text()).get("last_success") == (t0 - timedelta(days=3)).isoformat())

print("Crash counter, a plain systemd unit (no state file)")
daily = [datetime(2026, 11, d, 12, 30, tzinfo=UTC) for d in range(1, 15)]
real_show, real_next = alert._systemctl_show, alert._next_elapses


def fake(start):
    def show(unit, prop, *extra):
        return {"TriggeredBy": "carmen-deploy.timer",
                "ExecMainStartTimestamp": f"@{start.timestamp()}",
                "TimersCalendar": "{ OnCalendar=*-*-* 04:30:00 America/Los_Angeles ; next_elapse=x }"}[prop]

    def nxt(spec, base, n):
        return [d for d in daily if d > base][:n]
    alert._systemctl_show, alert._next_elapses = show, nxt


crash = lambda d: datetime(2026, 11, d, 12, 33, tzinfo=UTC)
fake(datetime(2026, 11, 6, 12, 30, 20, tzinfo=UTC))
check("Nov 5 then Nov 6: the very next run, in a row", not alert._timer_ran_between("x", crash(5)))
fake(datetime(2026, 11, 10, 12, 30, 30, tzinfo=UTC))
check("Nov 6 then Nov 10: runs on Nov 7 to 9 between, recovered", alert._timer_ran_between("x", crash(6)))
fake(datetime(2026, 11, 5, 20, 0, tzinfo=UTC))
check("a manual run the same evening, in a row", not alert._timer_ran_between("x", crash(5)))
alert._systemctl_show, alert._next_elapses = real_show, real_next
seen = []
alert._timer_ran_between = lambda unit, last: seen.append(unit) or False
alert.consecutive_crashes("nostate", t0, "carmen-deploy.service")
alert.consecutive_crashes("nostate", datetime.now(UTC), "carmen-deploy.service")
check("the unit systemd names is the one whose timer is read", seen == ["carmen-deploy.service"], f"got {seen}")

try:   # the real systemd-analyze, where there is one: its output still parses
    got = alert._next_elapses("*-*-* 04:30:00 America/Los_Angeles", crash(5), 2)
    check("systemd-analyze output parses", got == daily[5:7], f"got {got}")
except FileNotFoundError:
    print("  skip  systemd-analyze not on this machine")

print("Remote Control idle time")
rc_sessions.LOG_DIR = TMP
now = datetime.now(UTC)
ts = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def logfile(sid, lines):
    (TMP / f"bridge-session-{sid}.log").write_text("\n".join(lines) + "\n")
    return rc_sessions.idle_days(sid)


noise = [f"{ts(0)} [DEBUG] CCRClient: Heartbeat sent",
         f"{ts(0)} [DEBUG] SSETransport: Stream ended, reconnecting",
         f"{ts(0)} [DEBUG] SSETransport: Refreshed headers for reconnect",
         f"{ts(0)} [DEBUG] SSETransport: Reconnecting in 993ms (attempt 1, 0s elapsed)",
         f"{ts(0)} [DEBUG] SSETransport: Connected"]
used = f"{ts(8)} [DEBUG] SSETransport: Event seq=4 event_id=e1 event_type=client_event payload_type=user"
check("heartbeat and reconnect lines alone are unknown, never 0.0d", logfile("a", noise) is None)
d = logfile("b", [used] + noise)
check("last phone message 8 days ago reads about 8 days, reconnects since or not", d is not None and 7.9 < d < 8.1, f"got {d}")
d = logfile("c", [used, f"{ts(1)} [DEBUG] SSETransport: Event seq=9 event_id=e2 event_type=client_event payload_type=keep_alive"] + noise)
check("a keep_alive event is not use", d is not None and d > 7.9, f"got {d}")

print()
print(f"{len(fails)} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
