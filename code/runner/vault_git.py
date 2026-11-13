"""Git snapshotting for the vault repo on vault-host.

Every job commits its own pending and its own resulting changes (see
runner.job.Job's pre/post-run hooks), so a job's writes land in a commit
naming that job rather than disappearing into an anonymous periodic
snapshot. `jobs.snapshot` covers everything else on a 15-minute timer - Sync
writes from the phone, a chat/Cowork/mobile write through the MCP connector,
anything with no job behind it at all.
"""

import fcntl
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

GIT_LOCK = config.STATE_DIR / "git.lock"
LOCK_TIMEOUT = 30.0


class GitSnapshotError(Exception):
    """A git operation itself failed. Never raised for "nothing to commit"."""


@contextmanager
def _git_lock():
    """Serialise commits. Two jobs finishing at once must queue, not race
    `git add`/`git commit` against each other and corrupt the index."""
    config.ensure_dirs()
    handle = GIT_LOCK.open("w")
    deadline = time.monotonic() + LOCK_TIMEOUT
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise GitSnapshotError(
                        f"could not get the git lock within {LOCK_TIMEOUT:.0f}s - "
                        "another commit is in progress")
                time.sleep(0.5)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def commit_if_dirty(message: str) -> str | None:
    """Stage everything in the vault repo and commit if it's dirty.

    Returns the new commit's short hash, or None if there was nothing to
    commit. Raises GitSnapshotError if git itself failed - callers decide
    whether that's fatal for them.
    """
    repo = config.VAULT
    if not (repo / ".git").is_dir():
        return None
    with _git_lock():
        return _commit(repo, message)


def _commit(repo: Path, message: str) -> str | None:
    def run(*args, timeout):
        try:
            result = subprocess.run(["git", "-C", str(repo), *args],
                                    capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitSnapshotError(f"git {args[0]} timed out: {exc}") from exc
        if result.returncode != 0:
            raise GitSnapshotError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result

    status = run("status", "--porcelain", timeout=60)
    if not status.stdout.strip():
        return None
    run("add", "-A", timeout=180)
    run("commit", "-q", "-m", message, timeout=60)
    rev = run("rev-parse", "--short", "HEAD", timeout=10)
    return rev.stdout.strip()
