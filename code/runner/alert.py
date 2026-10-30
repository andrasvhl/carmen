"""OnFailure= target. Records that a job itself broke.

This is deliberately separate from the reporting inside Job. A job that
crashes, hangs, or is OOM-killed never reaches its own error handler, so
systemd has to be the one that notices. Keep this file dependency-light:
it runs precisely when something else did not work.

Routing since the maintenance brief (2026-10-03, _system/maintenance-brief.md):

    first crash          a block in the vault alert log; the Monday brief
                         re-runs the job once and reports it
    second in a row      the same, plus a push: a job that keeps crashing is
                         an unwatched concern, which is what CRITICAL means
    log write fails      mail, as before, so a crash never goes nowhere

"In a row" means no successful run in between, judged from the job's own
state file, so it holds for daily and weekly jobs alike.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone

from . import alert_log, config, notify
from .job import CRITICAL, IMPORTANT, AUTOMATIC, log

CRASHES = "alert-crashes.json"

# Daemons with their own unit point OnFailure= here too, but their journal is
# not automation@<name>.service: reading that would quote an empty log.
DAEMON_UNITS = {"tg_bridge": "tg-bridge.service"}


def unit_for(name: str) -> str:
    return DAEMON_UNITS.get(name, f"automation@{name}.service")
CHECK = "job crashed or could not start"


def recent_output(unit: str, lines: int = 25) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout.strip() or "(no output captured)"
    except Exception as exc:
        return f"(could not read journal: {exc})"


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def consecutive_crashes(name: str, now: datetime) -> int:
    """Count this crash, return how many in a row it makes, and remember it.

    The previous crash only counts if the job has not finished a run since:
    a state file with status ok/problem and `finished` after that crash means
    it recovered in between. A crash inside Job writes status=error, which
    never counts as recovery.
    """
    config.ensure_dirs()
    path = config.STATE_DIR / CRASHES
    crashes = _read_json(path)
    prev = crashes.get(name) or {}
    count = 1
    if prev.get("last"):
        try:
            last = datetime.fromisoformat(prev["last"])
        except ValueError:
            last = None
        state = _read_json(config.STATE_DIR / f"{name}.json")
        recovered = False
        if last and state.get("status") in ("ok", "problem") and state.get("finished"):
            try:
                recovered = datetime.fromisoformat(state["finished"]) > last
            except ValueError:
                recovered = False
        if last and not recovered:
            count = int(prev.get("count", 1)) + 1
    crashes[name] = {"last": now.isoformat(), "count": count}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(crashes, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return count


def _last_lines(output: str, n: int = 3) -> str:
    lines = [l.strip() for l in output.splitlines() if l.strip()]
    return " / ".join(lines[-n:])[:400]


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    unit = unit_for(name)
    now = datetime.now(timezone.utc)
    stamp = now.astimezone()
    output = recent_output(unit)

    try:
        count = consecutive_crashes(name, now)
    except Exception as exc:
        # Counting is a nicety; the crash itself must still be recorded.
        log(f"alert: could not count crashes ({exc}), treating as the first")
        count = 1
    again = count >= 2

    body = f"""The scheduled job `{name}` on vault-host failed to run{f' ({count} times in a row)' if again else ''}.

This is different from the job finding a problem in your vault. This means the
job itself broke: it crashed, hung, or could not start. Nothing was checked, so
whatever it normally watches is currently unwatched.

Last output:

{output}

To investigate:

    ssh vault-host 'systemctl status {unit} --no-pager'
    ssh vault-host 'journalctl -u {unit} -n 100 --no-pager'

To run it by hand and watch:

    ssh vault-host '~/automation/bin/run-job {name} --dry-run'
"""

    entry = alert_log.Entry(
        job=name, check=CHECK, key=f"crash|{name}", event=alert_log.CRASHED,
        tier=CRITICAL if again else IMPORTANT, owner=AUTOMATIC,
        detail=(f"{count} crashes in a row. " if again else "")
               + f"{stamp.strftime('%Y-%m-%d %H:%M %Z')}: {_last_lines(output)}",
        time=now)
    logged = False
    try:
        alert_log.append([entry])
        logged = True
        log(f"alert: logged crash of {name} ({count} in a row)")
    except alert_log.AlertLogUnavailable as exc:
        log(f"alert: alert log FAILED ({exc}), falling back to mail")

    subject = f"vault-host: job '{name}' FAILED to run" + (f" ({count}x in a row)" if again else "")
    mailed = False
    try:
        if again:
            to = notify.push_or_mail(
                subject, body,
                push_message=f"`{name}` crashed {count} times in a row, so whatever it "
                             f"watches is unwatched. Details in the alert log.")
            mailed = to != "push"
            log(f"alert: {'pushed' if not mailed else f'mailed {to}, the push failed'} "
                f"about {name}")
        if not logged and not mailed:
            to = notify.send_mail(f"[LOG FAILED] {subject}",
                                  "The vault alert log could not be written, so this mail is "
                                  "the only record of this crash.\n\n" + body)
            log(f"alert: mailed {to} about {name} (log fallback)")
    except notify.MailUnavailable as exc:
        log(f"alert: mail unavailable ({exc})")
    except Exception as exc:
        log(f"alert: delivery FAILED, {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
