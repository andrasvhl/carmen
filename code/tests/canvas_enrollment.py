#!/usr/bin/env python3
"""Enrolled / Waitlisted: the signal, the split rule, and moving on a change.

    ~/automation/.venv/bin/python -m tests.canvas_enrollment

A quarter with a waitlisted course splits into `Enrolled/` and `Waitlisted/`;
a change of status moves the course folder with `vt move`, and a note of
the owner's inside it must survive every move. Runs `vt move` for real, against
a scratch vault only.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import copy, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="canvas-enrol-"))
os.environ["VAULT_PATH"] = str(TMP / "vault")   # before runner.config is imported
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import __main__ as M, archive, order
from tools.canvas.layout import DROPPED, ENROLLED, WAITLISTED, course_dir, locate

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

check("scratch vault, not the real one", str(archive.ROOT).startswith(str(TMP)), str(archive.ROOT))

# Real shapes, as Canvas returned them on 2026-09-23.
ENR = {"type": "student", "role": "StudentEnrollment", "role_id": 11,
       "enrollment_state": "active"}
WAIT = {"type": "student", "role": "Waitlisted Student", "role_id": 17,
        "enrollment_state": "active"}

def course(cid, name, code, *enrols):
    return {"id": cid, "name": name, "course_code": code, "enrollments": list(enrols)}

check("StudentEnrollment is enrolled", archive.enrollment_of(course(1, "x", "", ENR)) == "enrolled")
check("Waitlisted Student is waitlisted", archive.enrollment_of(course(1, "x", "", WAIT)) == "waitlisted")
check("a place in one section outranks a waitlist in another",
      archive.enrollment_of(course(1, "x", "", WAIT, ENR)) == "enrolled")
check("no enrolments is unknown, not a guess", archive.enrollment_of(course(1, "x", "")) is None)

A = course(900109, "F26-STRAMGT-394-01 - Studio Practice: Building Small Firms", "", ENR)
B = course(900111, "F26-OB-461-01 - Mediation Lab", "", WAIT)
D = course(900107, "F26-GSBGEN-320-01 - Coastal Ecology", "", WAIT)
S = course(900103, "Sp26-OIT-278-01-02 - Shared Kitchens in the Age of Apps", "", ENR)
P = course(900101, "Program Orientation - Cohort A", "", WAIT)   # Program never splits
listing = [A, B, D, S, P]
fa, fb, fd, fs, fp = (archive.course_folder(c) for c in listing)

homes = archive.homes(listing)
check("a quarter with a waitlisted course splits",
      homes["900109"] == ENROLLED and homes["900111"] == WAITLISTED, str(homes))
check("a quarter with none stays flat", homes["900103"] is None)
check("Program never splits", homes["900101"] is None)

# Today's layout: every course in its quarter, unsplit, with state pointing there.
root, vault = archive.ROOT, archive.VAULT
data = {"courses": {}, "readings": {}}
for c in listing:
    f = archive.course_folder(c)
    d = course_dir(root, f, None)
    (d / "1 - Intro").mkdir(parents=True)
    (d / "1 - Intro" / "1 - Intro.md").write_text("tool note", encoding="utf-8")
    data["courses"][str(c["id"])] = {"folder": f, "items": {
        "page:intro": {"path": str(d / "1 - Intro" / "1 - Intro.md")}}, "files": {}}
b_dir = course_dir(root, fb, None)
(b_dir / "1 - Intro" / "My negotiation notes.md").write_text("mine, irreplaceable", encoding="utf-8")
elsewhere = vault / "Areas" / "MBA" / "Negotiation prep.md"
elsewhere.write_text(f"see [[{b_dir.relative_to(vault)}/1 - Intro/1 - Intro]]", encoding="utf-8")
data["readings"]["k1"] = {"notes": {fb: f"{b_dir.relative_to(vault)}/1 - Intro/Readings/R"}}

# Dry run moves nothing and records nothing.
before = copy.deepcopy(data)
blocked = M._reconcile_homes(data, listing, homes, dry_run=True)
check("dry run: nothing blocked", blocked == {}, str(blocked))
check("dry run: nothing moved", b_dir.is_dir() and not (root / "Fall 2026" / WAITLISTED).exists())
check("dry run: state untouched", data == before)

# The migration.
blocked = M._reconcile_homes(data, listing, homes, dry_run=False)
b_wait = course_dir(root, fb, WAITLISTED)
check("no course blocked", blocked == {}, str(blocked))
check("enrolled course moved to Enrolled/", course_dir(root, fa, ENROLLED).is_dir())
check("waitlisted course moved to Waitlisted/", b_wait.is_dir() and not b_dir.exists())
check("his note moved with it", (b_wait / "1 - Intro" / "My negotiation notes.md")
      .read_text(encoding="utf-8") == "mine, irreplaceable")
check("past quarter untouched", course_dir(root, fs, None).is_dir())
check("Program untouched", course_dir(root, fp, None).is_dir())
check("a link from elsewhere was rewritten", f"Waitlisted/{fb}" in elsewhere.read_text(encoding="utf-8"),
      elsewhere.read_text(encoding="utf-8"))
cb = data["courses"]["900111"]
check("state note path follows", f"/Waitlisted/{fb}/" in cb["items"]["page:intro"]["path"],
      cb["items"]["page:intro"]["path"])
check("reading state follows", "/Waitlisted/" in data["readings"]["k1"]["notes"][fb])
check("status recorded in state", cb["enrollment"] == "waitlisted"
      and data["courses"]["900109"]["enrollment"] == "enrolled")
check("folder name unchanged", cb["folder"] == fb)
check("a second pass is a no-op", M._reconcile_homes(data, listing, homes, dry_run=False) == {}
      and b_wait.is_dir())

# Sidebar: Fall 2026 > Enrolled, Waitlisted; the path alone carries the status.
orders, _ = order.plan_orders(root)
rel = str((root / "Fall 2026").relative_to(vault))
check("sidebar shows Enrolled then Waitlisted", orders.get(rel) == [ENROLLED, WAITLISTED], str(orders.get(rel)))
check("sidebar orders courses inside Waitlisted/",
      orders.get(f"{rel}/{WAITLISTED}") == sorted([fb, fd]), str(orders.get(f"{rel}/{WAITLISTED}")))
check("the course itself still gets its order", str(b_wait.relative_to(vault)) in orders)

# Off the waitlist: Mediation Lab becomes a real place.
B2 = dict(B, enrollments=[ENR])
listing2 = [A, B2, D, S, P]
homes2 = archive.homes(listing2)
M._reconcile_homes(data, listing2, homes2, dry_run=False)
b_enr = course_dir(root, fb, ENROLLED)
check("waitlisted -> enrolled moves the folder", b_enr.is_dir() and not b_wait.exists())
check("his note survived the second move", (b_enr / "1 - Intro" / "My negotiation notes.md")
      .read_text(encoding="utf-8") == "mine, irreplaceable")
check("status change recorded", data["courses"]["900111"]["enrollment"] == "enrolled")
check("link followed again", f"Enrolled/{fb}" in elsewhere.read_text(encoding="utf-8"))

# The last waitlisted course clears: the quarter folds back to flat.
D2 = dict(D, enrollments=[ENR])
listing3 = [A, B2, D2, S, P]
M._reconcile_homes(data, listing3, archive.homes(listing3), dry_run=False)
check("quarter folds back when nothing is waitlisted",
      all(course_dir(root, f, None).is_dir() for f in (fa, fb, fd)))
check("empty status folders removed", not (root / "Fall 2026" / ENROLLED).exists()
      and not (root / "Fall 2026" / WAITLISTED).exists())
check("his note survived the fold", (course_dir(root, fb, None) / "1 - Intro" /
      "My negotiation notes.md").exists())

# Dropped (2026-09-28): gone from Canvas. It moves to Dropped/, nothing is
# deleted, and the quarter splits so the kept classes sit under Enrolled/.
(course_dir(root, fd, None) / "1 - Intro" / "My fiscal notes.md").write_text("mine too", encoding="utf-8")
listing4 = [A, B2, S, P]
gone = archive.dropped(data, listing4)
check("dropped() finds the current-quarter course Canvas stopped listing",
      gone == {"900107": fd}, str(gone))
before = copy.deepcopy(data)
M._reconcile_homes(data, listing4, archive.homes(listing4, gone), dry_run=True)
check("dry run: a drop moves nothing", course_dir(root, fd, None).is_dir() and data == before)
blocked = M._reconcile_homes(data, listing4, archive.homes(listing4, gone), dry_run=False)
d_drop = course_dir(root, fd, DROPPED)
check("dropping: nothing blocked", blocked == {}, str(blocked))
check("a dropped course moves to Dropped/", d_drop.is_dir() and not course_dir(root, fd, None).exists())
check("its tool notes are kept", (d_drop / "1 - Intro" / "1 - Intro.md").read_text(encoding="utf-8") == "tool note")
check("his note in it is kept", (d_drop / "1 - Intro" / "My fiscal notes.md").read_text(encoding="utf-8") == "mine too")
check("kept classes move into Enrolled/", course_dir(root, fa, ENROLLED).is_dir()
      and course_dir(root, fb, ENROLLED).is_dir())
check("state records the drop", data["courses"]["900107"]["enrollment"] == "dropped")
check("state paths follow the drop", f"/{DROPPED}/{fd}/" in data["courses"]["900107"]["items"]["page:intro"]["path"])
check("past quarter still untouched", course_dir(root, fs, None).is_dir())
check("a second pass is a no-op", M._reconcile_homes(data, listing4, archive.homes(listing4, archive.dropped(data, listing4)),
      dry_run=False) == {} and d_drop.is_dir())
orders, _ = order.plan_orders(root)
check("sidebar shows Enrolled then Dropped", orders.get(rel) == [ENROLLED, DROPPED], str(orders.get(rel)))

# An old-quarter course Canvas stops listing is not a drop: left where it is.
listing_old = [A, B2, P]
check("a past-quarter course is never called dropped", "900103" not in archive.dropped(data, listing_old))
M._reconcile_homes(data, listing_old, archive.homes(listing_old, archive.dropped(data, listing_old)), dry_run=False)
check("and is not moved", course_dir(root, fs, None).is_dir())

# Re-enrolled: listed again, it comes back out of Dropped/.
listing_back = [A, B2, D2, S, P]
M._reconcile_homes(data, listing_back, archive.homes(listing_back, archive.dropped(data, listing_back)), dry_run=False)
check("re-enrolling moves it back out of Dropped/", locate(root, fd) == course_dir(root, fd, None)
      and not d_drop.exists(), str(locate(root, fd)))
check("his note came back with it", (course_dir(root, fd, None) / "1 - Intro" / "My fiscal notes.md").exists())
check("status back to enrolled", data["courses"]["900107"]["enrollment"] == "enrolled")

# A clash is refused, not merged.
listing5 = [A, dict(B2, enrollments=[WAIT]), D2, S, P]
clash = course_dir(root, fb, WAITLISTED); clash.mkdir(parents=True)
blocked = M._reconcile_homes(data, listing5, archive.homes(listing5), dry_run=False)
check("both copies existing blocks the course", "900111" in blocked, str(blocked))
check("and moves nothing", course_dir(root, fb, None).is_dir() and locate(root, fb) == course_dir(root, fb, None))

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
