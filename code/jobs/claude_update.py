"""Weekly: keep Claude Code on vault-host current, and prove -p still works.

Every AI job calls `claude -p --model opus` through `runner/ask.py`. The alias
resolves to the newest Opus *that the installed version knows*, so an old
install silently runs old models. Auto-update is enabled on the box (`claude
doctor` says so), but it never fires: short `-p` runs exit before the updater
gets a turn, and nobody opens an interactive session here. On 2026-09-30 the box
was found on 2.1.220 from July, where `opus` still meant Opus 5 with 5.5 out.

So this job is the only thing that ever updates the box:

  1. `claude update`
  2. probe: one trivial `-p` turn through `runner.ask.probe`, which is exactly
     the path every AI job uses. It records what `opus` resolved to.
  3. if the probe fails *after an update*, relink `~/.local/bin/claude` to the
     previous version (native installs keep old versions on disk) and report.
     An update must never be what breaks Monday's build brief.

Sunday 03:00 Pacific: a day's margin before `build_brief`, and clear of the
daily AI jobs.

    ssh vault-host '~/automation/bin/run-job claude_update --dry-run'
"""

import os
import subprocess
from pathlib import Path

from runner import config
from runner.ask import DEFAULT_MODEL, AskFailed, probe
from runner.job import IMPORTANT, ROUTINE, YOU, Job, log

COVERS = "claude-code-current"
LINK = config.HOME / ".local" / "bin" / "claude"


def main() -> int:
    with Job("claude_update", description="Claude Code update on vault-host",
             watches=(COVERS,)) as job:

        if not job.check("Claude Code installed", ok=LINK.exists(), tier=IMPORTANT,
                         owner=YOU, covers=COVERS, detail=f"{LINK} is missing"):
            return 0

        before = _target()
        job.record("version before", before.name)

        if job.dry_run:
            log(f"claude_update: would run `claude update` (now {before.name})")
        else:
            r = subprocess.run([str(LINK), "update"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=600)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            log(f"claude_update: {out[-300:]}")
            job.check("claude update ran", ok=r.returncode == 0, tier=IMPORTANT,
                      owner=YOU, covers=COVERS,
                      detail=f"exit {r.returncode}: {out[-300:]}")

        after = _target()
        updated = after != before
        job.record("version after", after.name)
        job.record("updated", updated)

        try:
            resolved = probe(DEFAULT_MODEL)
        except AskFailed as exc:
            if updated and before.exists():
                # Put the old version back before anything else runs on it.
                _relink(before)
                job.check("claude -p works after update", ok=False, tier=IMPORTANT,
                          owner=YOU, covers=COVERS,
                          detail=f"{after.name} failed the headless probe ({exc}); "
                                 f"rolled back to {before.name}. Check the release "
                                 f"before updating again.")
            else:
                job.check("claude -p works", ok=False, tier=IMPORTANT, owner=YOU,
                          covers=COVERS,
                          detail=f"headless probe failed on {after.name}, with no "
                                 f"update to roll back: {exc}. Every AI job is affected.")
            return 0

        job.record(f"`{DEFAULT_MODEL}` resolves to", resolved)
        previous = job.recall().get("resolved")
        job.remember({"resolved": resolved})
        if previous and previous != resolved:
            log(f"claude_update: `{DEFAULT_MODEL}` moved from {previous} to {resolved}")
            job.record("alias moved", f"{previous} -> {resolved}")

        # A tier check, not a version check: the alias should land on an Opus
        # model. Anything else means the alias or the account changed under us.
        job.check(f"`{DEFAULT_MODEL}` resolves to its own tier",
                  ok=DEFAULT_MODEL in resolved,  # "opus" in "claude-opus-5-5"
                  tier=IMPORTANT, owner=YOU, covers=COVERS,
                  detail=f"`{DEFAULT_MODEL}` answered as {resolved}")
        job.check("Claude Code current and working", ok=True, tier=ROUTINE, owner=YOU,
                  covers=COVERS,
                  detail=(f"updated {before.name} -> {after.name}" if updated
                          else f"already current ({after.name})") + f", `{DEFAULT_MODEL}` = {resolved}")
    return 0


def _target() -> Path:
    """The version directory the `claude` symlink points at."""
    return Path(os.path.realpath(LINK))


def _relink(target: Path) -> None:
    tmp = LINK.with_name(".claude.rollback")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, LINK)  # atomic: a job starting mid-swap sees old or new, never neither
    log(f"claude_update: relinked {LINK} -> {target}")


if __name__ == "__main__":
    raise SystemExit(main())
