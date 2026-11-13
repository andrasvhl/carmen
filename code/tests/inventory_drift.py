#!/usr/bin/env python3
"""inventory's Nov 8 drift checks: docs behind their code, timers behind their docs.

Pure functions only; the job itself reads git and systemctl.

    ~/automation/.venv/bin/python -m tests.inventory_drift
"""
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import jobs.inventory as inv

fails = []


def eq(name, got, want):
    if got != want:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# Paths: backticked, with or without ~/automation/, trailing dots and slashes
# dropped, only existing ones kept, each once.
text = ("Code: `jobs/checkin_app/` and ~/automation/tools/steps.py. Unit "
        "`units/automation@msg_watch.timer`. Run `bin/vt`. Gone: `jobs/old.py`. "
        "Again jobs/checkin_app/. Not ours: `_system/skills/x/jobs/y.py`, ~/carmen/tools/deploy.sh.")
real = {"jobs/checkin_app", "tools/steps.py", "units/automation@msg_watch.timer", "bin/vt"}
eq("paths", inv.doc_code_paths(text, exists=lambda p: p in real),
   ["jobs/checkin_app", "tools/steps.py", "units/automation@msg_watch.timer"])  # bin/vt is a hub

# Behind: strictly after the verified day, newest first, unknown dates ignored.
eq("behind", inv.doc_behind("2026-11-06", {"a": "2026-11-06", "b": "2026-11-08",
                                           "c": "2026-11-07", "d": ""}),
   [("b", "2026-11-08"), ("c", "2026-11-07")])
eq("not behind", inv.doc_behind("2026-11-08", {"a": "2026-11-08"}), [])

# Timers: the three Nov 8 cases.
f = inv.timer_findings(
    active={"automation@heartbeat.timer", "automation@bike_watch.timer", "carmen-deploy.timer"},
    in_repo={"automation@heartbeat.timer", "automation@example.timer", "carmen-kit-sync.timer",
             "carmen-deploy.timer"},
    docs_text="`heartbeat` runs daily; carmen-deploy at 04:30",
    expires={"bike_watch": "2026-10-08", "heartbeat": "2027-01-01"},
    today="2026-11-08")
eq("not installed", f["not_installed"], ["carmen-kit-sync.timer"])
eq("undocumented", f["undocumented"], ["automation@bike_watch.timer"])
eq("expired", f["expired"], ["automation@bike_watch.timer (expired 2026-10-08)"])

# Only our timers count.
eq("own", bool(inv.OWN_TIMER.match("apt-daily.timer")), False)
eq("own2", bool(inv.OWN_TIMER.match("automation@wiki.timer")), True)

if fails:
    print("FAIL\n  " + "\n  ".join(fails))
    raise SystemExit(1)
print("ok - doc paths, behind-code dates, timer drift (not installed, undocumented, expired)")
