"""Every 15 minutes: commit any vault changes nobody's job caught.

Every job commits its own pending and its own resulting changes (see
runner.job.Job's pre/post-run hooks), so most of what lands here has no job
behind it at all - the owner editing on his phone, a chat/Cowork/mobile write
through the MCP connector, or a plain Sync pull. This job's only purpose is
making sure nothing sits uncommitted for more than 15 minutes.

Recoverability and per-line attribution are the point, not backup - the
vault is already backed up nightly to B2 by restic (see [[backups]]). Git
adds "what changed, when, and which job (if any) wrote it" on top of that.

    ssh vault-host '~/automation/bin/run-job snapshot --dry-run'
"""

from runner import vault_git
from runner.job import Job, log, IMPORTANT, ROUTINE, AUTOMATIC


def main() -> int:
    with Job("snapshot", description="vault git snapshot",
             watches=("vault-git-history",)) as job:
        try:
            commit = vault_git.commit_if_dirty("snapshot: periodic")
        except vault_git.GitSnapshotError as exc:
            job.check("vault changes committed", ok=False,
                      tier=IMPORTANT, owner=AUTOMATIC, key="git-commit-failed",
                      covers="vault-git-history",
                      detail=f"git failed: {exc}. Uncommitted vault changes are "
                             f"accumulating - ssh in and check "
                             f"`git -C ~/vault status`")
            return 0

        job.record("committed_this_run", commit is not None)
        if commit:
            log(f"snapshot: committed {commit}")
        job.check("vault changes committed", ok=True,
                  tier=ROUTINE, owner=AUTOMATIC, covers="vault-git-history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
