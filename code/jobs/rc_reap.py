"""Daily: close Remote Control sessions nobody has touched in a week.

`claude remote-control` spawns one child per session and never reaps an idle
one, so they accumulate at ~250MB each until the auth token rotates (roughly
monthly) or the service restarts. Six held 1.6GB on 2026-09-01 and the kernel
OOM-killed an unrelated job.

Swap raised the ceiling but does not bound it: at the observed ~1.5 new
sessions a day, RAM plus swap fills in about 17 days, which is sooner than the
credential rotation that would have cleared them. Something has to cap it.

    ssh vault-host '~/automation/bin/run-job rc_reap --dry-run'

**A reaped session cannot be reopened from the phone.** The app retries a dead
process forever. The transcript survives in `~/.claude/projects/` and stays
resumable on the box via `claude --resume <uuid>`, so nothing is lost - but
the thread is gone from the phone, which is why `heartbeat` warns a day ahead
and why an idle time it cannot determine is never reaped.
"""

import os
import signal
import time

from runner import rc_sessions
from runner.job import Job, log, IMPORTANT, AUTOMATIC


def main() -> int:
    with Job("rc_reap", description="close idle Remote Control sessions",
             watches=("rc-session-sprawl",)) as job:
        live = rc_sessions.sessions()
        job.record("rc_sessions", len(live))
        job.record("rc_sessions_mb", round(sum(s["rss_mb"] for s in live)))

        # Unknown idle time is never reaped. It means the bridge log is missing
        # or unparseable, and a session that cannot be judged is left alone.
        due = [s for s in live if s["idle_days"] is not None
               and s["idle_days"] >= rc_sessions.REAP_IDLE_DAYS]
        job.record("rc_sessions_due", len(due))

        if job.dry_run:
            for s in due:
                log(f"rc_reap: would close {rc_sessions.describe(s)}")
            job.check("idle sessions closed", ok=True,
                      tier=IMPORTANT, owner=AUTOMATIC, covers="rc-session-sprawl",
                      detail=f"dry run: {len(due)} of {len(live)} session(s) are past "
                             f"{rc_sessions.REAP_IDLE_DAYS:.0f} days idle")
            return 0

        closed, failed = [], []
        for s in due:
            if _terminate(s["pid"]):
                closed.append(s)
            else:
                failed.append(s)

        job.record("rc_sessions_closed", len(closed))
        for s in closed:
            log(f"rc_reap: closed {rc_sessions.describe(s)}")

        job.check("idle sessions closed", ok=not failed,
                  tier=IMPORTANT, owner=AUTOMATIC, covers="rc-session-sprawl",
                  detail=(f"{len(failed)} session(s) would not exit: "
                          + ", ".join(rc_sessions.describe(s) for s in failed))
                         if failed else
                         f"closed {len(closed)} of {len(live)} session(s), "
                         f"freeing {sum(s['rss_mb'] for s in closed):.0f}MB")
        return 0


def _terminate(pid: str, timeout: float = 10.0) -> bool:
    """SIGTERM, then confirm it actually went.

    Deliberately no SIGKILL fallback: the process flushes its transcript on
    shutdown, and that transcript is the only thing that survives a reap. A
    session that ignores SIGTERM is reported rather than forced, so the next
    run tries again instead of losing the history.
    """
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        time.sleep(0.5)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
