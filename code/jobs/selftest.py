"""Proves the runner works, and shows what a job looks like.

This is the whole shape of a job. Everything else - locking, state, mail,
notes, the heartbeat ping, exit codes - is inherited from Job.

    python3 -m jobs.selftest --dry-run
    python3 -m jobs.selftest --fail     # force a failure to test the alert path
"""

import sys

from runner import config
from runner.job import Job, log


def main() -> int:
    with Job("selftest", description="Runner self-test") as job:

        job.check(
            "state directory writable",
            ok=config.STATE_DIR.exists() and _writable(config.STATE_DIR),
            detail=f"{config.STATE_DIR} is missing or not writable",
        )

        job.check(
            "vault Inbox reachable",
            ok=config.INBOX.is_dir(),
            detail=f"{config.INBOX} is not a directory - is obsidian-sync running?",
        )

        env = config.load_env()
        job.check(
            "credentials file present",
            ok=bool(env),
            detail=f"{config.ENV_FILE} missing or empty - mail will be skipped",
        )

        job.check(
            "mail configured",
            ok=bool(env.get("GMAIL_USER") and env.get("GMAIL_APP_PASSWORD")),
            detail="GMAIL_USER or GMAIL_APP_PASSWORD not set",
        )

        job.check(
            "heartbeat configured",
            ok=bool(env.get("HEALTHCHECKS_URL")),
            detail="HEALTHCHECKS_URL not set - no off-box dead man's switch",
        )

        if "--fail" in sys.argv:
            raise RuntimeError("deliberate failure, testing the OnFailure path")

        log(f"selftest: {len(job.checks) - len(job.failures)}/{len(job.checks)} checks passed")

    return 0


def _writable(path) -> bool:
    probe = path / ".write-probe"
    try:
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
