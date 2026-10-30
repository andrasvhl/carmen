#!/usr/bin/env python3
"""A dry run must leave nothing behind, especially not suppression memory.

    ~/automation/.venv/bin/python -m tests.dry_run_isolation

The one that bit, 2026-09-02: `remember()` wrote `failed_keys` unconditionally
while `_write_state()` and `_ping()` both refused on a dry run. So rehearsing a
job taught it that its failures were already known, and the next *real* run
marked them `ongoing` and never mailed. A fire drill that disarms the alarm.

This matters more than it looks because the state involved is precisely the one
that decides whether a failure reaches the owner at all. A silently downgraded
first alert is indistinguishable from nothing being wrong.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.VAULT = TMP / "vault"                   # NOT the real vault: a real run
config.INBOX = config.VAULT / "Inbox"          # writes an Inbox note, and did
config.INBOX.mkdir(parents=True)
config.STATE_DIR = TMP / "state"
config.LOCK_DIR = config.STATE_DIR / "locks"
config.ENV_FILE = TMP / "no-such-env"          # no mail, no heartbeat
config.STATE_DIR.mkdir(parents=True)
config.LOCK_DIR.mkdir(parents=True)

from runner.job import Job

MEMORY = config.STATE_DIR / "probe.memory.json"
STATE = config.STATE_DIR / "probe.json"

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def run(dry):
    with Job("probe", description="dry-run isolation probe", dry_run=dry) as job:
        job.check("deliberately failing", ok=False, detail="on purpose", key="probe-key")
    return job


job = run(dry=True)
check("a dry run writes no memory file", not MEMORY.exists())
# State is deliberately written on every run, dry included - it is the freshness
# signal the digest and heartbeat read. The distinction is the whole point of
# this test: state is DESCRIPTIVE (did I run), memory is DECISIVE (should the
# next failure alert). A rehearsal may write the first and must not write the
# second.
check("a dry run still writes state, which is by design", STATE.exists())
check("the failing check is not marked ongoing on a first dry run",
      not job.checks[0].ongoing)

job = run(dry=True)
check("a second dry run still writes no memory", not MEMORY.exists())
check("THE ONE THAT BIT: a real failure after two rehearsals is still NEW, so it alerts",
      not run(dry=False).checks[0].ongoing)

check("the real run did write memory", MEMORY.exists())
# Shape changed 2026-09-15: failed_keys went from a list of keys to
# {key: consecutive failures}, so a finding that keeps failing can escalate
# instead of staying quiet forever. The count is the assertion now -- a first
# real failure after two rehearsals must be streak 1, not 3, or a rehearsal
# would push a finding toward escalation without anything actually breaking.
_mem = json.loads(MEMORY.read_text()).get("failed_keys", {}).get("probe-key", {})
check("memory holds the failing key with a streak of 1",
      isinstance(_mem, dict) and _mem.get("n") == 1,
      MEMORY.read_text().strip() if MEMORY.exists() else "no file")
check("a second real failure IS ongoing, so it does not mail twice",
      run(dry=False).checks[0].ongoing)

check("explicit remember() also refuses on a dry run",
      _no_write := (lambda: (MEMORY.unlink(),
                             Job("probe", dry_run=True).remember({"x": 1}),
                             not MEMORY.exists())[-1])())

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
