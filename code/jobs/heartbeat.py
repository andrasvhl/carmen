"""Daily: is the machinery that everything else depends on still working?

This is the job that watches the other jobs. It checks state files rather
than output files, because a healthy job in a quiet week looks identical to
a dead one if you judge it by what it produced.

    ssh vault-host '~/automation/bin/run-job heartbeat --dry-run'
"""

import json
import re
import shutil
import smtplib
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runner import config, rc_sessions
from runner.job import Job, log, CRITICAL, IMPORTANT, YOU, AUTOMATIC, AUTOMATABLE

# unit -> (consequence, concern slug in _system/maintenance.md)
SERVICES = {
    "obsidian-sync": ("the vault stops syncing to and from this box", "sync-alive"),
    "obsidian-mcp": ("chat, Cowork and mobile lose their read/write path to the vault",
                     "mcp-alive"),
    "claude-rc": ("Remote Control from the phone stops working", "remote-control-alive"),
    # Both halves of the WhatsApp bridge. Every alert that bridge is meant to send
    # - session death, the ban tripwire - depends on these being up, so without
    # them here the watchdog is the thing nobody watches.
    "waha": ("the WhatsApp session drops and the bridge goes silent", "whatsapp-bridge-daemons"),
    "wa-consumer": ("WhatsApp messages stop reaching the vault, and the bridge's own "
                    "alerts stop firing", "whatsapp-bridge-daemons"),
    "tg-bridge": ("Telegram messages stop reaching the vault and approvals stop arriving",
                  "telegram-bridge-alive"),
    "checkin-app": ("the Check-in app stops loading on the phone and the Mac",
                    "checkin-app-alive"),
}

# Every 2h cron plus slack for a slow API. Tighter than this false-alarms.
FITNESS_MAX_AGE_H = 4
# Daily timer plus a full day of slack, so one skipped run is not an alarm.
WATCHDOG_MAX_AGE_H = 36
MIN_DISK_FREE_PCT = 15
# Capture is the north star's first priority ("capture must be flawless"), so
# this runs daily rather than weekly.
CAPTURE_WINDOW_DAYS = 14
# `claude remote-control` spawns one child per session and never reaps an idle
# one: each holds ~250MB to send a heartbeat every 20s, forever. Six abandoned
# phone sessions held 1.6GB on 2026-09-01 and the box OOM-killed an unrelated
# job. They clear only when the auth token rotates (~monthly) or the service
# restarts, so nothing bounds them in between. Three is roomy for normal use
# and well below where the box is in trouble.
MAX_RC_SESSIONS = 3
# The documented read path (_system/whatsapp-bridge § Reading WhatsApp).
WAHA_URL = "http://127.0.0.1:3000"
WAHA_SESSION = "default"
# 58 chats were active in the week this was written; two silent days across
# every chat means the session stopped receiving, not a quiet weekend.
WHATSAPP_MAX_AGE_H = 48
WHATSAPP_CHECK = f"WhatsApp readable, newest message within {WHATSAPP_MAX_AGE_H}h"
# The Monday brief is due 14:05 PT and takes up to ~45 min; heartbeat runs at
# 07:30 UTC, so in practice the check first sees it Tuesday 00:30 PT.
BRIEF_GRACE_H = 6


def main() -> int:
    with Job("heartbeat", description="vault-host heartbeat") as job:

        for unit, (consequence, concern) in SERVICES.items():
            job.check(f"{unit} running", ok=_is_active(f"{unit}.service"),
                      tier=CRITICAL, owner=AUTOMATIC, covers=concern,
                      detail=f"systemctl reports not active - {consequence}")

        job.check("claude-auth-watch.timer armed",
                  ok=_is_active("claude-auth-watch.timer"),
                  tier=CRITICAL, owner=AUTOMATIC, covers="watchdog-alive",
                  detail="the login-expiry watchdog is not scheduled, so the next "
                         "expiry would go unnoticed")

        drifted = _unit_drift()
        job.check("every running timer is committed to ~/automation/units",
                  ok=not drifted, tier=IMPORTANT, owner=YOU, covers="unit-repo-drift",
                  detail=f"{len(drifted)} unit(s) differ between the box and the repo: "
                         + "; ".join(drifted[:4]))

        orphans = _unreachable_tasks()
        job.record("tasks_unreachable", len(orphans))
        job.check("no open task sits where no view can see it",
                  ok=not orphans, tier=IMPORTANT, owner=YOU, covers="unreachable-tasks",
                  key="unreachable-tasks",
                  detail=f"{len(orphans)} open task(s) live in a folder ToDo.base excludes: "
                         + "; ".join(orphans[:4]))

        _check_freshness(job, "claude-auth-watch",
                         Path.home() / ".claude-auth-watch.state",
                         WATCHDOG_MAX_AGE_H, CRITICAL,
                         "the login watchdog has not run - it may be dead",
                         concern="watchdog-alive")

        _check_freshness(job, "fitness-data-sync",
                         Path.home() / ".fitness-sync" / "state.json",
                         FITNESS_MAX_AGE_H, IMPORTANT,
                         "Strava/WHOOP sync has not run",
                         concern="fitness-sync-freshness")

        _check_fitness_problems(job)
        _check_capture(job)
        _check_claude_login(job)
        _check_strava_rotation(job)
        _check_disk(job)
        _check_rc_sessions(job)
        _check_vault(job)
        _check_mail(job)
        _check_whatsapp_read(job)
        _check_maintenance_brief(job)

        job.record("uptime_days", _uptime_days())
        log(f"heartbeat: {len([c for c in job.checks if c.ok])}/{len(job.checks)} checks passed")

    return 0


# ---- individual checks ----------------------------------------------------

def _check_capture(job: Job) -> None:
    """Do recent captures still carry `priority`?

    The registry said this needed "a test capture", which is why it sat under
    things nothing can check. It does not: you do not have to MAKE a capture,
    you look at whether the real ones carry the field. Check the data, not the
    process - the same principle the rest of this file runs on.

    Covers both paths at once. Captures record no source, so a failure here
    means "something in the capture chain regressed", not which end of it.

    `type: dump` notes are excluded: a dump is raw capture, not a task, and
    correctly carries no priority.
    """
    import datetime
    cutoff = datetime.date.today() - datetime.timedelta(days=CAPTURE_WINDOW_DAYS)
    recent, missing = 0, []
    for f in config.INBOX.glob("*.md"):
        text = f.read_text(errors="ignore")
        m = re.search(r"^captured:\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        if not m or datetime.date.fromisoformat(m.group(1)) < cutoff:
            continue
        if re.search(r"^type:\s*dump\s*$", text, re.M):
            continue
        recent += 1
        if not re.search(r"^priority:", text, re.M):
            missing.append(f.stem)

    job.record("captures_recent", recent)
    job.record("captures_missing_priority", len(missing))
    job.check(f"captures still write priority (last {CAPTURE_WINDOW_DAYS}d)",
              ok=not missing, tier=IMPORTANT, owner=AUTOMATIC,
              covers="mobile-capture-priority", key="capture-priority",
              detail=f"{len(missing)} of {recent} recent captures have no `priority`: "
                     f"{', '.join(missing[:3])}. The capture chain silently stopped "
                     f"writing it once before, found by accident 2026-08-03")


def _check_freshness(job: Job, name: str, path: Path, max_age_h: int,
                     tier: str, why: str, concern: str = "") -> None:
    """Judge a job by its own state file, never by what it produced."""
    if not path.exists():
        job.check(f"{name} has run", ok=False, tier=tier, owner=AUTOMATIC,
                  covers=concern, detail=f"{path} does not exist - {why}")
        return

    age_h = (time.time() - path.stat().st_mtime) / 3600
    job.record(f"{name}_age_hours", round(age_h, 1))
    job.check(f"{name} ran within {max_age_h}h", ok=age_h <= max_age_h,
              tier=tier, owner=AUTOMATIC, covers=concern,
              detail=f"last run {age_h:.1f}h ago - {why}")


def _check_fitness_problems(job: Job) -> None:
    """Freshness says the sync ran; this says it ran *correctly*. sync.py writes
    `problems` into its state.json (WHOOP credential failing, run-type filter
    drifted). Added 2026-09-29 after a month of missed runs that every
    run-level check passed."""
    path = config.HOME / ".fitness-sync" / "state.json"
    if not path.exists():
        return  # _check_freshness already reports this
    problems = json.loads(path.read_text()).get("problems") or []
    job.check("fitness sync reports no problems", ok=not problems,
              tier=IMPORTANT, owner=YOU, covers="fitness-sync-correct",
              detail="; ".join(problems) + " - see _system/fitness-sync-status.md")


def _check_claude_login(job: Job) -> None:
    """Belt and braces. claude-auth-watch owns the 3-day warning; this only
    fires once the credential is already dead, which can only happen if that
    watchdog failed."""
    path = config.HOME / ".claude" / ".credentials.json"
    if not job.check("Claude credentials present", ok=path.exists(),
                     tier=CRITICAL, owner=YOU, covers="claude-login",
                     detail=f"{path} is missing - Remote Control cannot authenticate"):
        return

    oauth = json.loads(path.read_text()).get("claudeAiOauth", {})
    stamp = oauth.get("refreshTokenExpiresAt") or 0
    if not stamp or not oauth.get("refreshToken"):
        job.check("Claude login valid", ok=False, tier=CRITICAL, owner=YOU, covers="claude-login",
                  detail="refresh token is empty or has no expiry - this is exactly "
                         "what an expired-and-blanked credential looks like. "
                         "Run: ssh -t vault-host renew-claude")
        return

    days = (datetime.fromtimestamp(stamp / 1000, timezone.utc)
            - datetime.now(timezone.utc)).total_seconds() / 86400
    job.record("claude_login_days_left", round(days, 1))
    job.check("Claude login not expired", ok=days > 0, tier=CRITICAL, owner=YOU,
              covers="claude-login",
              detail=f"expired {abs(days):.1f} days ago - Remote Control is down and "
                     f"claude-auth-watch did not catch it. "
                     f"Run: ssh -t vault-host renew-claude")


def _check_strava_rotation(job: Job) -> None:
    """Strava rotates its refresh token every call and sync.py must re-persist
    it. A config.json that stops changing means the chain has broken."""
    path = config.HOME / ".fitness-sync" / "config.json"
    if not path.exists():
        job.check("Strava credentials present", ok=False, tier=IMPORTANT,
                  owner=YOU, covers="strava-whoop-tokens", detail=f"{path} is missing")
        return

    age_h = (time.time() - path.stat().st_mtime) / 3600
    job.record("strava_token_age_hours", round(age_h, 1))
    job.check("Strava token still rotating", ok=age_h <= 24,
              tier=IMPORTANT, owner=AUTOMATIC, covers="strava-whoop-tokens",
              detail=f"config.json unchanged for {age_h:.1f}h - the refresh token may "
                     f"have stopped being re-persisted, which breaks auth silently")


def _check_disk(job: Job) -> None:
    usage = shutil.disk_usage("/")
    free_pct = usage.free / usage.total * 100
    job.record("disk_free_pct", round(free_pct, 1))
    job.record("disk_free_gb", round(usage.free / 1e9, 1))
    job.check(f"disk above {MIN_DISK_FREE_PCT}% free", ok=free_pct >= MIN_DISK_FREE_PCT,
              tier=IMPORTANT, owner=AUTOMATABLE, covers="disk-space",
              detail=f"only {free_pct:.1f}% free - journald, the vault and the "
                     f"555MB claude-rc.log all live here")


def _check_vault(job: Job) -> None:
    ok = job.check("vault Inbox reachable", ok=config.INBOX.is_dir(),
                   tier=CRITICAL, owner=AUTOMATIC, covers="vault-reachable",
                   detail=f"{config.INBOX} is not a directory - is obsidian-sync running?")
    if ok:
        job.record("inbox_notes", len(list(config.INBOX.glob("*.md"))))


def _check_whatsapp_read(job: Job) -> None:
    """Can WhatsApp actually be read, right now, by the documented path?

    `waha running` only says the daemon is up. This does the read a session
    does (_system/whatsapp-bridge § Reading WhatsApp) and measures how old
    the newest message is. It exists because on 2026-09-23 a session read a
    stale doc header and told the owner WhatsApp was unreadable while it was
    live: the status page, not prose, is where that answer should come from.
    """
    def fail(detail: str) -> None:
        job.check(WHATSAPP_CHECK, ok=False, tier=IMPORTANT, owner=YOU,
                  covers="whatsapp-session", detail=detail)

    key = _read_env_file(Path.home() / ".waha.env").get("WAHA_API_KEY")
    if not key:
        return fail("~/.waha.env has no WAHA_API_KEY")

    req = urllib.request.Request(
        f"{WAHA_URL}/api/{WAHA_SESSION}/chats/overview?limit=50",
        headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            chats = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        return fail(f"{type(exc).__name__}: {exc} - chats cannot be read")

    if not isinstance(chats, list):
        return fail(f"API answered with {type(chats).__name__}, not a chat list: "
                    f"{str(chats)[:120]}")

    # A missing timestamp is reported as missing, never defaulted to 0: that
    # exact shortcut produced a false finding about unread counts on 2026-09-17.
    stamps = [(c.get("lastMessage") or {}).get("timestamp")
              for c in chats if isinstance(c, dict)]
    stamps = [s for s in stamps if isinstance(s, (int, float)) and s > 0]
    if not stamps:
        return fail(f"API returned {len(chats)} chats but none carried a message timestamp")

    age_h = (time.time() - max(stamps)) / 3600
    job.record("whatsapp_chats_read", len(chats))
    job.record("whatsapp_newest_message_age_hours", round(age_h, 1))
    job.check(WHATSAPP_CHECK, ok=age_h <= WHATSAPP_MAX_AGE_H, tier=IMPORTANT, owner=YOU,
              covers="whatsapp-session",
              detail=f"newest message across {len(chats)} chats is {age_h:.0f}h old - the "
                     f"session may be connected but no longer receiving")


def _read_env_file(path: Path) -> dict:
    """KEY=VALUE lines, comments skipped. Missing file -> empty dict."""
    out = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _check_mail(job: Job) -> None:
    """The alert channel itself. If this fails the Inbox note still lands,
    which is exactly why notes are not optional."""
    env = config.load_env()
    user = env.get("GMAIL_USER")
    password = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        job.check("mail configured", ok=False, tier=CRITICAL, owner=YOU, covers="gmail-app-password",
                  detail=f"{config.ENV_FILE} incomplete")
        return

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=25) as smtp:
            smtp.starttls()
            smtp.login(user, password)
        job.check("mail credentials valid", ok=True, tier=CRITICAL, owner=YOU,
                  covers="gmail-app-password")
    except Exception as exc:
        job.check("mail credentials valid", ok=False, tier=CRITICAL, owner=YOU,
                  covers="gmail-app-password",
                  detail=f"{type(exc).__name__}: {exc} - alerts can only reach you by "
                         f"Inbox note until this is fixed")


def _check_rc_sessions(job: Job) -> None:
    """Remote Control sessions accumulate until something kills them.

    `rc_reap` closes anything idle past REAP_IDLE_DAYS. This warns a day
    ahead, because a reaped session cannot be reopened from the phone - so a
    thread worth keeping is a decision declined once rather than a dead
    session discovered by tapping it.
    """
    live = rc_sessions.sessions()
    total_mb = sum(s["rss_mb"] for s in live)
    job.record("rc_sessions", len(live))
    job.record("rc_sessions_mb", round(total_mb))

    due = [s for s in live if s["idle_days"] is not None
           and s["idle_days"] >= rc_sessions.WARN_IDLE_DAYS]
    job.record("rc_sessions_due_soon", len(due))
    job.check("no Remote Control session closing within 24h", ok=not due,
              tier=IMPORTANT, owner=YOU, covers="rc-session-sprawl",
              detail="rc_reap will close these and they cannot be reopened from the "
                     "phone - use one now to keep it, or let it go (the transcript "
                     "survives in ~/.claude/projects/): "
                     + "; ".join(rc_sessions.describe(s) for s in due))

    job.check(f"Remote Control sessions at most {MAX_RC_SESSIONS}",
              ok=len(live) <= MAX_RC_SESSIONS,
              tier=IMPORTANT, owner=YOU, covers="rc-session-sprawl",
              detail=f"{len(live)} sessions holding {total_mb:.0f}MB; nothing reopens "
                     f"once closed. "
                     + "; ".join(rc_sessions.describe(s) for s in live[:6]))


def _check_maintenance_brief(job: Job) -> None:
    """Did this week's maintenance brief run? CRITICAL, so a dead brief pushes.

    The brief is now where every non-critical finding reaches the owner, so if it
    stops, the whole alert path goes quiet and a quiet week looks healthy. That
    is the "what confirms the thing that does the confirming" lesson from the
    WhatsApp bridge, whose own alerts depended on a daemon nothing watched.

    Judged on the note the brief always writes on a real run (a quiet week and
    a failed model pass included), with the job's own state as a second
    witness. Due Monday 14:05 PT, checked from BRIEF_GRACE_H later. Before its
    first run, the timer's activation time stands in, so installing it on a
    Wednesday does not page about the Monday before.
    """
    from jobs.maintenance_brief import TZ, brief_path
    name = "maintenance brief ran this week"
    now = datetime.now(TZ)
    due = (now - timedelta(days=now.weekday())).replace(hour=14, minute=5, second=0,
                                                        microsecond=0)
    if due > now - timedelta(hours=BRIEF_GRACE_H):
        due -= timedelta(days=7)

    if brief_path(due.date()).exists():
        return job.check(name, ok=True, tier=CRITICAL, owner=AUTOMATIC, covers="maintenance-brief")

    state = {}
    try:
        state = json.loads((config.STATE_DIR / "maintenance_brief.json").read_text())
    except (OSError, ValueError):
        pass
    try:
        finished = datetime.fromisoformat(state.get("finished", ""))
    except (TypeError, ValueError):
        finished = None
    if finished and finished >= due and state.get("status") in ("ok", "problem") \
            and not state.get("dry_run"):
        return job.check(name, ok=True, tier=CRITICAL, owner=AUTOMATIC, covers="maintenance-brief")

    timer = "automation@maintenance_brief.timer"
    # Never run for real: no brief note at all and no state, or only a dry run's
    # state. A dry run while deploying must not cancel the first-run grace, which
    # is exactly what the deploy on 2026-10-03 did.
    never_ran = (not any(brief_path(due.date()).parent.glob("*.md"))
                 and (not state or state.get("dry_run")))
    if never_ran:
        if _systemctl("is-enabled", timer) != "enabled":
            return job.check(name, ok=False, tier=CRITICAL, owner=YOU, covers="maintenance-brief",
                             detail=f"{timer} is not enabled and the brief has never run, so "
                                    f"nothing reports the alert log to you")
        since = _active_since(timer)
        if since and since > due:
            return job.check(name, ok=True, tier=CRITICAL, owner=AUTOMATIC,
                             covers="maintenance-brief")
    job.check(name, ok=False, tier=CRITICAL, owner=AUTOMATIC, covers="maintenance-brief",
              detail=f"no brief for {due.date()} in _system/maintenance/briefs/ and no real "
                     f"run since {due.strftime('%a %H:%M %Z')}"
                     + (f" (last: {state.get('status')}, {state.get('finished', '?')[:16]})"
                        if state else "")
                     + ". Findings are being logged and nobody is reading them. "
                       "ssh vault-host 'journalctl -u automation@maintenance_brief -n 80 --no-pager'")


# ---- helpers --------------------------------------------------------------

def _systemctl(*args) -> str:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                              timeout=25).stdout.strip()
    except Exception:
        return ""


def _active_since(unit: str):
    """When a timer was last activated (enabled or booted), or None."""
    raw = _systemctl("show", unit, "-p", "ActiveEnterTimestamp", "--value", "--timestamp=unix")
    if raw.startswith("@"):
        try:
            return datetime.fromtimestamp(float(raw[1:]), timezone.utc)
        except ValueError:
            return None
    return None


def _is_active(unit: str) -> bool:
    out = subprocess.run(["systemctl", "is-active", unit],
                         capture_output=True, text=True, timeout=30)
    return out.stdout.strip() == "active"


def _uptime_days() -> float:
    with open("/proc/uptime") as fh:
        return round(float(fh.read().split()[0]) / 86400, 1)


def _unit_drift() -> list[str]:
    """Units running on the box that the repo does not have, and vice versa.

    vps-vault-host.md records six scripts that were deployed and running on the
    box but never `git add`ed, and declares the gap fixed on 2026-09-02. It
    recurred immediately: automation@rc_reap.timer was installed in
    /etc/systemd/system and absent from units/ until 2026-09-15, so a rebuild
    from the repo would have come back with the reaper silently missing.

    Reported rather than repaired: which side is right depends on whether a unit
    was retired or never committed, and that is a judgement.
    """
    repo = {p.name for p in (config.HOME / "automation" / "units").glob("automation@*.timer")}
    repo.discard("automation@example.timer")   # the template, never installed
    listing = subprocess.run(
        ["systemctl", "list-unit-files", "--no-pager", "--no-legend", "--plain",
         "automation@*.timer"],
        capture_output=True, text=True, timeout=25).stdout
    live = {line.split()[0] for line in listing.splitlines() if line.split()}
    out = [f"{u} runs but is not in the repo" for u in sorted(live - repo)]
    out += [f"{u} is in the repo but not installed" for u in sorted(repo - live)]
    return out


# ToDo.base filters `type == "action"` and then excludes two folders by name.
# A task note that lands in either is overdue and invisible at the same time,
# which is the worst state a task system has: it under-reports what is open
# while looking healthy.
#
# Seven of them sat in `Archive/<COACHING_PROJECT> (paused)/` -- five flagged urgent,
# most due 2026-08-25 -- and no surface showed them. `obsidian-todo` reads only
# Inbox/, so it could not see them either. Found 2026-09-15 by audit, not by
# any check. Closed as lapsed the same day.
_TASK_BLIND_FOLDERS = ("Archive", "Notion")


def _unreachable_tasks() -> list[str]:
    out = []
    for folder in _TASK_BLIND_FOLDERS:
        base = config.VAULT / folder
        if not base.is_dir():
            continue
        for f in base.rglob("*.md"):
            head = f.read_text(errors="ignore")[:1500]
            if not re.search(r"^type:\s*action\s*$", head, re.M):
                continue
            m = re.search(r"^status:\s*(\S+)\s*$", head, re.M)
            if (m.group(1) if m else "none") not in ("done", "cancelled"):
                out.append(str(f.relative_to(config.VAULT)))
    return sorted(out)


if __name__ == "__main__":
    raise SystemExit(main())
