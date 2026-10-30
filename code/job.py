"""The shared runner. Every scheduled job on vault-host is one of these.

A job records checks. The runner handles everything else: locking so runs
cannot overlap, a state file the weekly digest reads, the alert path, the
off-box heartbeat ping, and the exit-code contract systemd keys off.

    from runner.job import Job

    with Job("heartbeat") as job:
        job.check("obsidian-sync running", ok=is_active("obsidian-sync"),
                  detail="systemctl reports inactive")

THE EXIT-CODE CONTRACT, which is the whole design:

    exit 0  - the job ran. It may have found problems; finding problems is
              the job working correctly, and it reports them itself by mail
              and Inbox note.
    exit 1  - the job itself broke. systemd's OnFailure= fires and the alert
              unit mails about the job, not about the vault.

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
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from types import TracebackType

from . import config, notify


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


class AlreadyRunning(Exception):
    pass


class Job:
    def __init__(self, name: str, *, description: str = "", dry_run: bool | None = None):
        self.name = name
        self.description = description or name
        self.checks: list[Check] = []
        self.records: dict[str, object] = {}
        self.started = datetime.now(timezone.utc)
        self._lock_handle = None
        self._skipped = False

        if dry_run is None:
            dry_run = "--dry-run" in sys.argv
        self.dry_run = dry_run

        config.ensure_dirs()
        self.state_path = config.STATE_DIR / f"{name}.json"
        self.lock_path = config.LOCK_DIR / f"{name}.lock"

    # ---- the API a job actually uses -------------------------------------
    #
    # Two concepts, deliberately. A CHECK is an assertion that can fail, and
    # failing sends mail. A RECORD is a measurement that cannot fail; it is
    # written to state and surfaces in the weekly digest.
    #
    # Jobs that only inspect things use checks alone. Jobs that do real work
    # need both: "the Strava call succeeded" is a check, "3 activities
    # fetched" is a record. Without records, a working job has nothing to say
    # for itself beyond a pass count.

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        """Assert something. Returns ok, so it can gate further work."""
        self.checks.append(Check(name=name, ok=bool(ok), detail=detail if not ok else ""))
        return bool(ok)

    def record(self, name: str, value) -> None:
        """Note a measurement or what the job did. Never alerts.

        Records are what the weekly digest reports as "work done", so they are
        summaries, not transcripts. A job that processed 200 notes records the
        number 200, never the 200 names. That is enforced here rather than
        trusted to each job: long collections are collapsed to a count plus a
        couple of examples, and long strings are truncated.
        """
        self.records[name] = _summarise(value)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def healthy(self) -> bool:
        return not self.failures

    # ---- lifecycle -------------------------------------------------------

    def __enter__(self) -> "Job":
        try:
            self._acquire_lock()
        except AlreadyRunning:
            # A previous run is still going. Not a failure: the schedule is
            # simply tighter than the work. Record it and exit quietly.
            self._skipped = True
            self._write_state(status="skipped", note="previous run still in progress")
            log(f"{self.name}: skipped, previous run still holding the lock")
            raise SystemExit(0)

        self._ping("/start")
        log(f"{self.name}: started{' (dry run)' if self.dry_run else ''}")
        return self

    def __exit__(self, exc_type, exc: BaseException | None, tb: TracebackType | None) -> bool:
        if self._skipped:
            return False

        try:
            if exc is not None:
                # The job itself broke. Let it propagate to a non-zero exit so
                # OnFailure= fires; do not try to send mail from in here, since
                # whatever broke may be the thing that would send it.
                self._write_state(status="error", note=f"{type(exc).__name__}: {exc}")
                self._ping("/fail")
                log(f"{self.name}: ERROR {type(exc).__name__}: {exc}")
                log(traceback.format_exc())
                return False

            status = "ok" if self.healthy else "problem"
            self._write_state(status=status)

            if self.healthy:
                self._ping("")
                log(f"{self.name}: ok, {len(self.checks)} checks passed")
            else:
                # Problems found. The job succeeded at its purpose, so it still
                # pings healthy and still exits 0 - but it reports what it found.
                self._ping("")
                self._report()
        finally:
            self._release_lock()

        return False

    # ---- reporting -------------------------------------------------------

    def _report(self) -> None:
        failures = self.failures
        log(f"{self.name}: {len(failures)} of {len(self.checks)} checks FAILED")
        for c in failures:
            log(f"  - {c.name}: {c.detail}")

        if self.dry_run:
            log(f"{self.name}: dry run, no mail and no note written")
            return

        stamp = datetime.now().astimezone()
        lines = [f"- **{c.name}** — {c.detail}" if c.detail else f"- **{c.name}**"
                 for c in failures]
        summary = "\n".join(lines)

        title = f"{self.description} found {len(failures)} problem{'s' if len(failures) != 1 else ''} ({stamp.date()})"
        body = (
            f"`{self.name}` ran at {stamp.strftime('%Y-%m-%d %H:%M %Z')} on vault-host and "
            f"{len(failures)} of {len(self.checks)} checks failed.\n\n"
            f"{summary}\n\n"
            f"Check the job's own output for detail:\n\n"
            f"```\nssh vault-host 'journalctl -u automation@{self.name} -n 50 --no-pager'\n```\n\n"
            f"Written by `~/automation/jobs/{self.name}.py`. This note is created fresh each "
            f"time and never edited, so closing it is safe."
        )

        note = notify.write_note(title, body)
        log(f"{self.name}: note {'created' if note else 'already exists for today'}")

        try:
            to = notify.send_mail(
                subject=f"vault-host: {self.description} found {len(failures)} problem"
                        f"{'s' if len(failures) != 1 else ''}",
                body=(
                    f"{len(failures)} of {len(self.checks)} checks failed in `{self.name}` "
                    f"at {stamp.strftime('%Y-%m-%d %H:%M %Z')}.\n\n"
                    + "\n".join(f"  * {c.name}" + (f"\n      {c.detail}" if c.detail else "")
                                for c in failures)
                    + "\n\nA task note is waiting in your Obsidian Inbox.\n\n"
                    f"Detail:\n    ssh vault-host 'journalctl -u automation@{self.name} -n 50 --no-pager'\n"
                ),
            )
            log(f"{self.name}: mailed {to}")
        except notify.MailUnavailable as exc:
            log(f"{self.name}: mail skipped, {exc}")
        except Exception as exc:
            # Mail failing must not fail the job. The note already landed, and
            # a non-zero exit here would blame the job for a network problem.
            log(f"{self.name}: mail FAILED, {type(exc).__name__}: {exc}")

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

        This is the freshness signal the digest and the heartbeat read. It has
        to be the job's own state and not a downstream artifact: fitness sync's
        training-log.md only changes when the owner actually trains, so its mtime
        says nothing about whether the job ran. state.json always moves.
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
            "checks_failed": len(self.failures),
            "failures": [asdict(c) for c in self.failures],
            "records": self.records,
            "note": note,
            "dry_run": self.dry_run,
        }
        # Atomic: write beside the target, then rename. A job killed mid-write
        # must never leave truncated JSON behind, because the digest and the
        # heartbeat both parse this file and would then report a false failure.
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _ping(self, suffix: str) -> None:
        """Off-box dead man's switch. Never allowed to affect the job.

        A per-job URL wins over the shared one, so a weekly job does not keep
        a daily job's check looking alive. Set HEALTHCHECKS_URL_HEARTBEAT to
        give the job named 'heartbeat' its own.
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
    """Keep a record small enough that the weekly digest stays readable.

    The digest is a summary of what happened, not a log. A job that touches
    200 files reports 200, plus a couple of examples so the number means
    something.
    """
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
                        help="run the checks but send no mail and write no note")
    return parser.parse_args()
