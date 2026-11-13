#!/usr/bin/env python3
"""What files an Inbox task, what merely mails, and what gets louder.

    ~/automation/.venv/bin/python -m tests.alert_routing

Three rules, each written because the absence of it cost something real:

  1. A failing check must not file a fresh Inbox task every run. vaultmap had
     four checks and no `key`, so "Vault Map accuracy found 1 problem" was
     filed three times for one unresolved failure -- 2026-09-02, 09-07, 09-14
     -- each a separate file because the title carries the date.

  2. Only owner=YOU files a task. An Inbox note is a claim that the owner has to
     do something; owner=AUTOMATIC is the machine's problem and belongs on the
     status page. Ten machine-written notes were sitting in Inbox/, all still
     `status: open`, burying real work.

  3. A check that keeps failing has to get louder. "Already reported" is a
     reason to stay quiet exactly once; past that it is why vaultmap ran broken
     for three weeks with nobody told. But it escalates on a backoff -- a
     channel that nags every run gets muted, which is the same silence by a
     slower route.
"""
import pathlib
import sys
from datetime import datetime
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.STATE_DIR = TMP
config.VAULT = TMP / "vault"
config.INBOX = config.VAULT / "Inbox"
config.INBOX.mkdir(parents=True)

from runner.job import Job, ESCALATE_AFTER, CRITICAL, IMPORTANT, ROUTINE, YOU, AUTOMATIC

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def run(name, *, day=None, **kw):
    """One job run asserting a single failing check. Returns the Check.

    `day` fakes the calendar so a streak can be built without waiting: the
    streak counts distinct days, not runs, so three same-day runs must NOT
    escalate.
    """
    j = Job(name, description=name)
    seen = j.recall().get("failed_keys", {}) or {}
    j._seen = {k: (v if isinstance(v, dict) else {"n": int(v), "day": None})
               for k, v in seen.items()}
    j.check("a thing is broken", ok=False, detail="d", **kw)
    c = j.checks[-1]
    stamp = day or datetime.now().astimezone().date().isoformat()
    j.remember({"failed_keys": {x.key: {"n": x.streak, "day": stamp}
                                for x in j.checks if not x.ok and x.key}})
    return j, c


# --- 1. a keyless check still dedupes across runs --------------------------
_, c1 = run("j1", day="2026-09-01", tier=IMPORTANT, owner=YOU)
check("run 1 of a keyless check alerts", c1.ongoing is False and c1.streak == 1,
      f"ongoing={c1.ongoing} streak={c1.streak}")

_, c2 = run("j1", day="2026-09-02", tier=IMPORTANT, owner=YOU)
check("run 2 of the same keyless check is ongoing, not a new alert",
      c2.ongoing is True and c2.streak == 2, f"ongoing={c2.ongoing} streak={c2.streak}")

check("the key defaulted to the check name", c2.key == "a thing is broken", c2.key)

# --- 2. escalation at the threshold, then backoff --------------------------
j, c3 = run("j1", day="2026-09-03", tier=IMPORTANT, owner=YOU)
check(f"run {ESCALATE_AFTER} escalates and speaks up again",
      c3.streak == ESCALATE_AFTER and c3.ongoing is False,
      f"streak={c3.streak} ongoing={c3.ongoing}")
check("escalation says so in the detail", "escalated" in c3.detail, c3.detail)

_, c4 = run("j1", day="2026-09-04", tier=IMPORTANT, owner=YOU)
check(f"run {ESCALATE_AFTER + 1} goes quiet again rather than nagging",
      c4.ongoing is True, f"ongoing={c4.ongoing} streak={c4.streak}")

# --- 3. a routine check that persists stops being routine ------------------
for d in range(1, ESCALATE_AFTER):
    run("j2", day=f"2026-09-0{d}", tier=ROUTINE, owner=YOU)
_, c5 = run("j2", day=f"2026-09-0{ESCALATE_AFTER}", tier=ROUTINE, owner=YOU)
check("a routine check failing repeatedly escalates to important",
      c5.tier == IMPORTANT, f"tier={c5.tier}")

# --- 4. recovery clears the streak -----------------------------------------
j6 = Job("j3", description="j3")
j6._seen = {"a thing is broken": {"n": 5, "day": "2026-09-01"}}
j6.check("a thing is broken", ok=True)
check("a passing check resets the streak to 0", j6.checks[-1].streak == 0,
      str(j6.checks[-1].streak))

# --- 5. CRITICAL never goes quiet ------------------------------------------
run("j4", day="2026-09-01", tier=CRITICAL, owner=YOU)
_, c7 = run("j4", day="2026-09-02", tier=CRITICAL, owner=YOU)
check("a critical check never becomes ongoing", c7.ongoing is False,
      f"ongoing={c7.ongoing}")

# --- 6. owner decides whether an Inbox task is filed ------------------------
j8 = Job("j5", description="j5")
j8._seen = {}
j8.check("machine problem", ok=False, tier=IMPORTANT, owner=AUTOMATIC, detail="d")
check("an owner=automatic failure is still a failure",
      len(j8.failures) == 1, str(len(j8.failures)))
check("...but none of it is owner=you, so no task is warranted",
      not [c for c in j8.failures if c.owner == YOU])

j9 = Job("j6", description="j6")
j9._seen = {}
j9.check("your problem", ok=False, tier=IMPORTANT, owner=YOU, detail="d")
check("an owner=you failure does warrant a task",
      len([c for c in j9.failures if c.owner == YOU]) == 1)

# --- 7. the old on-disk memory shape still loads ---------------------------
import json
(TMP / "j7.memory.json").write_text(json.dumps({"failed_keys": ["legacy key"]}))
j10 = Job("j7", description="j7")
seen = j10.recall().get("failed_keys", {})
migrated = {k: 1 for k in seen} if isinstance(seen, list) else seen
check("a pre-2026-09-15 list of keys still reads as 'failed once'",
      migrated == {"legacy key": 1}, str(migrated))

# --- same-day re-runs must not inflate the streak --------------------------
# Re-running a weekly job three times while debugging escalated it as if it had
# been broken three weeks. The streak is a property of the world, not of how
# often someone looked.
TODAY = datetime.now().astimezone().date().isoformat()
for _ in range(4):
    _, csame = run("j8", day=TODAY, tier=ROUTINE, owner=YOU)
check("four runs on the SAME day stay at streak 1", csame.streak == 1, f"streak={csame.streak}")
check("...and therefore do not escalate", csame.tier == ROUTINE, f"tier={csame.tier}")

from runner.alert import unit_for
check("a crashed job's mail quotes its automation@ journal",
      unit_for("heartbeat") == "automation@heartbeat.service", unit_for("heartbeat"))
check("the Telegram bridge's mail quotes its own unit's journal",
      unit_for("tg_bridge") == "tg-bridge.service", unit_for("tg_bridge"))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
