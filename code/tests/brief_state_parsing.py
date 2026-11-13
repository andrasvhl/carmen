#!/usr/bin/env python3
"""`load_states` must survive a non-report .json sitting in STATE_DIR.

    ~/automation/.venv/bin/python -m tests.brief_state_parsing

Ported 2026-10-03 from the retired `digest` job to the maintenance brief, which
inherited its loader and its purpose. The history below is the digest's.

The digest crashed on every run from 2026-09-07 to 2026-09-15 because
`canvas.json` -- the Canvas archiver's own cache, 2.5 MB, no `finished`, no
`job` -- lives in STATE_DIR beside the real job reports. `fromisoformat(None)`
raises TypeError and the job exits non-zero.

The shape of that failure is what makes it worth a test. The digest is the
weekly rollup: the one job whose purpose is to surface everything that broke.
When it is the thing that breaks, its own outage is the one nothing reports.
Two Inbox tasks were filed about it, one flagged urgent, and it still ran
broken for nine days.

So this asserts the discrimination, not just the absence of a crash: a real
report must still be loaded. A `_load_states` that returns [] for everything
would survive a naive "did it raise?" test while silently reporting a healthy
system with no jobs in it -- the same vacuous-pass shape `tests/wiki_drift.py`
guards against.
"""
import json
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
config.STATE_DIR = TMP

from jobs import maintenance_brief as D

# A job with no timer must not be called "stale"; stub it so the test does not
# depend on systemd being present.
D._has_timer = lambda name: True

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


now = datetime.now(timezone.utc).isoformat()

# 1. a real job report
(TMP / "heartbeat.json").write_text(json.dumps({
    "job": "heartbeat", "finished": now, "status": "ok",
    "description": "vault-host heartbeat", "records": {},
    "checks_total": 3, "checks_failed": 0, "failures": [],
}))
# 2. the archiver cache that caused the outage: no `finished`, no `job`
(TMP / "canvas.json").write_text(json.dumps({"courses": {"900104": {"name": "x"}}}))
# 3. a job's private memory file, already excluded by name
(TMP / "heartbeat.memory.json").write_text(json.dumps({"seen": []}))
# 4. a report with a corrupt timestamp: must be reported, not fatal
(TMP / "profiles.json").write_text(json.dumps({
    "job": "profiles", "finished": "not-a-timestamp", "status": "ok",
    "description": "profile layer", "records": {},
    "checks_total": 1, "checks_failed": 0, "failures": [],
}))
# 5. unparseable JSON: the pre-existing branch, must still work
(TMP / "wiki.json").write_text("{ truncated")

try:
    states = D.load_states()
    crashed = None
except Exception as exc:
    states, crashed = [], f"{type(exc).__name__}: {exc}"

check("load_states does not raise", crashed is None, crashed or "")

by_job = {s.get("job"): s for s in states}

check("the real report is still loaded", "heartbeat" in by_job,
      f"got {sorted(by_job)}")
check("the archiver cache is skipped", "canvas" not in by_job,
      f"got {sorted(by_job)}")
check("the .memory.json file is skipped",
      not any(str(s.get("job", "")).endswith(".memory") for s in states))
check("a corrupt timestamp is reported, not fatal",
      by_job.get("profiles", {}).get("status") == "unreadable",
      f"got {by_job.get('profiles', {}).get('status')!r}")
check("unparseable JSON is still reported",
      by_job.get("wiki", {}).get("status") == "unreadable",
      f"got {by_job.get('wiki', {}).get('status')!r}")
check("a loaded report carries an age", "_age_days" in by_job.get("heartbeat", {}))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
