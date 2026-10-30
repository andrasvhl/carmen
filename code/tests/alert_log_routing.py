#!/usr/bin/env python3
"""Where a finding goes since the maintenance brief, and what happens when that fails.

    ~/automation/.venv/bin/python -m tests.alert_log_routing

the owner does not read the maintenance mails (2026-10-03). So the runner stopped
mailing and started logging, and every rule below is a way a finding could
otherwise reach nobody:

  1. Routing per tier: CRITICAL pushes and is logged, never mailed when the
     push lands. IMPORTANT and ROUTINE are logged only. Nothing writes an
     Inbox note any more.
  2. The log is append-only and parseable: one block per finding, fields on
     single lines, a header written once, earlier bytes never touched.
  3. If the log cannot be written, the non-critical findings are mailed with
     [LOG FAILED]. A dry run logs nothing.
  4. A crashed job is logged; it pushes only on the second crash in a row, and
     a successful run in between resets the count.
"""
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.VAULT = TMP / "vault"
config.INBOX = config.VAULT / "Inbox"
config.INBOX.mkdir(parents=True)
(config.VAULT / "_system").mkdir()
config.STATE_DIR = TMP / "state"
config.LOCK_DIR = config.STATE_DIR / "locks"
config.ENV_FILE = TMP / "no-such-env"
config.ensure_dirs()

from runner import alert, alert_log, notify
from runner.job import Job, CRITICAL, IMPORTANT, ROUTINE, YOU, AUTOMATIC

fails = []
pushes, mails = [], []
push_ok = True


def fake_push(title, message, **kw):
    if not push_ok:
        raise notify.PushUnavailable("stub refused")
    pushes.append((title, message, kw.get("priority", 0)))


def fake_mail(subject, body):
    mails.append((subject, body))
    return "to@example.com"


notify.send_push = fake_push
notify.send_mail = fake_mail


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def reset():
    pushes.clear()
    mails.clear()


def logged():
    return alert_log.read(datetime.now(timezone.utc) - timedelta(days=40),
                          datetime.now(timezone.utc) + timedelta(seconds=5))


def run(name, checks, *, dry=False):
    with Job(name, description=name, dry_run=dry) as job:
        for c in checks:
            job.check(**c)
    return job


# --- 1. routing per tier ------------------------------------------------------
reset()
run("crit", [dict(name="sync down", ok=False, detail="not active", tier=CRITICAL, owner=AUTOMATIC)])
blocks = [e for e in logged() if e.job == "crit"]
check("CRITICAL pushes", len(pushes) == 1 and pushes[0][2] == 1, str(pushes))
check("CRITICAL does not mail when the push lands", mails == [], str(mails))
check("CRITICAL is logged too, so the log is the whole record",
      len(blocks) == 1 and blocks[0].tier == CRITICAL, str(blocks))

reset()
run("imp", [dict(name="drift found", ok=False, detail="3 units", tier=IMPORTANT, owner=YOU)])
blocks = [e for e in logged() if e.job == "imp"]
check("IMPORTANT does not push", pushes == [], str(pushes))
check("IMPORTANT does not mail", mails == [], str(mails))
check("IMPORTANT is logged once", len(blocks) == 1 and blocks[0].event == "failing", str(blocks))

reset()
run("rout", [dict(name="doc stale", ok=False, detail="old", tier=ROUTINE, owner=YOU)])
blocks = [e for e in logged() if e.job == "rout"]
check("ROUTINE is logged when new", len(blocks) == 1 and blocks[0].tier == ROUTINE, str(blocks))
check("ROUTINE neither pushes nor mails", pushes == [] and mails == [])

reset()
run("imp", [dict(name="drift found", ok=False, detail="3 units", tier=IMPORTANT, owner=YOU)])
blocks = [e for e in logged() if e.job == "imp"]
check("an ongoing IMPORTANT finding is not logged again on the next run",
      len(blocks) == 1, f"{len(blocks)} blocks")

run("imp", [dict(name="drift found", ok=True, tier=IMPORTANT, owner=YOU)])
blocks = [e for e in logged() if e.job == "imp"]
check("a finding that passes again is logged as resolved",
      [e.event for e in blocks] == ["failing", "resolved"], str([e.event for e in blocks]))

check("no Inbox note is written for any tier", list(config.INBOX.glob("*.md")) == [],
      str(list(config.INBOX.glob("*.md"))))

# --- 2. the log's format --------------------------------------------------------
path = alert_log.path_for(datetime.now(timezone.utc))
text = path.read_text()
check("the month file starts with frontmatter, written once",
      text.startswith("---\ntype: reference\ncomponent: maintenance-brief\n---")
      and text.count("component: maintenance-brief") == 1)
check("the file carries no `status:` or `verified:` (frontmatter-properties)",
      "\nstatus:" not in text.split("\n---\n", 1)[0] and "verified:" not in text)
before = path.read_bytes()
e = alert_log.Entry(job="fmt", check="a check", tier=IMPORTANT, owner=YOU,
                    detail="line one\nline two\n- field: fake", key="k1")
alert_log.append([e])
after = path.read_bytes()
check("append never rewrites earlier bytes", after.startswith(before))
block = after[len(before):].decode()
for f in ("time", "job", "check", "key", "tier", "owner", "event", "detail"):
    check(f"block carries `{f}`", f"\n- {f}: " in block, block)
check("a multi-line detail is folded onto one line",
      "- detail: line one / line two / - field: fake" in block, block)
back = [x for x in logged() if x.job == "fmt"]
check("the block parses back with its values intact",
      len(back) == 1 and back[0].key == "k1" and back[0].detail.startswith("line one /"),
      str(back))
check("a folded fake field did not become a real one", back and back[0].tier == IMPORTANT)
check("no em dash in the block", "—" not in block)

# --- 3. the log cannot be written -------------------------------------------
reset()
sysdir = config.VAULT / "_system"
sysdir.rename(config.VAULT / "_system_gone")
run("nolog", [dict(name="imp thing", ok=False, detail="d1", tier=IMPORTANT, owner=YOU),
              dict(name="crit thing", ok=False, detail="d2", tier=CRITICAL, owner=AUTOMATIC)])
check("an unwritable log falls back to one mail", len(mails) == 1, str(mails))
check("...subject says the log failed", mails and "[LOG FAILED]" in mails[0][0], str(mails))
check("...and carries the non-critical finding", mails and "imp thing" in mails[0][1])
check("...but not the critical one, which pushed on its own path",
      mails and "crit thing" not in mails[0][1] and len(pushes) == 1, str(pushes))
check("the vault was not recreated by the log", not sysdir.exists())
(config.VAULT / "_system_gone").rename(sysdir)

reset()
n0 = len(logged())
run("dry", [dict(name="x", ok=False, tier=CRITICAL, owner=YOU)], dry=True)
check("a dry run logs, pushes and mails nothing",
      len(logged()) == n0 and pushes == [] and mails == [])

# --- 4. a crashed job ---------------------------------------------------------
alert.recent_output = lambda unit, lines=25: "line a\nTraceback\nValueError: boom"


def crash(name):
    reset()
    sys.argv = ["alert", name]
    alert.main()
    return [e for e in logged() if e.job == name and e.event == "crashed"]


blocks = crash("flaky")
check("a first crash is logged", len(blocks) == 1 and blocks[0].tier == IMPORTANT, str(blocks))
check("a first crash does not push or mail", pushes == [] and mails == [], f"{pushes} {mails}")
check("the crash block carries the last output lines", blocks and "ValueError: boom" in blocks[0].detail)

blocks = crash("flaky")
check("a second crash in a row pushes", len(pushes) == 1, str(pushes))
check("...is logged as critical", blocks and blocks[-1].tier == CRITICAL, str(blocks))
check("...and does not mail when the push lands", mails == [], str(mails))

# A successful run in between: the job's state says ok, finished after the crash.
(config.STATE_DIR / "flaky.json").write_text(json.dumps({
    "job": "flaky", "status": "ok",
    "finished": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}))
import time
time.sleep(1.2)
crash("flaky")
check("a crash after a successful run counts as a first crash again", pushes == [], str(pushes))

# An in-job crash writes status=error, which is not a recovery.
(config.STATE_DIR / "flaky.json").write_text(json.dumps({
    "job": "flaky", "status": "error",
    "finished": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()}))
time.sleep(1.2)
crash("flaky")
check("a status=error state between crashes still counts as in a row", len(pushes) == 1)

sysdir.rename(config.VAULT / "_system_gone")
crash("nolog_crash")
check("a crash the log cannot take is mailed", len(mails) == 1 and "[LOG FAILED]" in mails[0][0],
      str(mails))
(config.VAULT / "_system_gone").rename(sysdir)

check("a crash writes no Inbox note", list(config.INBOX.glob("*.md")) == [])

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
