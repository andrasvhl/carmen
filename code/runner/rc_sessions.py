"""Claude Remote Control sessions: how many, how big, how long idle.

`claude remote-control` spawns one child process per session and never reaps
an idle one. Each holds ~250MB to send a heartbeat every 20 seconds, forever.
Six abandoned phone sessions held 1.6GB on 2026-09-01 - 43% of a 3.7GB box -
and the kernel OOM-killed an unrelated job that needed 400MB.

Only two things have ever cleared them, and neither is idle management: the
auth token expiring (six died together on 2026-08-27 after lifespans of 8 to
28 days) and a `claude-rc` restart. Between credential rotations nothing
bounds them.

Shared by `jobs/heartbeat.py`, which reports, and `jobs/rc_reap.py`, which
acts, so the two can never disagree about what "idle" means.
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

LOG_DIR = Path("/tmp/claude-1000")

# An inbound event from the phone, as CLI 2.1.319 logs it:
#   SSETransport: Event seq=12 event_id=… event_type=… payload_type=user
ACTIVITY = re.compile(r"SSETransport: Event seq=\S+ .*?\bpayload_type=(\S+)")
NOT_USE = {"keep_alive"}

# Sessions idle this long are reaped. Sized against measured use: ~1.5 new
# sessions/day at the observed rate caps this at ~10 live sessions (~2.5GB),
# which fits in RAM plus swap with room to spare. Revisit once the heartbeat
# has reported a real rate over a few weeks.
REAP_IDLE_DAYS = 7.0
# Warned about one day before they go, so a session worth keeping is a
# decision declined once rather than a dead session discovered by tapping it.
WARN_IDLE_DAYS = 6.0


def sessions() -> list[dict]:
    """Every live Remote Control session child, newest activity first."""
    out = []
    for pid in _pids("print --sdk-url"):
        proc = Path("/proc") / pid
        try:
            cmd = (proc / "cmdline").read_bytes().decode().split("\0")
            rss_mb = int((proc / "statm").read_text().split()[1]) * 4 / 1024
        except (OSError, ValueError, IndexError):
            continue
        # `pgrep -f` matches any command line merely CONTAINING the pattern -
        # a shell one-liner inspecting these very processes matched itself
        # during testing. Only a real session binary counts.
        if not cmd or "/claude/versions/" not in cmd[0]:
            continue
        sid = ""
        for i, a in enumerate(cmd):
            if a == "--session-id" and i + 1 < len(cmd):
                sid = cmd[i + 1]
        if not sid:
            continue
        out.append({"pid": pid, "rss_mb": rss_mb, "session_id": sid,
                    "idle_days": idle_days(sid)})
    return sorted(out, key=lambda s: (s["idle_days"] is None, -(s["idle_days"] or 0)))


def idle_days(session_id: str) -> float | None:
    """Days since this session last received anything from the phone.

    Process age and log mtime are both useless: the 20-second heartbeat keeps
    the process and its file looking alive indefinitely. Skipping heartbeat
    lines was not enough either: the CLI also logs SSE reconnects every few
    minutes on every session, used or not, so every session read "idle 0.0d"
    and nothing was ever reaped (7 sessions, 1.8GB on another install, Nov 10).
    The CLI writes hundreds of such housekeeping lines and adds more each
    version, so this counts only what is use: an inbound event, the line the
    CLI logs for every message, interrupt or permission answer from the phone.

    Returns None when it cannot be determined, and every caller treats that as
    "do not touch" - an unknown session is never reaped. That includes a log
    with no inbound event at all, so a CLI that renames the line makes every
    session read "unknown" on the heartbeat rather than look abandoned.
    """
    log = LOG_DIR / f"bridge-session-{session_id}.log"
    if not session_id or not log.exists():
        return None
    last = None
    try:
        with log.open(errors="ignore") as fh:
            for line in fh:
                m = ACTIVITY.search(line)
                if not m or m.group(1) in NOT_USE:
                    continue
                if len(line) > 24 and line[4] == "-" and line[10] == "T":
                    last = line[:19]
    except OSError:
        return None
    if not last:
        return None
    try:
        when = datetime.strptime(last, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def describe(s: dict) -> str:
    idle = "unknown" if s["idle_days"] is None else f"{s['idle_days']:.1f}d"
    return f"{s['session_id'][:12]}… idle {idle}, {s['rss_mb']:.0f}MB"


def _pids(needle: str) -> list[str]:
    # No leading dashes in the pattern: pgrep would read them as its own options.
    out = subprocess.run(["pgrep", "-f", needle], capture_output=True,
                         text=True, timeout=30)
    return [p for p in out.stdout.split() if p.isdigit()]
