"""Hourly, silent: is Beeper readable, and is every connected account logged in?

Beeper on this box exists so Claude can read the owner's LinkedIn and Messenger chats (the
plan-today pre-run and delta, any session; _system/beeper-bridge.md). It sends him
nothing: he asked for no pushes (2026-10-16, "it for you to be able to read the things,
please dont send anything to me"). This job only checks that the read path still works,
so a dead login becomes an alert-log entry (IMPORTANT: logged, shown in the Monday
brief, never pushed), not a daily plan that silently stops seeing replies.

    ssh vault-host '~/automation/bin/run-job msg_watch --dry-run'
"""

from runner import beeper
from runner.beeper import BeeperFailed, get
from runner.job import Job, IMPORTANT, YOU


def main() -> int:
    with Job("msg_watch", description="Beeper read-path health (no pushes)",
             watches=("beeper-reachable", "beeper-accounts")) as job:
        try:
            accounts = beeper.run("accounts", "list")
        except BeeperFailed as exc:
            job.check("Beeper reachable", ok=False, detail=str(exc), tier=IMPORTANT,
                      owner=YOU, key="beeper-reachable", covers="beeper-reachable")
            return 0
        job.check("Beeper reachable", ok=True, key="beeper-reachable",
                  covers="beeper-reachable")
        rows = accounts if isinstance(accounts, list) else get(accounts, "items", [])
        bad = [f"{get(a, 'network', '?')}: {get(a, 'status', '?')}" for a in rows
               if str(get(a, "status", "")).lower() != "connected"]
        job.check("Beeper accounts connected", ok=bool(rows) and not bad,
                  detail="; ".join(bad) + ". Re-login in Beeper on the Mac" if bad
                  else "no accounts connected",
                  tier=IMPORTANT, owner=YOU, key="beeper-accounts", covers="beeper-accounts")
        job.record("accounts", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
