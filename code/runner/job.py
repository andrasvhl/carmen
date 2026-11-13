"""The shared runner. Every scheduled job on vault-host is one of these.

A job records checks. The runner handles everything else: locking so runs
cannot overlap, a state file the maintenance brief reads, the alert path, the
off-box heartbeat ping, and the exit-code contract systemd keys off.

    from runner.job import Job

    with Job("heartbeat") as job:
        job.check("obsidian-sync running", ok=is_active("obsidian-sync"),
                  tier=CRITICAL, owner=AUTOMATIC,
                  detail="the vault stops syncing to and from this box")

THE EXIT-CODE CONTRACT, which is the whole design:

    exit 0  - the job ran. It may have found problems; finding problems is
              the job working correctly, and it reports them itself: CRITICAL
              by push, everything else into the vault's alert log.
    exit 1  - the job itself broke. systemd's OnFailure= fires and the alert
              unit logs the crash, pushing when it is the second in a row.

Collapsing those two would make "your sync is down" indistinguishable from
"the thing that checks your sync is down".
"""

import argparse
import fcntl
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from types import TracebackType

from . import alert_log, config, notify, vault_git

# --- Tiers: how loudly a failure is allowed to interrupt --------------------
#
# CRITICAL   the system stops working and you would not find out on your own.
#            Pushes every single run it is failing, never suppressed.
# IMPORTANT  silent degradation with a real cost. Logged when NEW (and when it
#            escalates); while it stays broken the Monday maintenance brief
#            lists it under "Still broken" instead of repeating it.
# ROUTINE    slow rot, low weekly consequence. Logged when new, never pushes.
#
# Nothing mails any more except as a fallback: when a push fails, or when the
# alert log cannot be written (2026-10-03, _system/maintenance-brief.md).
CRITICAL = "critical"
IMPORTANT = "important"
ROUTINE = "routine"

# How many consecutive failing runs before a finding stops being "already
# reported" and gets loud again. Three is one missed weekly cycle plus a
# grace: long enough that a thing you are actively fixing does not nag, short
# enough that nothing repeats vaultmap's three silent weeks.
ESCALATE_AFTER = 3

# --- Owner: who actually resolves it ---------------------------------------
#
# YOU         only the owner can do it (a decision, a payment, an interactive login)
# AUTOMATIC   the machine handles it; he only needs to know it is still true
# AUTOMATABLE a human does it today but it could be handed over once the three
#             gaps in scheduled-execution.md close. This is the standing list
#             of what to automate next.
YOU = "you"
AUTOMATIC = "automatic"
AUTOMATABLE = "automatable"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    tier: str = IMPORTANT
    owner: str = YOU
    key: str = ""
    ongoing: bool = False
    covers: str = ""   # slug of the concern in _system/maintenance.md
    streak: int = 0    # consecutive runs this check has failed; 0 when passing


class AlreadyRunning(Exception):
    pass


class Job:
    def __init__(self, name: str, *, description: str = "", dry_run: bool | None = None,
                 watches: tuple[str, ...] = ()):
        """`watches` names every concern this job is responsible for.

        A job that only creates checks when it FINDS something proves nothing
        on a quiet week: tripwires reported 0/0 checks with nothing expiring,
        so all six concerns it watches read as unwatched. That understates
        coverage exactly when everything is healthy, which is the worst
        direction for a number to be wrong in.

        Any declared concern with no check by the end of the run gets a
        passing one. "Checked, nothing due" is a result.
        """
        self.name = name
        self.description = description or name
        self.watches = tuple(watches)
        self.checks: list[Check] = []
        self.records: dict[str, object] = {}
        self.started = datetime.now(timezone.utc)
        self._lock_handle = None
        self._skipped = False
        self._seen: dict[str, int] = {}

        if dry_run is None:
            dry_run = "--dry-run" in sys.argv
        self.dry_run = dry_run

        config.ensure_dirs()
        self.state_path = config.STATE_DIR / f"{name}.json"
        self.lock_path = config.LOCK_DIR / f"{name}.lock"
        self._memory_path = config.STATE_DIR / f"{name}.memory.json"

    # ---- the API a job actually uses -------------------------------------
    #
    # A CHECK is an assertion that can fail. A RECORD is a measurement that
    # cannot. Jobs that only inspect things use checks alone; jobs that do
    # real work need both.

    def check(self, name: str, ok: bool, detail: str = "", *,
              tier: str = IMPORTANT, owner: str = YOU, key: str = "",
              covers: str = "") -> bool:
        """Assert something. Returns ok, so it can gate further work.

        Pass `key` for a finding that can persist across runs. A non-critical
        check that failed last run too is marked ongoing: it still appears in
        the Monday brief and the status page, but it is not logged again.
        Without that, an unresolved problem is reported every run forever.

        CRITICAL checks ignore `key` entirely. A dead obsidian-sync on day
        three is not less urgent than on day one.

        `covers` names the concern slug this check answers, from the tables in
        _system/maintenance.md. The status job joins the two, so a concern with
        no check shows as unwatched and a check naming no concern shows as
        undeclared. That join is what stops either side rotting quietly.
        """
        ok = bool(ok)
        # A check with no key can never be "ongoing", so it mails and files a
        # fresh Inbox note on every single run. vaultmap had four checks and no
        # keys, which is why "Vault Map accuracy found 1 problem" was filed
        # three times -- 2026-09-02, 09-07, 09-14 -- for one unresolved
        # failure, each note a new file because the title carries the date.
        #
        # Defaulting the key to the check's name makes persistence the default
        # and per-instance keys the deliberate choice, rather than the reverse.
        # Callers that need one key per subject (tripwires: one per file+date)
        # still pass theirs.
        key = key or name
        streak = self._streak(key) if not ok else 0
        ongoing = (not ok and key in self._seen and tier != CRITICAL)

        # --- escalation on repetition -------------------------------------
        # The failure this whole mechanism was missing. vaultmap failed every
        # week from at least 2026-09-02 to 09-14 and never got louder, because
        # "already reported" is a reason to stay quiet exactly once. Past that
        # it becomes a reason to shout: a check that has failed N runs running
        # is not being handled, and silence is what let it run three weeks.
        if not ok and streak >= ESCALATE_AFTER and tier != CRITICAL:
            # Getting worse, so it stops being routine permanently...
            tier = IMPORTANT if tier == ROUTINE else tier
            detail = (f"{detail} [failed {streak} runs running — escalated]"
                      if detail else f"failed {streak} runs running — escalated")
            # ...but it speaks up on a backoff, not on every run. Re-alerting
            # each time would replace three weeks of silence with daily nagging,
            # and a nagging channel gets muted, which is the same outcome by a
            # slower route.
            if streak % ESCALATE_AFTER == 0:
                ongoing = False
        self.checks.append(Check(name=name, ok=ok, detail="" if ok else detail,
                                 tier=tier, owner=owner, key=key, ongoing=ongoing,
                                 covers=covers, streak=streak))
        return ok

    def _streak(self, key: str) -> int:
        """How many distinct days this finding has been failing.

        Days, not runs. Counting runs meant that re-running a weekly job three
        times while debugging escalated it as if it had been broken for three
        weeks -- which happened the day this was written. A finding's age is a
        property of the world, not of how often someone looked at it.

        Same-day re-runs therefore hold the count steady rather than raising it,
        and a job that runs daily and weekly both measure in the same unit.
        """
        prior = self._seen.get(key)
        today = datetime.now().astimezone().date().isoformat()
        if prior is None:
            return 1
        if isinstance(prior, int):                 # pre-2026-09-15 shape
            return prior + 1
        n, day = prior.get("n", 1), prior.get("day")
        return n if day == today else n + 1

    def record(self, name: str, value) -> None:
        """Note a measurement or what the job did. Never alerts.

        Records are what the Monday brief reads as work done, so they are
        summaries, not transcripts. A job that processed 200 notes records
        200, never the 200 names. Enforced here rather than trusted to each
        job.
        """
        self.records[name] = _summarise(value)

    def remember(self, data: dict) -> None:
        """Persist machine state for the next run of this job.

        Separate from record() on purpose: records are summarised for human
        reading, which is exactly wrong for state read back by a program.

        A dry run remembers nothing. It writes no state and sends no mail, so
        writing memory would make a rehearsal indistinguishable from a real run
        to the next real run - which is worse than either, because the state it
        carries is `failed_keys`, the very thing that decides whether a failure
        alerts. The guard lives here rather than at the call site so a future
        caller cannot get it wrong, the same way _write_state and _ping refuse
        for themselves.
        """
        if self.dry_run:
            return
        merged = self.recall()
        merged.update(data)
        self._memory_path.write_text(json.dumps(merged, indent=2, default=str) + "\n",
                                     encoding="utf-8")

    def recall(self) -> dict:
        """What the previous run remembered. Empty dict on a first run."""
        if not self._memory_path.exists():
            return {}
        try:
            return json.loads(self._memory_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    # ---- what alerts, what merely reports ---------------------------------

    @property
    def failures(self) -> list[Check]:
        """Failed, and loud enough to report right now: push if critical, log otherwise."""
        return [c for c in self.checks
                if not c.ok and not c.ongoing and c.tier != ROUTINE]

    @property
    def ongoing(self) -> list[Check]:
        """Still broken, already reported. The Monday brief lists these."""
        return [c for c in self.checks if not c.ok and c.ongoing]

    @property
    def quiet_failures(self) -> list[Check]:
        """ROUTINE failures: real, but never worth an interruption. Logged when new."""
        return [c for c in self.checks if not c.ok and c.tier == ROUTINE]

    @property
    def healthy(self) -> bool:
        return not any(not c.ok for c in self.checks)

    # ---- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Job":
        try:
            self._acquire_lock()
        except AlreadyRunning:
            self._skipped = True
            self._write_state(status="skipped", note="previous run still in progress")
            log(f"{self.name}: skipped, previous run still holding the lock")
            raise SystemExit(0)

        seen = self.recall().get("failed_keys", {})
        # Was a list of keys before 2026-09-15. Read the old shape as "failed
        # once" so the first run after the upgrade does not forget every
        # outstanding finding and re-mail the lot.
        # Three shapes have existed: a list of keys (pre-2026-09-15), {key: count},
        # and {key: {n, day}}. Read them all so an upgrade never forgets an
        # outstanding finding and re-mails the lot.
        if isinstance(seen, list):
            self._seen = {k: {"n": 1, "day": None} for k in seen}
        else:
            self._seen = {k: (v if isinstance(v, dict) else {"n": int(v), "day": None})
                          for k, v in seen.items()}
        self._ping("/start")
        log(f"{self.name}: started{' (dry run)' if self.dry_run else ''}")
        self._snapshot(f"{self.name}: pre-run snapshot")
        return self

    def __exit__(self, exc_type, exc: BaseException | None, tb: TracebackType | None) -> bool:
        if self._skipped:
            return False

        try:
            if exc is not None:
                # The job itself broke. Let it propagate to a non-zero exit so
                # OnFailure= fires; do not try to mail from here, since
                # whatever broke may be the thing that would send it.
                self._write_state(status="error", note=f"{type(exc).__name__}: {exc}")
                self._ping("/fail")
                log(f"{self.name}: ERROR {type(exc).__name__}: {exc}")
                log(traceback.format_exc())
                return False

            self._close_coverage_gaps()
            today = datetime.now().astimezone().date().isoformat()
            self.remember({"failed_keys": {c.key: {"n": c.streak, "day": today}
                                           for c in self.checks if not c.ok and c.key}})

            status = "ok" if self.healthy else "problem"
            self._write_state(status=status)
            self._ping("")

            if self.failures or self._loggable() or self._resolved():
                self._report()
            if not self.failures:
                passed = len([c for c in self.checks if c.ok])
                extra = []
                if self.ongoing:
                    extra.append(f"{len(self.ongoing)} ongoing")
                if self.quiet_failures:
                    extra.append(f"{len(self.quiet_failures)} routine")
                suffix = f" ({', '.join(extra)}, not alerting)" if extra else ""
                log(f"{self.name}: ok, {passed}/{len(self.checks)} checks passed{suffix}")
        finally:
            # Runs even when the job errored above, so a partial write still
            # lands in a commit naming this job rather than an anonymous one
            # from the next `snapshot` tick.
            self._snapshot(f"{self.name}: post-run")
            self._release_lock()

        return False

    def _snapshot(self, message: str) -> None:
        """Commit any dirty vault state under this job's name.

        Every job gets this for free so the git history stays built-in
        rather than something each job has to remember to call. A dry run
        writes nothing and commits nothing, matching remember(). A git
        failure here is logged and swallowed - it must never fail the job
        that happened to trigger it; `jobs.snapshot` is what actually alerts
        on persistent git trouble.
        """
        if self.dry_run:
            return
        try:
            commit = vault_git.commit_if_dirty(message)
        except vault_git.GitSnapshotError as exc:
            log(f"{self.name}: vault snapshot failed, continuing ({exc})")
            return
        if commit:
            log(f"{self.name}: vault snapshot {commit}")

    def _close_coverage_gaps(self) -> None:
        """Emit a pass for every declared concern nothing spoke to this run."""
        spoken_for = {c.covers for c in self.checks if c.covers}
        for slug in self.watches:
            if slug not in spoken_for:
                self.checks.append(Check(
                    name=f"{slug}: checked, nothing to report", ok=True,
                    tier=ROUTINE, owner=YOU, covers=slug))

    # ---- reporting -------------------------------------------------------

    def _loggable(self) -> list[Check]:
        """Failures worth a block in the alert log: every tier, but only when
        new, escalated, or critical. `ongoing` is the same suppression mail
        used to have, so a finding broken for a month is one block plus its
        escalations, not thirty. The brief reads current state for the rest."""
        return [c for c in self.checks if not c.ok and not c.ongoing]

    def _resolved(self) -> list[Check]:
        """Checks that failed last run and pass now. Logged, so the brief can
        say what got fixed this week; a finding that simply disappears (a
        per-date key) is caught by the brief reading current state instead."""
        return [c for c in self.checks if c.ok and c.key and c.key in self._seen]

    def _report(self) -> None:
        """Route by tier (2026-10-03, _system/maintenance-brief.md).

            CRITICAL            push only; mail only if the push fails
            IMPORTANT, ROUTINE  a block in the alert log; no mail
            log write fails     mail, so a finding never goes nowhere

        Every failure is logged, critical included, so the log is the whole
        record. No Inbox task notes any more: an automatic task was a claim
        that the owner had to act, made by a job that could not know. The Monday
        brief now asks him whether to create one.
        """
        failures = self.failures
        loggable = self._loggable()
        resolved = self._resolved()
        crit = [c for c in failures if c.tier == CRITICAL]
        if failures:
            log(f"{self.name}: {len(failures)} check(s) FAILED"
                f"{f', {len(crit)} CRITICAL' if crit else ''}")
        for c in loggable:
            log(f"  - [{c.tier}] {c.name}: {c.detail}")
        for c in resolved:
            log(f"  + resolved: {c.name}")

        if self.dry_run:
            log(f"{self.name}: dry run, nothing pushed, logged or mailed")
            return

        stamp = datetime.now().astimezone()
        entries = [alert_log.Entry(job=self.name, check=c.name, tier=c.tier, owner=c.owner,
                                   detail=c.detail, key=c.key, event=alert_log.FAILING)
                   for c in loggable]
        entries += [alert_log.Entry(job=self.name, check=c.name, tier=c.tier, owner=c.owner,
                                    key=c.key, event=alert_log.RESOLVED)
                    for c in resolved]
        try:
            path = alert_log.append(entries)
            if path:
                log(f"{self.name}: {len(entries)} block(s) logged to "
                    f"{path.relative_to(config.VAULT)}")
        except alert_log.AlertLogUnavailable as exc:
            # The one case where mail is still the channel: a finding must
            # never go nowhere. Same shape as [PUSH FAILED].
            log(f"{self.name}: alert log FAILED ({exc}), falling back to mail")
            self._mail_fallback(loggable, stamp, why=str(exc))

        if not crit:
            return
        subject = (f"vault-host: CRITICAL: {self.description}, {len(crit)} problem"
                   f"{'s' if len(crit) != 1 else ''}")
        body = (f"{len(crit)} critical check(s) failed in `{self.name}` at "
                f"{stamp.strftime('%Y-%m-%d %H:%M %Z')}.\n\n"
                + "\n".join(f"  !! {c.name}  [resolved by: {c.owner}]"
                            + (f"\n        {c.detail}" if c.detail else "") for c in crit)
                + f"\n\nDetail:\n    ssh vault-host 'journalctl -u automation@{self.name} "
                  f"-n 50 --no-pager'\n")
        try:
            to = notify.push_or_mail(
                subject, body, priority=1,
                push_message="\n".join(
                    f"{c.name}" + (f": {c.detail}" if c.detail else "") for c in crit))
            log(f"{self.name}: critical "
                + ("pushed" if to == "push" else f"mailed to {to}, the push failed"))
        except Exception as exc:
            # Delivery failing must not fail the job: a non-zero exit would
            # blame the job for a network problem. The alert becomes an Inbox note (S-4).
            note = notify.push_failed(subject, body, exc)
            log(f"{self.name}: delivery FAILED, {type(exc).__name__}: {exc}; "
                + (f"Inbox note {note.name}" if note else "no Inbox note could be written"))

    def _mail_fallback(self, checks: list[Check], stamp: datetime, *, why: str) -> None:
        """Mail what the log could not take. Critical findings are left out:
        they are pushed (or mailed as [PUSH FAILED]) on their own path."""
        rest = [c for c in checks if c.tier != CRITICAL]
        if not rest:
            return
        subject = (f"vault-host: [LOG FAILED] {self.description}, {len(rest)} finding"
                   f"{'s' if len(rest) != 1 else ''}")
        body = (f"The vault alert log could not be written ({why}), so this mail is the only "
                f"record of these findings from `{self.name}` at "
                f"{stamp.strftime('%Y-%m-%d %H:%M %Z')}.\n\n"
                + "\n".join(f"  {c.name}  [{c.tier}, resolved by: {c.owner}]"
                            + (f"\n        {c.detail}" if c.detail else "") for c in rest)
                + "\n")
        try:
            log(f"{self.name}: mailed {notify.send_mail(subject, body)} (log fallback)")
        except notify.MailUnavailable as exc:
            log(f"{self.name}: log fallback mail unavailable, {exc}")
        except Exception as exc:
            log(f"{self.name}: log fallback mail FAILED, {type(exc).__name__}: {exc}")

    # ---- plumbing --------------------------------------------------------

    def _acquire_lock(self) -> None:
        self._lock_handle = self.lock_path.open("w")
        try:
            fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_handle.close()
            self._lock_handle = None
            raise AlreadyRunning(self.name)
        self._lock_handle.write(f"{os.getpid()}\n")
        self._lock_handle.flush()

    def _release_lock(self) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _write_state(self, *, status: str, note: str = "") -> None:
        """Written on EVERY run, including runs that found nothing to do.

        This is the freshness signal the brief and the heartbeat read. It has
        to be the job's own state, not a downstream artifact: fitness sync's
        training-log.md only changes when the owner actually trains, so its mtime
        says nothing about whether the job ran.
        """
        finished = datetime.now(timezone.utc)
        state = {
            "job": self.name,
            "description": self.description,
            "status": status,
            "started": self.started.isoformat(),
            "finished": finished.isoformat(),
            "duration_s": round((finished - self.started).total_seconds(), 2),
            "checks_total": len(self.checks),
            "checks_failed": len([c for c in self.checks if not c.ok]),
            "checks": [asdict(c) for c in self.checks],
            "failures": [asdict(c) for c in self.failures],
            "records": self.records,
            "note": note,
            "dry_run": self.dry_run,
            # The last run that finished (ok or problem), carried over by an error
            # write. runner/alert.py counts crashes "in a row" against it: the
            # error write of the crash being counted lands before the alert reads
            # this file, so `status`/`finished` alone can never show a recovery
            # (Nov 10: every second crash was pushed as critical, days apart).
            "last_success": (finished.isoformat() if status in ("ok", "problem")
                             else self._previous_success()),
        }
        # Atomic: a job killed mid-write must never leave truncated JSON, since
        # the brief and the status page both parse this.
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _previous_success(self) -> str | None:
        """`last_success` from the state file as it stands, or `finished` of a
        clean run written before the field existed. None if never."""
        try:
            prev = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(prev, dict):
            return None
        if prev.get("last_success"):
            return prev["last_success"]
        return prev.get("finished") if prev.get("status") in ("ok", "problem") else None

    def _ping(self, suffix: str) -> None:
        """Off-box dead man's switch. Never allowed to affect the job.

        A per-job URL wins over the shared one, so a weekly job does not keep
        a daily job's check looking alive.
        """
        env = config.load_env()
        key = f"HEALTHCHECKS_URL_{self.name.upper().replace('-', '_')}"
        base = env.get(key) or env.get("HEALTHCHECKS_URL")
        if not base or self.dry_run:
            return
        try:
            urllib.request.urlopen(f"{base.rstrip('/')}{suffix}", timeout=10).close()
        except (urllib.error.URLError, OSError) as exc:
            log(f"{self.name}: heartbeat ping failed ({exc}), continuing")


def _summarise(value, *, max_items: int = 3, max_chars: int = 160):
    """Keep a record small enough that the state file stays readable."""
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        if len(items) <= max_items:
            return [_summarise(i, max_chars=max_chars) for i in items]
        shown = [_summarise(i, max_chars=max_chars) for i in items[:max_items]]
        return f"{len(items)} items, e.g. {', '.join(str(s) for s in shown)}"
    if isinstance(value, dict):
        if len(value) <= max_items:
            return {k: _summarise(v, max_chars=max_chars) for k, v in value.items()}
        return f"{len(value)} entries"
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars].rstrip() + "..."
    return value


def log(message: str) -> None:
    """Straight to stdout, which systemd routes to journald.

    No job defines its own log file. claude-rc.log reached 555 MB that way.
    """
    print(message, flush=True)


def parse_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--dry-run", action="store_true",
                        help="run the checks but push, log and mail nothing")
    return parser.parse_args()
