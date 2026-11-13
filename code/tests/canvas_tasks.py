#!/usr/bin/env python3
"""The canvas_tasks rules, offline, against a scratch vault.

    ~/automation/.venv/bin/python -m tests.canvas_tasks

Shapes are real: the tasks made by hand on 2026-09-23 carry the assignment URL
in the body, and the job must adopt them rather than add a second copy.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import os, pathlib, sys, tempfile, time
from datetime import date

TMP = pathlib.Path(tempfile.mkdtemp(prefix="canvas-tasks-"))
os.environ["HOME"] = str(TMP / "home")
os.environ["VAULT_PATH"] = str(TMP / "vault")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import tasks as T

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

V = pathlib.Path(os.environ["VAULT_PATH"])
(V / "Inbox").mkdir(parents=True)
(V / "Archive" / "Inbox" / "2026-08").mkdir(parents=True)
URL = "https://canvas.stanford.edu/courses/{}/assignments/{}"

def task(name, status, due, url=None, where="Inbox", age=3600):
    p = V / where / f"{name}.md"
    p.write_text(f"---\ntype: action\ncaptured: 2026-09-23T11:45\nstatus: {status}\n"
                 f"projects:\n  - \"[[MBA]]\"\npriority: 3-normal\nwaiting: false\ndue: {due}\n---\n"
                 f"Canvas assignment.\n\n{url or ''}\n\nCourse: [[X]]\n", encoding="utf-8")
    t = time.time() - age
    os.utime(p, (t, t))
    return p

# 575: two assignments already tasked by hand (one open, one done), URL in body.
task("Complete Harbor Operations Simulation A (GSBGEN 510)", "open", "2026-09-30", URL.format(900108, 1001))
task("Complete Harbor Operations Simulation B (GSBGEN 510)", "done", "2026-10-07", URL.format(900108, 1002))
# 335: a hand task WITHOUT a URL whose title is near the assignment name.
task("Submit STRAMGT 121 group project organisation selection", "open", "2026-10-01")
# 353: task for an assignment that later vanished from Canvas.
gone = task("Submit STRAMGT 394 project proposal", "open", "2026-10-02", URL.format(900109, 3009))
# 353: a done task already swept to Archive/Inbox - must still block a duplicate.
task("Complete Market Sketch Memo (STRAMGT 394)", "done", "2026-09-20", URL.format(900109, 3001),
     where="Archive/Inbox/2026-08")
# 572: open task edited 2 minutes ago - must not be touched this run.
fresh = task("Complete Orchard write-up (STRAMGT 612)", "open", "2026-09-29", URL.format(900105, 5001), age=120)

tasks = T.load_tasks(V)
check("tasks found in Inbox/ and Archive/Inbox/", len(tasks) == 6, str(len(tasks)))

today = date(2026, 9, 24)
sub = {"submitted_at": "2026-09-24T10:00:00Z", "workflow_state": "submitted"}
courses = [
    {"id": 900108, "code": "F26-GSBGEN-510-01", "folder": "Leadership and Harbor Operations - F26-GSBGEN-510-01",
     "assignments": [
        # due moved from 30 Sep to 2 Oct (UTC 06:59 next day = 23:59 Pacific)
        {"id": 1001, "name": "Harbor Operations Simulation A", "due_at": "2026-10-03T06:59:00Z",
         "points_possible": 10, "html_url": URL.format(900108, 1001)},
        # done in Obsidian, NOT submitted on Canvas -> stays done, recorded only
        {"id": 1002, "name": "Harbor Operations Simulation B", "due_at": "2026-10-08T06:59:00Z",
         "html_url": URL.format(900108, 1002)},
        # new, due in 10 days -> create
        {"id": 1003, "name": "Harbor Briefing Memo: Draft", "due_at": "2026-10-05T00:00:00Z",
         "points_possible": 20.0, "html_url": URL.format(900108, 1003)},
        # new but due in 40 days -> beyond horizon
        {"id": 1004, "name": "Final Reflection", "due_at": "2026-11-03T08:00:00Z",
         "html_url": URL.format(900108, 1004)},
        # new, due soon, but already submitted -> no task
        {"id": 1005, "name": "Pre-course survey", "due_at": "2026-09-26T00:00:00Z",
         "submission": sub, "html_url": URL.format(900108, 1005)},
        # undated -> no task
        {"id": 1006, "name": "Participation", "html_url": URL.format(900108, 1006)},
     ]},
    {"id": 900110, "code": "F26-STRAMGT-121-01/EDUC-398-01", "folder": "Rethinking Libraries - F26-STRAMGT-121-01",
     "assignments": [
        {"id": 2001, "name": "Group Project Organisation Selection", "due_at": "2026-10-02T00:00:00Z",
         "html_url": URL.format(900110, 2001)},
     ]},
    {"id": 900109, "code": "F26-STRAMGT-394-01", "folder": "Studio Practice - F26-STRAMGT-394-01",
     "assignments": [
        # swept done task, submitted -> nothing; must NOT be recreated
        {"id": 3001, "name": "Market Sketch Memo", "due_at": "2026-09-30T00:00:00Z", "submission": sub,
         "html_url": URL.format(900109, 3001)},
     ]},
    {"id": 900105, "code": "F26-STRAMGT-612-01", "folder": "Field Methods - F26-STRAMGT-612-01",
     "assignments": [
        {"id": 5001, "name": "Orchard write-up", "due_at": "2026-09-29T00:00:00Z", "submission": sub,
         "html_url": URL.format(900105, 5001)},
     ]},
]
acts = T.plan(courses, tasks, today)
by = {}
for a in acts:
    by.setdefault(a.kind, []).append(a)
names = lambda k: sorted(a.title for a in by.get(k, []))

check("a changed due date is updated, in Pacific time",
      [(a.title, a.fields) for a in by.get("set_due", [])] == [("Harbor Operations Simulation A", {"due": "2026-10-02"})],
      str([(a.title, a.fields) for a in by.get("set_due", [])]))
check("exactly one task created: the new one inside the horizon",
      names("create") == ["Harbor Briefing Memo: Draft"], str(names("create")))
c = by.get("create", [None])[0]
check("new task title follows the house style",
      c and c.new_title == "Complete Harbor Briefing Memo - Draft (GSBGEN 510)", c and c.new_title)
check("new task body carries points, URL and course link",
      c and "20 points" in c.body and URL.format(900108, 1003) in c.body
      and "Course: [[Leadership and Harbor Operations - F26-GSBGEN-510-01]]" in c.body, c and c.body)
check("done in Obsidian but not submitted: left done, only recorded",
      names("done_not_submitted") == ["Harbor Operations Simulation B"], str(names("done_not_submitted")))
check("no URL but similar title in the same course: reported, not created",
      names("possible_match") == ["Group Project Organisation Selection"], str(names("possible_match")))
check("a swept done task still blocks its duplicate", "Market Sketch Memo" not in names("create"))
check("an assignment gone from Canvas is flagged, not deleted",
      names("removed") == ["Submit STRAMGT 394 project proposal"] and gone.exists(), str(names("removed")))
check("a task edited minutes ago is deferred, not written",
      names("recent") == ["Orchard write-up", "Orchard write-up"] and not by.get("set_done")
      and sorted(k for a in by["recent"] for k in a.fields) == ["completedDate", "due", "status"],
      str([(a.title, a.fields) for a in by.get("recent", [])]))

# --- applying ---------------------------------------------------------------
sd = by["set_due"][0]
T.set_fields(sd.task.path, sd.fields)
txt = sd.task.path.read_text(encoding="utf-8")
check("set_fields rewrites only the due line", "due: 2026-10-02" in txt and "status: open" in txt
      and txt.count("due:") == 1 and "Canvas assignment." in txt, txt)
p = T.create_task(V, c)
new = p.read_text(encoding="utf-8") if p else ""
check("created task has the triage frontmatter",
      "status: inbox" in new and "priority: 1-none" in new and "due: 2026-10-04" in new
      and '"[[MBA]]"' in new and "type: action" in new, new)
check("a second create does not overwrite", T.create_task(V, c) is None)
T.set_fields(fresh, {"status": "done", "completedDate": "2026-09-24"})
t2 = fresh.read_text(encoding="utf-8")
check("set_fields adds a missing key", "completedDate: 2026-09-24" in t2 and "status: done" in t2, t2)
# Re-planning after applying: nothing new to create (idempotent).
acts2 = T.plan(courses, T.load_tasks(V), today, now=time.time() + 3600)
check("a second run creates nothing", not [a for a in acts2 if a.kind == "create"],
      str([a.title for a in acts2 if a.kind == "create"]))

# --- writing file (2026-09-30) -------------------------------------------------
ROOT = V / "Areas" / "MBA" / "Course Archive"
course = ROOT / "Fall 2026" / "Enrolled" / c.folder
course.mkdir(parents=True)
c.assignment["description"] = "<p>Write one <b>memo</b>.</p>"
w = T.ensure_writing_file(ROOT, c)
check("writing file created under the course's _mine/Assignments, found via locate",
      w == course / "_mine" / "Assignments" / "Harbor Briefing Memo - Draft"
      / "Harbor Briefing Memo - Draft.md", str(w))
wt = w.read_text(encoding="utf-8")
check("writing file carries due, Canvas link, prompt and a Notes section",
      "**Due:**" in wt and f"[Canvas]({URL.format(900108, 1003)})" in wt
      and "memo" in wt and "## Notes" in wt and "type: note" in wt, wt)
w.write_text(wt + "my draft\n", encoding="utf-8")
check("a second call returns the same file and does not overwrite it",
      T.ensure_writing_file(ROOT, c) == w and "my draft" in w.read_text(encoding="utf-8"))
decoy = w.parent.parent / "Other" / "Other.md"
decoy.parent.mkdir()
decoy.write_text(f"# Other\n\n[Canvas]({URL.format(900108, 9999)})\n\n> see {URL.format(900108, 1003)}\n", encoding="utf-8")
check("a prompt that merely links this assignment is not mistaken for its file",
      T.find_writing_file(course, "1003") == w, str(T.find_writing_file(course, "1003")))
renamed = w.parent.parent / "Memo he renamed" / "Memo he renamed.md"
renamed.parent.mkdir(); w.rename(renamed); w.parent.rmdir()
check("an existing file is found by its Canvas URL even after a rename",
      T.ensure_writing_file(ROOT, c) == renamed and not w.exists())
p.unlink()
p2 = T.create_task(V, c, renamed)
nt = p2.read_text(encoding="utf-8")
lines = nt.split("---\n", 2)[2].splitlines()
check("the task links the writing file directly under the Next line",
      lines[1].startswith("- [ ] Next:") and lines[3].startswith(
          "**Write it here:** [Memo he renamed](obsidian://open?vault=<VAULT_NAME>&file=Areas%2FMBA%2F"),
      "\n".join(lines[:5]))

# --- readings (2026-10-05) -----------------------------------------------------
# Real shapes: STRAMGT 387 (module with page, Materials, Readings), OB 242
# (a prompt linking sibling assignments), ECON 342 (everything _Unfiled).
CU = "https://canvas.stanford.edu/courses/900108"
def note(rel, kind, title, body="", **fm):
    p = course / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = "".join(f'{k}: "{v}"\n' for k, v in fm.items())
    p.write_text(f"---\ntype: class-material\ncanvas_kind: {kind}\n{extra}---\n\n# {title}\n\n{body}",
                 encoding="utf-8")
    return p
REL = lambda p: str(p.relative_to(V)).removesuffix(".md")
mod = "3 - Harbor Day (10-05-2026)"
hbr = note(f"{mod}/Readings/https ---hbr.org-crisis.md", "reading", "https://hbr.org/crisis",
           canvas_external_url="https://hbr.org/crisis", canvas_fetch_status="paywalled")
deck = note(f"{mod}/Materials/Deck.pdf.md", "file", "Deck.pdf",
            canvas_file_path=f"Areas/MBA/Course Archive/_files/{c.folder}/{mod}/Deck.pdf",
            canvas_extract_status="ok")
page = note(f"{mod}/{mod}.md", "page", "Harbor Day", canvas_url=f"{CU}/pages/harbor-day")
note(f"{mod}/Discussions/Chat.md", "discussion", "Chat")
sib = note(f"{mod}/Assignments/Sibling.md", "assignment", "Sibling task",
           canvas_url=f"{CU}/assignments/1007")
memo = note(f"{mod}/Assignments/Memo.md", "assignment", "Harbor Briefing Memo: Draft",
            f"Read the [[{REL(hbr)}|HBR piece]] and watch [*Sibling*]({CU}/assignments/1007), "
            f"also [Other](https://example.com/other) and [[{c.folder}]].\n\n"
            f"## My submission\n\n[mine](https://example.com/mine)\n",
            canvas_url=f"{CU}/assignments/1003")
note("_Unfiled/Assignments/Loose.md", "assignment", "Loose", f"See [[{REL(hbr)}|link]].",
     canvas_url=f"{CU}/assignments/1010")
note("_Unfiled/Assignments/Bare.md", "assignment", "Bare", canvas_url=f"{CU}/assignments/1011")
note("4 - Empty/Assignments/E.md", "assignment", "E", canvas_url=f"{CU}/assignments/1013")

blk = T.readings_block(V, ROOT, c.folder, "1003")
bl = blk.splitlines()
check("readings block names its session", bl[0] == f"Readings ({mod}):", bl[0])
check("the assignment's own links first, then page and files; deduped; no discussions",
      [l.split("](")[0] for l in bl[1:]] == ["- [ ] [HBR piece", "- [ ] [Sibling task",
                                              "- [ ] Other · [original", "- [ ] [Harbor Day", "- [ ] [Deck.pdf"],
      blk)
check("every reading carries the archive note and the original",
      f"(obsidian://open?vault=<VAULT_NAME>&file={T.quote(REL(hbr), safe='')}) · "
      "[original](https://hbr.org/crisis) (archive text: paywalled)" in bl[1]
      and f"· [original]({CU}/pages/harbor-day)" in bl[4], blk)
check("a file links its raw copy in _files", "· [PDF](obsidian://" in bl[5]
      and T.quote(f"_files/{c.folder}/{mod}/Deck.pdf", safe="") in bl[5]
      and "archive text" not in bl[5], bl[5])
check("a reading with no archive copy keeps its original link, never a bare title",
      bl[3] == "- [ ] Other · [original](https://example.com/other)", bl[3])
check("links in his own submission are not readings", "example.com/mine" not in blk)
check("no module: own links, then one honest line",
      T.readings_block(V, ROOT, c.folder, "1010") ==
      "Readings linked from the assignment:\n"
      f"- [ ] [hbr.org/crisis](obsidian://open?vault=<VAULT_NAME>&file={T.quote(REL(hbr), safe='')})"
      " · [original](https://hbr.org/crisis) (archive text: paywalled)\n"
      "Session readings not mapped: this assignment is in no Canvas module.\n",
      T.readings_block(V, ROOT, c.folder, "1010"))
check("no module and no links: only the honest line",
      T.readings_block(V, ROOT, c.folder, "1011").startswith("Session readings not mapped"))
check("a session with nothing says so", T.readings_block(V, ROOT, c.folder, "1013")
      == "No readings in this session on Canvas.\n")
check("an assignment not in the archive says so",
      T.readings_block(V, ROOT, c.folder, "1099").startswith("Readings not found"))

p2.unlink()
p3 = T.create_task(V, c, renamed, blk)
b3 = p3.read_text(encoding="utf-8").split("---\n", 2)[2].splitlines()
check("new task: Next, writing file, then readings, then the Canvas details",
      b3[1].startswith("- [ ] Next:") and b3[3].startswith("**Write it here:**")
      and b3[5] == f"Readings ({mod}):" and b3[11] == "" and b3[12].startswith("Canvas assignment"),
      "\n".join(b3[:13]))

# Backfill: open without readings -> filled; hand-filled, done, recent -> left.
def ctask(name, status, aid, extra="", age=3600):
    p = V / "Inbox" / f"{name}.md"
    p.write_text(f"---\ntype: action\nstatus: {status}\ndue: 2026-10-04\n---\n## Next action\n"
                 f"- [ ] Next: complete and submit on Canvas\n\n**Write it here:** [W](obsidian://"
                 f"open?vault=<VAULT_NAME>&file=Areas%2FMBA%2FCourse%20Archive%2Fx%2F_mine%2FW)\n\n"
                 f"{extra}Canvas assignment.\n\n{CU}/assignments/{aid}\n\nCourse: [[{c.folder}]]\n",
                 encoding="utf-8")
    os.utime(p, (time.time() - age,) * 2)
    return p
old = ctask("Old memo", "inbox", 1003)
hand = ctask("Hand filled", "open", 1010, "Readings: session 8.\n- [ ] [x](obsidian://open?vault="
             "<VAULT_NAME>&file=Areas%2FMBA%2FCourse%20Archive%2Fy)\n\n")
ctask("Done one", "done", 1011)
fresh2 = ctask("Fresh one", "inbox", 1013, age=60)
mine = {old, hand, V / "Inbox" / "Done one.md", fresh2}
fill, wait = T.plan_backfill([t for t in T.load_tasks(V) if t.path in mine])
check("backfill picks only the open task without readings",
      [t.path for t, _ in fill] == [old] and [t.path for t, _ in wait] == [fresh2]
      and fill[0][1] == c.folder, str([(t.title, f) for t, f in fill + wait]))
check("a writing-file link alone does not count as readings",
      not T.has_readings(old.read_text(encoding="utf-8")))
before = old.read_text(encoding="utf-8")
check("backfill inserts once", T.insert_readings(old, blk) and not T.insert_readings(old, blk))
after = old.read_text(encoding="utf-8")
check("backfill only inserts: frontmatter and every old line kept, block after the writing link",
      after.startswith("---\ntype: action\nstatus: inbox\n") and all(l in after for l in before.splitlines())
      and after.index("**Write it here:**") < after.index(f"Readings ({mod}):") < after.index("Canvas assignment."),
      after)
nf = ctask("Not yet archived", "inbox", 1003, T.NOT_FOUND + "\n\n")
check("a 'not found' placeholder is not a readings block, and is replaced, not duplicated",
      not T.has_readings(nf.read_text(encoding="utf-8")) and not T.insert_readings(nf, T.NOT_FOUND + "\n")
      and T.insert_readings(nf, blk) and T.NOT_FOUND not in nf.read_text(encoding="utf-8")
      and nf.read_text(encoding="utf-8").count(f"Readings ({mod}):") == 1, nf.read_text(encoding="utf-8"))

rel = V / "Inbox" / "Complete Reflection 1 (OB 242).md"
rel.write_text("---\ntype: action\nstatus: open\ndue: 2026-10-01\n---\n## Next action\n- [ ] Next: x\n\n"
               f"- [ ] Video · [original]({URL.format(900106, 817866)})\n\n{URL.format(900106, 817860)}\n",
               encoding="utf-8")
got = next(t for t in T.load_tasks(V) if t.path == rel)
check("a task's own URL line wins over related assignment links above it",
      got.aid == "817860", str(got.aid))

# --- several links, none on its own line (Nov 11, 2026: the FNV final paper task
# linked the midterm in a background bullet first and took its Oct 23 due) --------
fnv = V / "Inbox" / "Submit the FNV final paper (Pharos vs BreakLine).md"
fnv.write_text("---\ntype: action\ncaptured: 2026-11-10T12:39\nstatus: inbox\ndue: 2026-12-04\n---\n"
               "## Next action\n- [ ] Next: x\n\n"
               f"- **Check:** confirm the midterm on [Canvas]({URL.format(900109, 818461)})\n\n"
               f"Canvas: [Final Paper, due Fri Dec 4]({URL.format(900109, 818460)})\n", encoding="utf-8")
t = time.time() - 3600
os.utime(fnv, (t, t))
got = next(x for x in T.load_tasks(V) if x.path == fnv)
check("several assignment links and none on its own line: linked to none",
      got.aid is None and got.ambiguous == ["818461", "818460"], f"{got.aid} {got.ambiguous}")
mid = [{"id": 900109, "code": "F26-STRAMGT-394-01", "folder": "Studio Practice - F26-STRAMGT-394-01",
        "assignments": [{"id": 818461, "name": "Midterm Paper", "due_at": "2026-10-23T17:00:00Z",
                         "submission": sub, "html_url": URL.format(900109, 818461)},
                        {"id": 818460, "name": "Final Paper", "due_at": "2026-12-04T18:00:00Z",
                         "html_url": URL.format(900109, 818460)}]}]
acts3 = [a for a in T.plan(mid, T.load_tasks(V), date(2026, 11, 11)) if a.task and a.task.path == fnv]
check("that task is reported as ambiguous and never gets the other assignment's due or done",
      [a.kind for a in acts3] == ["ambiguous"], str([(a.kind, a.fields) for a in acts3]))
one = V / "Inbox" / "Only one link.md"
one.write_text("---\ntype: action\nstatus: inbox\n---\n"
               f"See [it]({URL.format(900109, 818460)}) and [again]({URL.format(900109, 818460)})\n",
               encoding="utf-8")
got = next(x for x in T.load_tasks(V) if x.path == one)
check("one assignment linked inline, even twice, still links", got.aid == "818460" and not got.ambiguous,
      f"{got.aid} {got.ambiguous}")
one.unlink()
fnv.write_text(fnv.read_text(encoding="utf-8").replace("due: 2026-12-04", "due: 2026-12-05")
               + f"\n{URL.format(900109, 818461)}\n", encoding="utf-8")   # wrongly owned by the midterm
os.utime(fnv, (t, t))
acts4 = [a for a in T.plan(mid, T.load_tasks(V), date(2026, 11, 11)) if a.task and a.task.path == fnv]
check("a Canvas due before the task was captured is reported; neither due nor done is written",
      [a.kind for a in acts4] == ["due_before_capture"]
      and "captured 2026-11-10" in acts4[0].detail, str([(a.kind, a.detail) for a in acts4]))
fnv.unlink()

check("course number from a cross-listed code", T.course_number("F26-STRAMGT-121-01/EDUC-398-01") == "STRAMGT 121")
check("course number with a letter", T.course_number("F26-CS-351Z-01") == "CS 351Z")

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
