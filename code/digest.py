"""Weekly: one email confirming everything ran and everything passed.

This exists so a quiet week is provably quiet. Without it, "no alerts" means
either "healthy" or "the alerting is dead", and those look identical. That
ambiguity is what let the Claude login expire unnoticed for a day.

Deliberately does NOT use job.check() to mirror other jobs' problems. Those
jobs already mailed when they found something; re-checking here would send a
second alert for one problem. The digest reports, it does not judge.

    ssh vault-host '~/automation/bin/run-job digest --dry-run'
"""

import json
from datetime import datetime, timezone

from runner import config, notify
from runner.job import Job, log

# A state file older than this means the job stopped running entirely. Longer
# than the slowest schedule (weekly) plus slack.
STALE_AFTER_DAYS = 8
# Cap per job, so one chatty job cannot crowd out the rest of the digest.
MAX_RECORDS_SHOWN = 12


def main() -> int:
    with Job("digest", description="Weekly all-green") as job:

        job.check("state directory readable", ok=config.STATE_DIR.is_dir(),
                  detail=f"{config.STATE_DIR} is missing - no job has ever run")

        states = _load_states()
        job.record("jobs_reporting", len(states))

        problems = [s for s in states if s["status"] in ("problem", "error")]
        stale = [s for s in states
                 if s["_scheduled"] and s["_age_days"] > STALE_AFTER_DAYS]
        job.record("jobs_with_problems", len(problems))
        job.record("jobs_stale", len(stale))

        subject, body = _compose(states, problems, stale)

        if job.dry_run:
            log("digest: dry run, would have sent:")
            log(f"  Subject: {subject}")
            log(body)
        else:
            try:
                to = notify.send_mail(subject, body)
                log(f"digest: mailed {to}")
            except notify.MailUnavailable as exc:
                log(f"digest: mail unavailable, {exc}")
            except Exception as exc:
                # The weekly mail failing is itself worth knowing about, and
                # unlike a job's own alert there is no note fallback here.
                # Fail the job so OnFailure= reports it.
                raise RuntimeError(f"weekly digest could not be sent: {exc}") from exc

    return 0


def _load_states() -> list[dict]:
    now = datetime.now(timezone.utc)
    states = []
    for path in sorted(config.STATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            states.append({"job": path.stem, "status": "unreadable",
                           "_age_days": 0.0, "_error": str(exc),
                           "description": path.stem, "records": {},
                           "checks_total": 0, "checks_failed": 0, "failures": [],
                           "_scheduled": False})
            continue
        finished = datetime.fromisoformat(data.get("finished"))
        data["_age_days"] = (now - finished).total_seconds() / 86400
        data["_finished"] = finished
        # A job with no timer is a manual tool (selftest), not something that
        # stopped running. Only scheduled jobs can go stale.
        data["_scheduled"] = _has_timer(data["job"])
        states.append(data)
    return states


def _has_timer(name: str) -> bool:
    out = _run(["systemctl", "is-enabled", f"automation@{name}.timer"])
    return out == "enabled"


def _run(cmd: list[str]) -> str:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception:
        return ""


def _scheduled_tasks() -> list[str]:
    """Every scheduled thing on this box and when it last fired.

    Not the same list as the state files above: claude-auth-watch and the
    fitness cron are ours but predate the runner, so they write no state here.
    Leaving them out would make the digest look complete while silently
    covering only half of what runs.
    """
    lines = []

    # Ours, plus anything else we own. OS housekeeping timers are excluded
    # deliberately - apt, logrotate and sysstat are Ubuntu's problem.
    units = [u for u in _run(["systemctl", "list-timers", "--all", "--no-pager",
                              "--no-legend", "--plain"]).splitlines()]
    ours = []
    for line in units:
        parts = line.split()
        unit = next((p for p in parts if p.endswith(".timer")), None)
        if unit and ("automation@" in unit or "claude-auth-watch" in unit):
            ours.append(unit)

    for unit in sorted(ours):
        last = _stamp(_run(["systemctl", "show", unit, "-p", "LastTriggerUSec", "--value"]))
        nxt = _stamp(_run(["systemctl", "show", unit, "-p",
                           "NextElapseUSecRealtime", "--value"]))
        state = _run(["systemctl", "is-active", unit])
        flag = "" if state == "active" else f"  <-- TIMER {state.upper()}"
        lines.append(f"  {unit:36s} last {last:22s} next {nxt}{flag}")

    # Cron cannot report on itself: there is no equivalent of LastTriggerUSec,
    # and reading the journal needs privileges this job does not have. This is
    # a concrete argument for the pending migration to timers.
    for entry in _run(["crontab", "-l"]).splitlines():
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        name = entry.split()[-1].split("/")[-1] if "/" in entry else entry[:40]
        lines.append(f"  {'cron: ' + name:36s} "
                     f"cron does not report its own runs (migration pending)")

    return lines or ["  (none found)"]


def _stamp(value: str) -> str:
    if not value or value in ("0", "n/a"):
        return "never"
    # systemd prints e.g. "Fri 2026-08-28 10:51:37 UTC"; keep it short.
    parts = value.split()
    return " ".join(parts[1:3]) if len(parts) >= 3 else value


def _compose(states: list[dict], problems: list[dict], stale: list[dict]) -> tuple[str, str]:
    healthy = not problems and not stale
    stamp = datetime.now().astimezone()

    if healthy:
        subject = f"vault-host weekly: all green ({len(states)} jobs)"
        opening = (f"Every scheduled job on vault-host ran and passed.\n\n"
                   f"This email is the point: it means the week was quiet AND the "
                   f"machinery that would have told you otherwise is working.")
    else:
        count = len(problems) + len(stale)
        subject = f"vault-host weekly: {count} job{'s' if count != 1 else ''} need attention"
        opening = "Not everything is healthy. Detail below."

    lines = [opening, "", f"Week ending {stamp.strftime('%A %d %B %Y')}", "", "-" * 60, ""]

    for s in sorted(states, key=lambda x: x["job"]):
        marker = {"ok": "OK  ", "problem": "FAIL", "error": "ERR ",
                  "skipped": "SKIP", "unreadable": "??  "}.get(s["status"], "?   ")
        age = s["_age_days"]
        when = f"{age * 24:.0f}h ago" if age < 1 else f"{age:.1f} days ago"
        stale_flag = ("  <-- STOPPED RUNNING"
                      if s.get("_scheduled") and age > STALE_AFTER_DAYS else "")
        if not s.get("_scheduled"):
            stale_flag = "  (manual, not scheduled)"

        lines.append(f"{marker}  {s.get('description', s['job'])}")
        lines.append(f"      last run {when}, "
                     f"{s['checks_total'] - s['checks_failed']}/{s['checks_total']} checks"
                     f"{stale_flag}")

        for f in s.get("failures", []):
            lines.append(f"      ! {f['name']}: {f['detail']}")

        records = s.get("records") or {}
        if records:
            # Records are already summarised by the runner, but cap the count
            # too so one chatty job cannot crowd out the others.
            for k, v in list(records.items())[:MAX_RECORDS_SHOWN]:
                lines.append(f"        {k.replace('_', ' ')}: {v}")
            if len(records) > MAX_RECORDS_SHOWN:
                lines.append(f"        ... and {len(records) - MAX_RECORDS_SHOWN} more")
        lines.append("")

    lines += ["-" * 60, "", "SCHEDULED TASKS", ""]
    lines += _scheduled_tasks()
    lines += ["", "-" * 60, "",
              "Detail for any job:",
              "    ssh vault-host 'journalctl -u automation@<job> -n 50 --no-pager'",
              "",
              "This digest is sent by ~/automation/jobs/digest.py, weekly. If it stops",
              "arriving, that is itself the signal - and Healthchecks.io should catch it",
              "within the hour."]

    return subject, "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
