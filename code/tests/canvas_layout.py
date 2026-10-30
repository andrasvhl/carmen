#!/usr/bin/env python3
"""Quarter folders: which quarter a course is in, and the order they sort in.

    ~/automation/.venv/bin/python -m tests.canvas_layout
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas.layout import (DROPPED, ENROLLED, PROGRAM, WAITLISTED, course_dir, course_dirs,
                                 course_of, group_key, group_of, is_group, is_status,
                                 locate, quarter_of)

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

cases = {
    "Studio Practice - Building Small Firms - F26-STRAMGT-394-01": "Fall 2026",
    "Pricing Puzzles for Hardware Markets - Sp26-STRAMGT-648-01-02": "Spring 2026",
    "Numbers and Choices - Base (Studio Format) - W26-OIT-108-02-04": "Winter 2026",
    "Finance - F25-FINANCE-742-01-02-03-04": "Fall 2025",
    "Rethinking Libraries in the Age of Sensors - F26-STRAMGT-121-01-EDUC-398-01": "Fall 2026",
    "25F-GSBGEN-745-breakout": PROGRAM,
    "25F-MBA-Program": PROGRAM,
    "Program Orientation - Cohort A": PROGRAM,
    "Robotics Studio Projects - F26-CS-351Z-01": "Fall 2026",
}
for folder, want in cases.items():
    got = quarter_of(folder)
    check(f"quarter of {folder[:45]!r}", got == want, f"got {got!r}")

order = sorted(["Fall 2025", "Program", "Spring 2026", "Winter 2026", "Fall 2026"], key=group_key)
check("quarters sort newest first, Program last",
      order == ["Fall 2026", "Spring 2026", "Winter 2026", "Fall 2025", "Program"], str(order))

check("a quarter is a group", is_group("Fall 2026") and is_group(PROGRAM))
check("a course is not a group", not is_group("Finance - F25-FINANCE-742-01-02-03-04"))
check("course_of reads the grouped layout",
      course_of(("Fall 2026", "Mediation Lab - F26-OB-461-01", "1 - Intro", "x.md")) == "Mediation Lab - F26-OB-461-01")
check("course_of reads the flat layout",
      course_of(("Mediation Lab - F26-OB-461-01", "1 - Intro", "x.md")) == "Mediation Lab - F26-OB-461-01")

root = pathlib.Path(tempfile.mkdtemp())
flat = root / "Mediation Lab - F26-OB-461-01"; flat.mkdir()
(root / "Fall 2025" / "Finance - F25-FINANCE-742-01-02-03-04").mkdir(parents=True)
(root / "_files" / "whatever").mkdir(parents=True)
check("locate finds a course not yet moved", locate(root, flat.name) == flat)
check("course_dir always names the grouped home",
      course_dir(root, flat.name, None) == root / "Fall 2026" / flat.name)
found = sorted(d.name for d in course_dirs(root))
check("course_dirs walks both layouts and skips _files",
      found == ["Finance - F25-FINANCE-742-01-02-03-04", "Mediation Lab - F26-OB-461-01"], str(found))

# --- enrolment split (2026-09-23) -------------------------------------------
N = "Mediation Lab - F26-OB-461-01"
check("status folders are not quarters", not is_group(ENROLLED) and not is_group(WAITLISTED))
check("status folders are recognised", is_status(ENROLLED) and is_status(WAITLISTED) and not is_status(N))
check("Enrolled sorts before Waitlisted",
      sorted([WAITLISTED, ENROLLED], key=group_key) == [ENROLLED, WAITLISTED])
check("Dropped sorts last and is a status folder",
      sorted([DROPPED, WAITLISTED, ENROLLED], key=group_key) == [ENROLLED, WAITLISTED, DROPPED]
      and is_status(DROPPED) and not is_group(DROPPED))
check("course_dir with a group adds the level",
      course_dir(root, N, WAITLISTED) == root / "Fall 2026" / WAITLISTED / N)
check("course_dir without a group stays in the quarter",
      course_dir(root, N, None) == root / "Fall 2026" / N)
try:
    course_dir(root, N, "Maybe"); ok = False
except ValueError:
    ok = True
check("course_dir refuses a made-up group", ok)
try:
    course_dir(root, N); ok = False
except TypeError:
    ok = True
check("course_dir demands the group (no silent default)", ok)
check("course_of reads the split layout",
      course_of(("Fall 2026", WAITLISTED, N, "1 - Intro", "x.md")) == N)
check("course_of on a bare status folder is no course",
      course_of(("Fall 2026", ENROLLED)) is None)

root2 = pathlib.Path(tempfile.mkdtemp())
w = root2 / "Fall 2026" / WAITLISTED / N; w.mkdir(parents=True)
e = root2 / "Fall 2026" / ENROLLED / "Acting with Power - F26-OB-242-01-02"; e.mkdir(parents=True)
old = root2 / "Spring 2026" / "Shared Kitchens in the Age of Apps - Sp26-OIT-278-01-02"; old.mkdir(parents=True)
(root2 / "_files" / N).mkdir(parents=True)
check("locate finds a waitlisted course", locate(root2, N) == w)
check("locate finds an enrolled course", locate(root2, e.name) == e)
check("locate still finds an unsplit quarter", locate(root2, old.name) == old)
check("group_of reads the group back", group_of(w) == WAITLISTED and group_of(old) is None)
found = sorted(d.name for d in course_dirs(root2))
check("course_dirs descends into Enrolled/ and Waitlisted/, never lists them as courses",
      found == sorted([N, e.name, old.name]), str(found))

from tools.canvas.order import _ordered
got = _ordered(["10 - Harvest", "2 - Elm Street", "1 - ProjectKITE", "19 - Wrap-Up"], [])
check("order fallback sorts sessions by number, not as text",
      got == ["1 - ProjectKITE", "2 - Elm Street", "10 - Harvest", "19 - Wrap-Up"], str(got))

from tools.canvas.render import safe_filename
for raw, want in [("[INDIVIDUAL] DoorDash Quiz", "(INDIVIDUAL) DoorDash Quiz"),
                  ("Planning and Review Workbook]]", "Planning and Review Workbook"),
                  ("A | B", "A - B"), ("Plain (ok) title", "Plain (ok) title")]:
    check(f"filename free of link syntax: {raw!r}", safe_filename(raw) == want, safe_filename(raw))

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
