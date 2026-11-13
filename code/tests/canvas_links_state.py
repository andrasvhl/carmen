#!/usr/bin/env python3
"""A links run must not erase what adopt recorded about a hand-saved reading.

    ~/automation/.venv/bin/python -m tests.canvas_links_state

run() used to rebuild each reading's state entry from four named fields, which
dropped `manual_file` and `assigned` on every links pass. Found 2026-09-23 when
the first `vt canvas refresh` re-scanned files adopt had already filed.

Runs against its own HOME and vault: run() loads and saves the state file.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import json, os, pathlib, sys, tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="canvas-links-state-"))
os.environ["HOME"] = str(TMP / "home")
os.environ["VAULT_PATH"] = str(TMP / "vault")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import links as L, state as st
from tools.canvas.layout import course_dir

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

def links_run(**kw):
    # run() reports a missing trafilatura as a return value, not an exception.
    # Ignored, it surfaced as five unrelated-looking failures under the system
    # python3 (2026-10-04). Stop and say so instead.
    out = L.run(ROOT, delay=0, **kw)
    if "error" in out:
        sys.exit(f"links run refused: {out['error']}\n"
                 "Run this test with ~/automation/.venv/bin/python, not python3.")
    return out

check("state is in the scratch HOME", str(st.STATE_FILE).startswith(str(TMP)), str(st.STATE_FILE))

ROOT = pathlib.Path(os.environ["VAULT_PATH"]) / "Areas/MBA/Course Archive"
COURSE = "Mediation Lab - F26-OB-461-01"
base = course_dir(ROOT, COURSE, None)
note_rel = f"{base.relative_to(ROOT.parents[2])}/1 - Intro/Readings/use this link instead"
(base / "1 - Intro" / "Readings").mkdir(parents=True)
(ROOT.parents[2] / (note_rel + ".md")).write_text(
    "---\ncanvas_kind: reading\ncanvas_fetch_status: manual\n---\n\n# use this link instead\n",
    encoding="utf-8")
URL = "https://drive.google.com/file/d/EXAMPLE_DRIVE_FILE_TWO/view?usp=sharing"
k = L.key_of(URL)
manual_file = str(ROOT / "_files" / COURSE / "_manual" / "calloway.pdf")
st.save({"version": 1, "courses": {"1": {"folder": COURSE, "items": {}, "files": {}}},
         "readings": {k: {"url": URL, "title": "use this link instead",
                          "canvas_fetch_status": "manual", "canvas_fetched_at": "2026-09-23T00:00:00Z",
                          "chars": 30, "manual_file": manual_file, "assigned": True,
                          "notes": {COURSE: note_rel}}}})
L._write_cache(k, "The Seven Quiet Pressures That Bend Strategy, by Thea E. Calloway.")

links_run()
r = st.load()["readings"][k]
check("still manual after a links run", r.get("canvas_fetch_status") == "manual", str(r))
check("manual_file survives a links run", r.get("manual_file") == manual_file, str(r))
check("assigned survives a links run", r.get("assigned") is True, str(r))
check("adoption time is not reset to the links run",
      r.get("canvas_fetched_at") == "2026-09-23T00:00:00Z", r.get("canvas_fetched_at"))

# --- a reading lands in the session that links it, in the split layout -----
# The session and the "Linked from" line were read from parts[0]/parts[1] of the
# source path, which stopped matching once courses moved under
# <Quarter>/<Enrolled|Waitlisted>/ (2026-09-23): readings fell back to _Unfiled
# with no "Linked from" line.
C2 = "Mediation Lab Two - F26-OB-343-01"
page = ROOT / "Fall 2026" / "Enrolled" / C2 / "3 - Anchoring" / "3 - Anchoring.md"
page.parent.mkdir(parents=True)
page.write_text("Read [the anchoring case](https://hbsp.harvard.edu/tu/abc12345) first.\n",
                encoding="utf-8")
d = st.load(); d["courses"]["2"] = {"folder": C2, "items": {}, "files": {}}; st.save(d)
links_run()
hits = sorted((ROOT / "Fall 2026" / "Enrolled" / C2).rglob("Readings/*.md"))
check("reading note written", len(hits) == 1, str(hits))
if hits:
    check("reading sits in the session that links it", hits[0].parent.parent.name == "3 - Anchoring",
          str(hits[0]))
    body = hits[0].read_text(encoding="utf-8")
    check("Linked from line names the session page",
          "**Linked from:**" in body and "Fall 2026/Enrolled/" + C2 + "/3 - Anchoring/3 - Anchoring|" in body,
          body)
    check("canvas_module is the session", "canvas_module: 3 - Anchoring" in body, body)

# --- a reading known only by an internal link finds its session ------------
# After the first links pass a course note links to the reading NOTE, not the
# URL, so the reading had no visible source and stayed in _Unfiled/ with no
# "Linked from" (28 of them on 2026-09-23). The harvest now follows the
# internal link, and the session page's link follows the note when it moves.
C3 = "Harbor Ops - F26-GSBGEN-510-01"
cbase = ROOT / "Fall 2026" / "Enrolled" / C3
U3 = URL3 = "https://hbsp.harvard.edu/tu/gullwing01"
k3 = L.key_of(URL3)
old_rel = f"Areas/MBA/Course Archive/Fall 2026/Enrolled/{C3}/_Unfiled/Readings/The Gullwing Incident"
(ROOT.parents[2] / (old_rel + ".md")).parent.mkdir(parents=True)
(ROOT.parents[2] / (old_rel + ".md")).write_text(
    "---\ncanvas_kind: reading\ncanvas_fetch_status: login-required\n---\n\n# The Gullwing Incident\n",
    encoding="utf-8")
sess = cbase / "3 - Monday" / "3 - Monday.md"
sess.parent.mkdir(parents=True)
sess.write_text(f"Read [[{old_rel}|The Gullwing Incident]] before class.\n", encoding="utf-8")
d = st.load()
d["courses"]["3"] = {"folder": C3, "items": {}, "files": {}}
d["readings"][k3] = {"url": URL3, "title": "The Gullwing Incident", "canvas_fetch_status": "login-required",
                     "notes": {C3: old_rel}}
st.save(d)
links_run(prune=True)
new_note = cbase / "3 - Monday" / "Readings" / "The Gullwing Incident.md"
check("reading moved into the session that links it", new_note.exists(),
      str(sorted(p.relative_to(cbase) for p in cbase.rglob("*.md"))))
check("the old _Unfiled copy was pruned", not (ROOT.parents[2] / (old_rel + ".md")).exists())
page = sess.read_text(encoding="utf-8")
check("the session page link follows the note",
      f"[[Areas/MBA/Course Archive/Fall 2026/Enrolled/{C3}/3 - Monday/Readings/The Gullwing Incident|The Gullwing Incident]]" in page,
      page)
if new_note.exists():
    check("Linked from names the session page", "**Linked from:**" in new_note.read_text(encoding="utf-8"))
check("state records the new note path",
      st.load()["readings"][k3]["notes"][C3].endswith("/3 - Monday/Readings/The Gullwing Incident"))

# --- never link to a reading note that is not on disk ----------------------
dd = {"readings": {L.key_of("https://example.com/gone"): {
    "url": "https://example.com/gone", "notes": {C3: f"{old_rel}-deleted"}}}}
check("no internal link to a missing note", L.internal_target(dd, "https://example.com/gone", C3) is None)
md = L.internalise("See [gone](https://example.com/gone).", dd, C3)
check("the URL is kept instead", "(https://example.com/gone)" in md, md)

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
