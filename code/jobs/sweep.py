"""Weekly: move finished tasks out of the Inbox.

`Inbox/` is three things at once -- the capture queue, the alert channel, and
the archive -- and only the first two were ever designed. Nothing had ever been
swept out, so on 2026-09-15 it held 199 notes of which 81 were `status: done`.
Completed work was 41% of the folder every triage pass had to read past.

## Why moving, not deleting

A done task is the record that something happened, and several carry the only
note of how it went. They go to `Archive/Inbox/<YYYY-MM>/`, grouped by the month
they were completed. `ToDo.base` already excludes `Archive/`, so they leave every
view without leaving the vault, and `git log` in the vault keeps the move.

## Why a grace period

A task marked done today may be re-opened tomorrow, and a note that moved out
from under the owner mid-week is worse than one sitting in the list. Only notes
finished more than GRACE_DAYS ago move, so the sweep never touches anything in
the current working set.

## What it will not touch

Only `type: action` with `status: done` or `cancelled`. Never `open`, never `inbox`, never a
note of any other type -- `learning`, `reference` and `dump` notes live in the
Inbox by design and are not this job's business. A note whose status cannot be
read is left alone and counted, not guessed at.

    ssh vault-host '~/automation/bin/run-job sweep --dry-run'
"""

import datetime
import re
import shutil

from runner import config
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU, AUTOMATIC

# How long a task stays visible after it is finished.
GRACE_DAYS = 30

# Above this many done notes still inside the grace window, something is wrong
# with the sweep rather than with the owner's week: either it stopped running or
# the grace is too long to be doing its job.
BACKLOG_ALERT = 120

ARCHIVE = config.VAULT / "Archive" / "Inbox"


def main() -> int:
    with Job("sweep", description="Inbox sweep") as job:

        if not job.check("Inbox reachable", ok=config.INBOX.is_dir(),
                         tier=IMPORTANT, owner=AUTOMATIC, covers="vault-reachable",
                         detail=f"{config.INBOX} is not a directory"):
            return 0

        cutoff = datetime.date.today() - datetime.timedelta(days=GRACE_DAYS)
        moved, held, unreadable = [], 0, []

        for f in sorted(config.INBOX.glob("*.md")):
            head = f.read_text(errors="ignore")[:2000]
            if not re.search(r"^type:\s*action\s*$", head, re.M):
                continue
            m = re.search(r"^status:\s*(\S+)\s*$", head, re.M)
            if not m:
                unreadable.append(f.name)
                continue
            if m.group(1) not in ("done", "cancelled"):  # closed either way
                continue

            when = _finished_on(f, head)
            if when > cutoff:
                held += 1
                continue

            dest_dir = ARCHIVE / when.strftime("%Y-%m")
            dest = dest_dir / f.name
            if dest.exists():
                # Same title finished twice in one month. Keep both rather than
                # silently overwriting the older record.
                dest = dest_dir / f"{f.stem} ({when.isoformat()}){f.suffix}"
            if not job.dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
            moved.append(f"{f.name} -> {dest.parent.name}/")

        job.record("swept", len(moved))
        job.record("done_within_grace", held)
        job.record("grace_days", GRACE_DAYS)

        job.check("every Inbox note states a status",
                  ok=not unreadable, tier=ROUTINE, owner=YOU, key="sweep-unreadable",
                  detail=f"{len(unreadable)} action note(s) have no readable `status`, so no "
                         f"view can place them and this sweep will not touch them: "
                         + ", ".join(unreadable[:4]))

        job.check(f"finished work is not piling up in the Inbox",
                  ok=held <= BACKLOG_ALERT, tier=ROUTINE, owner=YOU, key="sweep-backlog", covers="inbox-sweep",
                  detail=f"{held} done note(s) are still inside the {GRACE_DAYS}-day grace "
                         f"window, over the {BACKLOG_ALERT} threshold — either the sweep "
                         f"stopped running or the grace is too long to be useful")

        if job.dry_run:
            log(f"sweep: dry run, would move {len(moved)} note(s), holding {held} in grace")
        else:
            log(f"sweep: moved {len(moved)} note(s), holding {held} in grace")
        for line in moved[:12]:
            log(f"  {line}")
        if len(moved) > 12:
            log(f"  ... and {len(moved) - 12} more")

    return 0


def _finished_on(path, head: str) -> datetime.date:
    """When this task was finished, best available.

    `completedDate` is written by TaskNotes and is the real answer. Notes closed
    before that property existed, or closed by hand, fall back to mtime -- which
    is when the file last changed, and for a done task that is when it was
    closed. Both are dates the note can be grouped by; neither is guessed.
    """
    m = re.search(r"^completedDate:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
    if m:
        try:
            return datetime.date.fromisoformat(m.group(1))
        except ValueError:
            pass
    return datetime.date.fromtimestamp(path.stat().st_mtime)


if __name__ == "__main__":
    raise SystemExit(main())
