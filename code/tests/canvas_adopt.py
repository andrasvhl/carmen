#!/usr/bin/env python3
"""Adopting hand-saved readings: matched by content and URL, never by filename.

Builds a scratch vault with a course, three gated readings and three dropped
files with deliberately useless names.

    ~/automation/.venv/bin/python -m tests.canvas_adopt

The two assertions that matter: a file named `download (3).txt` is matched by
what is inside it, and a file that genuinely could be either of two readings is
refused rather than guessed at.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import json, os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import links as L, archive as A

TMP = pathlib.Path(tempfile.mkdtemp())
VAULT = TMP / "vault"
ROOT = VAULT / "Areas/MBA/Course Archive"
FILES = ROOT / "_files"
COURSE = "Design Lab - W26-GSBGEN-133-01"

L.VAULT = VAULT
L.CACHE_DIR = TMP / "readings"
A.VAULT = VAULT
A.ROOT = ROOT
A.FILES_ROOT = FILES

READINGS = {
    "k_pay":  {"url": "https://hbsp.harvard.edu/tu/3a030325",
               "title": "How to Pay for Urban Transit", "canvas_fetch_status": "login-required",
               "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/3 - Route Economics/Readings/How to Pay for Urban Transit"}},
    "k_kaiser": {"url": "https://hbsp.harvard.edu/tu/9b117742",
               "title": "Northwind Transit Integrated Service", "canvas_fetch_status": "login-required",
               "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/4 - Delivery Models/Readings/Northwind Transit Integrated Service"}},
    "k_done": {"url": "https://example.com/free", "title": "An Article Already Fetched",
               "canvas_fetch_status": "ok",
               "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/1 - Intro/Readings/An Article Already Fetched"}},
}

def fresh_state():
    return {"version": 1,
            "courses": {"551": {"folder": COURSE, "name": "Design Lab", "items": {}, "files": {}}},
            "readings": json.loads(json.dumps(READINGS))}

def setup_tree():
    for k, r in READINGS.items():
        p = VAULT / (r["notes"][COURSE] + ".md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ncanvas_fetch_status: {r['canvas_fetch_status']}\n---\n\n# {r['title']}\n", encoding="utf-8")
    (FILES / COURSE / "_manual").mkdir(parents=True, exist_ok=True)

def drop(name, body):
    p = FILES / COURSE / "_manual" / name
    p.write_text(body, encoding="utf-8")
    return p

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

setup_tree()
# Deliberately useless filenames: this is the whole point.
drop("download (3).txt",
     "How to Pay for Urban Transit\n\nTwo planners compare fare "
     "models and how operators should be paid for riders served rather than miles driven.")
drop("9b117742-PDF-ENG.txt",
     "Some case body text that does not name its own title anywhere at all.")
drop("random-handout.txt",
     "A completely unrelated handout about supply chain logistics and warehousing.")

data = fresh_state()
res = L.adopt(ROOT, data, dry_run=True)
by_file = {m["file"]: m for m in res["matched"]}
un = {u["file"]: u for u in res["unmatched"]}

check("scanned all three drops", res["scanned"] == 3, str(res["scanned"]))
check("badly-named file matched by its CONTENT",
      by_file.get("download (3).txt", {}).get("title") == "How to Pay for Urban Transit",
      str(by_file.get("download (3).txt")))
check("slug-named file matched by its URL slug",
      by_file.get("9b117742-PDF-ENG.txt", {}).get("title") == "Northwind Transit Integrated Service",
      str(by_file.get("9b117742-PDF-ENG.txt")))
check("unrelated file was NOT guessed at", "random-handout.txt" in un, str(un.keys()))
check("the refusal explains itself",
      "no clear match" in un.get("random-handout.txt", {}).get("reason", ""),
      un.get("random-handout.txt", {}).get("reason", ""))
check("an already-fetched reading is not a candidate",
      all(m["title"] != "An Article Already Fetched" for m in res["matched"]))
check("dry run wrote nothing to state",
      data["readings"]["k_pay"]["canvas_fetch_status"] == "login-required")

# --- for real this time ---
data = fresh_state()
res = L.adopt(ROOT, data, dry_run=False)
check("adopted two", len(res["matched"]) == 2, str(len(res["matched"])))
check("status is `manual`, not `ok`",
      data["readings"]["k_pay"]["canvas_fetch_status"] == "manual",
      data["readings"]["k_pay"]["canvas_fetch_status"])
check("provenance recorded", data["readings"]["k_pay"]["manual_file"].endswith("download (3).txt"))

note = VAULT / (data["readings"]["k_pay"]["notes"][COURSE] + ".md")
body = note.read_text(encoding="utf-8")
check("text landed in the reading's OWN note", "bundled" in body, note.name)
check("note sits in the session that assigned it", "3 - Route Economics" in str(note), str(note))
check("note title is the reading's, not the filename",
      "# How to Pay for Urban Transit" in body and "download (3)" not in body)
check("note says it was hand-saved", "saved by hand" in body)
check("no `_manual` junk-drawer note was created",
      not list((ROOT / COURSE).glob("_manual*")), str(list((ROOT/COURSE).glob("*"))))

# --- the old gotcha: renaming an adopted file must not create a second note ---
before = sorted(p.name for p in (VAULT / (READINGS["k_pay"]["notes"][COURSE] + ".md")).parent.iterdir())
os.rename(FILES / COURSE / "_manual" / "download (3).txt",
          FILES / COURSE / "_manual" / "renamed-by-hand.txt")
res2 = L.adopt(ROOT, data, dry_run=False)
after = sorted(p.name for p in (VAULT / (READINGS["k_pay"]["notes"][COURSE] + ".md")).parent.iterdir())
check("renaming an adopted file creates no second note", before == after, f"{before} -> {after}")
check("renamed file is reported, not silently ignored",
      any(u["file"] == "renamed-by-hand.txt" for u in res2["unmatched"]), str(res2["unmatched"]))

# --- gated worklist shrinks ---
# Against the scratch state, never the machine's own: this assertion used to
# call gated(ROOT), which ignored its argument and loaded the real canvas.json,
# so it passed on the Mac's stale stub and failed on the box's real 685.
g = L.gated(data)
check("adopted readings leave the gated worklist", len(g) == 0, str(g))

# --- --force must not destroy hand-saved text ---
check("`manual` is protected from --force in run()",
      'if cached_status == MANUAL:' in open(pathlib.Path.home()/"automation/tools/canvas/links.py").read())

# --- dropzones ---
data2 = fresh_state()
import shutil; shutil.rmtree(FILES / COURSE / "_manual")
made = L.ensure_dropzones(ROOT, data2, dry_run=True)
check("dry run does NOT create the folder",
      made == [COURSE] and not (FILES / COURSE / "_manual").is_dir(), str(made))
made = L.ensure_dropzones(ROOT, data2)
check("missing drop folder is created", (FILES / COURSE / "_manual").is_dir(), str(made))

# A course the cache remembers under a name that no longer exists on disk.
d5 = fresh_state()
d5["readings"]["k_gone"] = {"url": "https://hbsp.harvard.edu/tu/zzz99999", "title": "Vanished",
                            "canvas_fetch_status": "login-required",
                            "notes": {"Old Course Name - X-000": "Areas/.../Readings/Vanished"}}
made = L.ensure_dropzones(ROOT, d5)
check("no drop folder for a renamed/stale course",
      "Old Course Name - X-000" not in made and not (FILES / "Old Course Name - X-000").exists(),
      str(made))

# --- titles that share a subject vocabulary must still separate ---
CONFUSABLE = {
    "c1": {"url": "https://hbsp.harvard.edu/tu/aaa11111", "title": "Urban Transit Costs at Northwind",
           "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/5 - Costs/Readings/Urban Transit Costs at Northwind"}},
    "c2": {"url": "https://hbsp.harvard.edu/tu/bbb22222", "title": "Urban Transit Costs at Lakeshore",
           "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/6 - Systems/Readings/Urban Transit Costs at Lakeshore"}},
}
for k, r in CONFUSABLE.items():
    q = VAULT / (r["notes"][COURSE] + ".md")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(f"---\ncanvas_fetch_status: login-required\n---\n\n# {r['title']}\n", encoding="utf-8")
(FILES / COURSE / "_manual").mkdir(parents=True, exist_ok=True)
for f in (FILES / COURSE / "_manual").iterdir():
    f.unlink()
drop("scan0042.txt", "Urban Transit Costs at Lakeshore. This case examines how the Lakeshore "
                     "system controls the cost of urban transit service across its depots.")
d3 = fresh_state(); d3["readings"] = json.loads(json.dumps(CONFUSABLE))
res3 = L.adopt(ROOT, d3, dry_run=True)
got = res3["matched"][0]["title"] if res3["matched"] else None
check("shared subject words do not confuse the match",
      got == "Urban Transit Costs at Lakeshore", f"got {got!r}; {res3['unmatched']}")

# And the reverse: a file that genuinely could be either must NOT be adopted.
for f in (FILES / COURSE / "_manual").iterdir():
    f.unlink()
drop("scan0043.txt", "A discussion of urban transit costs in large integrated systems.")
d4 = fresh_state(); d4["readings"] = json.loads(json.dumps(CONFUSABLE))
res4 = L.adopt(ROOT, d4, dry_run=True)
check("a genuinely ambiguous file is refused, not guessed",
      not res4["matched"], str(res4["matched"]))

# --- a tie that a sibling file resolves must not depend on filename order ---
# Real case, 2026-09-22: a lending note contains every word of both titles, so it tied
# with "Risk and Reward" and was refused because it sorted before the file that
# claimed Risk and Reward. The dry run also reported the tie, since it never
# removed a matched candidate.
VC = {
    "v1": {"url": "https://hbsp.harvard.edu/tu/e485e2b9", "title": "Risk and Reward in Seed Lending",
           "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/7 - Lending/Readings/Risk and Reward in Seed Lending"}},
    "v2": {"url": "https://hbsp.harvard.edu/tu/88025eac", "title": "Deal Structure and Deal Terms",
           "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/7 - Lending/Readings/Deal Structure and Deal Terms"}},
}
for k, r in VC.items():
    q = VAULT / (r["notes"][COURSE] + ".md")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(f"---\ncanvas_fetch_status: login-required\n---\n\n# {r['title']}\n", encoding="utf-8")
for f in (FILES / COURSE / "_manual").iterdir():
    f.unlink()
drop("806085-PDF-ENG.txt", "Deal Structure and Deal Terms. Seed lending investors weigh "
                           "risk against reward when they set terms and structure a deal.")
drop("811036-PDF-ENG.txt", "Risk and Reward in Seed Lending. Returns to seed lending "
                           "are concentrated; the risk of any one investment is high.")
for mode in (True, False):
    dv = fresh_state(); dv["readings"] = json.loads(json.dumps(VC))
    rv = L.adopt(ROOT, dv, dry_run=mode)
    got = {m["file"]: m["title"] for m in rv["matched"]}
    check(f"sibling-resolved tie adopts both ({'dry run' if mode else 'real run'})",
          got == {"806085-PDF-ENG.txt": "Deal Structure and Deal Terms",
                  "811036-PDF-ENG.txt": "Risk and Reward in Seed Lending"},
          f"{got}; {rv['unmatched']}")

# --- a profile page never competes with the document about that person ---
PROF = {
    "p1": {"url": "https://drive.google.com/file/d/EXAMPLE_DRIVE_FILE_ONE/view",
           "title": "Corvid: Time to Pivot? (September 2026)", "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/8 - Cases/Readings/Corvid - Time to Pivot (September 2026)"}},
    "p2": {"url": "https://www.linkedin.com/in/ines-thornbury-vale",
           "title": "Ines Thornbury Vale", "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/8 - Cases/Readings/Ines Thornbury Vale"}},
}
for k, r in PROF.items():
    q = VAULT / (r["notes"][COURSE] + ".md")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(f"---\ncanvas_fetch_status: login-required\n---\n\n# {r['title']}\n", encoding="utf-8")
for f in (FILES / COURSE / "_manual").iterdir():
    f.unlink()
drop("Corvid Case Sept 2026.txt", "DRAFT Rev. September 16, 2026. Corvid: Time to Pivot? "
                                  "Ines Thornbury Vale, CEO and co-founder, Corvid.")
dp = fresh_state(); dp["readings"] = json.loads(json.dumps(PROF))
rp = L.adopt(ROOT, dp, dry_run=True)
got = rp["matched"][0]["title"] if rp["matched"] else None
check("a LinkedIn profile does not tie with the case about that person",
      got == "Corvid: Time to Pivot? (September 2026)", f"got {got!r}; {rp['unmatched']}")
check("profile links are not matchable in adopt_from either",
      not L.matchable({"url": "https://www.linkedin.com/in/someone", "title": "Some Person Name"}))
check("a LinkedIn article is still matchable",
      L.matchable({"url": "https://www.linkedin.com/pulse/20130603165708-56725-everyone-pivots",
                   "title": "Everyone Pivots"}))

# --- `--as`: a person assigns what no text can match ---
# Real case, 2026-09-22: Calloway's Seven Pressures, whose only title is Canvas's
# anchor text "use this link instead".
CALLOWAY_URL = "https://drive.google.com/file/d/EXAMPLE_DRIVE_FILE_TWO/view?usp=sharing"
ASSIGN = {
    "a1": {"url": CALLOWAY_URL, "title": "use this link instead", "canvas_fetch_status": "login-required",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/9 - Strategy/Readings/use this link instead"}},
    "a2": {"url": "https://example.com/already", "title": "Already Fetched", "canvas_fetch_status": "ok",
           "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/9 - Strategy/Readings/Already Fetched"}},
}
OTHER = "Other Course - X-999"
(ROOT / OTHER).mkdir(parents=True, exist_ok=True)
(FILES / OTHER / "_manual").mkdir(parents=True, exist_ok=True)
for k, r in ASSIGN.items():
    q = VAULT / (r["notes"][COURSE] + ".md")
    q.parent.mkdir(parents=True, exist_ok=True)
    q.write_text(f"---\ncanvas_fetch_status: {r['canvas_fetch_status']}\n---\n\n# {r['title']}\n", encoding="utf-8")
for f in (FILES / COURSE / "_manual").iterdir():
    f.unlink()
calloway = "THE SEVEN QUIET PRESSURES THAT BEND STRATEGY. - LIBRARY.txt"
drop(calloway, "The Seven Quiet Pressures That Bend Strategy, by Thea E. Calloway.")
def fresh_assign():
    d = fresh_state(); d["readings"] = json.loads(json.dumps(ASSIGN)); return d

da = fresh_assign()
ra = L.adopt(ROOT, da, dry_run=True)
check("scored adopt still refuses the anchor-text reading", not ra["matched"], str(ra["matched"]))

r = L.adopt_as(ROOT, da, CALLOWAY_URL, calloway, dry_run=True)
check("--as dry run names the assignment", r.get("title") == "use this link instead", str(r))
check("--as dry run writes nothing", da["readings"]["a1"]["canvas_fetch_status"] == "login-required")

r = L.adopt_as(ROOT, da, CALLOWAY_URL.split("?")[0] + "?usp=drive_link", calloway)
check("--as tolerates a different query string", "error" not in r, str(r))
check("--as records the adoption as manual",
      da["readings"]["a1"]["canvas_fetch_status"] == "manual", str(da["readings"]["a1"]))
check("--as marks it as a person's match", da["readings"]["a1"].get("assigned") is True)
note = VAULT / (da["readings"]["a1"]["notes"][COURSE] + ".md")
check("--as puts the text in the reading's own note", "Thea E. Calloway" in note.read_text(encoding="utf-8"))

r = L.adopt_as(ROOT, da, CALLOWAY_URL, calloway)
check("--as refuses a file already adopted", "already adopted" in r.get("error", ""), str(r))

drop("second.txt", "Some other text entirely.")
r = L.adopt_as(ROOT, fresh_assign(), "https://example.com/already", "second.txt")
check("--as refuses a reading that is not gated", "not waiting" in r.get("error", ""), str(r))
r = L.adopt_as(ROOT, fresh_assign(), "https://example.com/nowhere", "second.txt")
check("--as refuses an unknown URL", "no reading" in r.get("error", ""), str(r))

stray = TMP / "stray.txt"; stray.write_text("Loose file.", encoding="utf-8")
r = L.adopt_as(ROOT, fresh_assign(), CALLOWAY_URL, str(stray))
check("--as refuses a file outside _manual/", "not in a course" in r.get("error", ""), str(r))

(FILES / OTHER / "_manual" / "wrong-course.txt").write_text("Calloway text.", encoding="utf-8")
r = L.adopt_as(ROOT, fresh_assign(), CALLOWAY_URL, str(FILES / OTHER / "_manual" / "wrong-course.txt"))
check("--as refuses a reading from another course", "not linked from" in r.get("error", ""), str(r))

# --- the gated worklist honours --course ----------------------------------
gd = {"readings": {
    "g1": {"url": "https://hbsp.harvard.edu/tu/aaa11111", "title": "Case A",
           "canvas_fetch_status": "login-required", "notes": {"Course A - F26-X-1": "n/a"}},
    "g2": {"url": "https://hbsp.harvard.edu/tu/bbb22222", "title": "Case B",
           "canvas_fetch_status": "login-required", "notes": {"Course B - F26-Y-1": "n/a"}},
    "g3": {"url": "https://example.com/ok", "title": "Fine", "canvas_fetch_status": "ok",
           "notes": {"Course A - F26-X-1": "n/a"}}}}
check("gated with no filter lists every course's", len(L.gated(gd)) == 2)
ga = L.gated(gd, {"Course A - F26-X-1"})
check("gated --course lists only that course's", [r["title"] for r in ga] == ["Case A"], str(ga))
check("gated rows say which course", ga and ga[0]["courses"] == ["Course A - F26-X-1"])

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
