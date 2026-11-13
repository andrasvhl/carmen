#!/usr/bin/env python3
"""The routine_push timer fires exactly at SLOTS' times and days (Nov 11, audit A6). Offline.

    python3 -m tests.routine_push_timer

The repo's timer had lost the weekend line (Oct 6 edit from an older base) while the installed
one kept it; a reinstall from the repo would have silently dropped the Sat, Sun 09:00 push. The
timer and SLOTS are two copies by necessity (systemd needs its own file), so this holds them equal.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.tg_bridge._fakes import Checks  # noqa: E402
from jobs import routine_push as RP  # noqa: E402

check = Checks("routine_push timer")
DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def days_of(spec: str) -> frozenset:
    out = set()
    for part in spec.split(","):
        if ".." in part:
            a, b = part.split("..")
            out |= set(range(DAYS[a], DAYS[b] + 1))
        else:
            out.add(DAYS[part])
    return frozenset(out)


timer = (ROOT / "units" / "automation@routine_push.timer").read_text(encoding="utf-8")
fires = set()
for m in re.finditer(r"^OnCalendar=(?:([A-Za-z.,]+)\s+)?\*-\*-\*\s+(\d{2}:\d{2}):00\s+America/Los_Angeles\s*$", timer, re.M):
    fires.add((days_of(m.group(1)) if m.group(1) else frozenset(range(7)), m.group(2)))
want = {(frozenset(days), t) for t, _note, days in RP.SLOTS.values()}
check("every OnCalendar line is a slot, and every slot has its line", fires == want,
      (sorted((sorted(d), t) for d, t in fires), sorted((sorted(d), t) for d, t in want)))
check("the weekend morning is in the timer", (frozenset({5, 6}), "09:00") in fires)
print("\nall passed" if not check.fails else f"\n{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
