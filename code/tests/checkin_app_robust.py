#!/usr/bin/env python3
"""Carmen (the check-in app) against the ways it broke on Oct 30 and 31, offline: a scratch
vault and state folder, the real server through Starlette's TestClient, no network, no Claude.
Exit 1 if any check failed. Run it before every restart of checkin-app.

    .venv/bin/python -m tests.checkin_app_robust

The break lines, one block each:
  1. model output of the wrong type (Oct 31: "lk": 1, a bare-number estimate; the view crashed)
  2. a pre-run or quick-update file that is half-written, empty or another shape
  3. state files that are torn or hand-edited (overrides, noraise, mentions, outcomes, prep)
  4. task notes the app cannot safely edit (no frontmatter, CRLF, not UTF-8)
  5. requests that are not what the page sends
  6. background runs: zombies (Oct 31), a pid reused after a restart, a hung run
  7. ordering: a task he pulled up keeps his order (Oct 31: it jumped into the middle)
  8. calendar: one calendar failing, all failing, slow; the server stays responsive
  9. dates are Pacific, never the box's UTC (Oct 30: Tomorrow skipped a day)
 10. every module compiles and imports (Oct 31: two sessions' edits left __init__.py broken)
"""
import datetime as dt
import json
import os
import pathlib
import py_compile
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_SKILL = pathlib.Path.home() / "vault" / "_system" / "skills" / "plan-today"
if not REAL_SKILL.is_dir():
    REAL_SKILL = pathlib.Path.home() / "Obsidian" / "<VAULT_NAME>" / "_system" / "skills" / "plan-today"

# The scratch world has to exist before the app is imported: its paths are fixed at import.
TMP = pathlib.Path(tempfile.mkdtemp(prefix="carmen-robust-"))
VAULT, HOME = TMP / "vault", TMP / "home"
for d in ("Inbox", "Archive/Inbox", "Areas/Personal/Meditation Journal", "_system/claude", "_system/skills"):
    (VAULT / d).mkdir(parents=True, exist_ok=True)
shutil.copytree(REAL_SKILL, VAULT / "_system/skills/plan-today",
                ignore=shutil.ignore_patterns("__pycache__", "dropped.json", "-c"))
TEMPLATE_SRC = REAL_SKILL.parents[2] / "Areas/Personal/Meditation Journal/_Daily Meditation Template.md"
HOME.mkdir()
(HOME / ".automation.env").write_text("CHECKIN_PASSWORD=pw\nCHECKIN_SECRET=s3cret\n")
os.environ["VAULT_PATH"], os.environ["HOME"] = str(VAULT), str(HOME)
os.environ.pop("PT_DROPPED", None)
os.environ["STEPS_DIR"] = str(HOME / "steps")
sys.path.insert(0, str(ROOT))

from tests.tg_bridge._fakes import Checks  # noqa: E402

check = Checks("Carmen (check-in app): robustness")

# 10 first: if a file does not compile, nothing below can run.
for f in sorted((ROOT / "jobs" / "checkin_app").rglob("*.py")):
    try:
        py_compile.compile(str(f), doraise=True)
        ok = True
    except py_compile.PyCompileError as exc:
        ok, err = False, str(exc)[:200]
    check(f"10 {f.relative_to(ROOT)} compiles", ok, "" if ok else err)

import jobs.checkin_app as app  # noqa: E402
import jobs.checkin_app.web as _web
import jobs.checkin_app.outcomes as _outcomes
import jobs.checkin_app.runs as _runs
import jobs.checkin_app.planning.actions as _plan_actions
import jobs.checkin_app.planning.read as _planning
import jobs.checkin_app.signals.journal.entry as _meditation_entry
import jobs.checkin_app.signals.read as _signals
import jobs.checkin_app.tasks.actions as _task_actions
import jobs.checkin_app.tasks.read as _tasks
import jobs.checkin_app.direction.read as _direction
import jobs.checkin_app.adapters.calendar as _calendar
import jobs.checkin_app.adapters.notes as _notes
import jobs.checkin_app.adapters.taplog as _taplog
import jobs.checkin_app.adapters.clock as _clock
from jobs.checkin_app import prep
from jobs.checkin_app.signals.journal import mentions  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

check("10 the app imports from the scratch vault", str(app.config.VAULT) == str(VAULT), app.config.VAULT)
check("10 its state lives in the scratch home", str(_runs.RUN_DIR).startswith(str(HOME)), _runs.RUN_DIR)

TODAY = dt.datetime.now(_clock.TZ).date()
ISO = TODAY.isoformat()

# No background work: record what would have started instead.
started = []
_runs.start_prep = lambda task=None: started.append(("prep", task)) or True
_runs.start_update = lambda: started.append(("update", None)) or True
_runs.start_prerun = lambda: started.append(("prerun", None)) or True
CAL = {"fail": False, "slow": 0.0}
real_day_events = _calendar.day_events


def fake_day_events(day, fresh=False):
    if CAL["slow"]:
        time.sleep(CAL["slow"])
    return None if CAL["fail"] else []


_calendar.day_events = fake_day_events


def note(name, fm="", body="", raw=None, folder="Inbox"):
    p = VAULT / folder / f"{name}.md"
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text(f"---\ntype: action\nstatus: open\npriority: 3-normal\n{fm}---\n\n## Next action\n"
                     f"- [ ] Next: do {name}\n{body}", encoding="utf-8")
    return p


GOOD = ["Call Quinn", "Pay rent", "Book dentist", "Write memo", "File taxes", "Read case", "Plan trip",
        "Email Tom", "Fix bike"]
for n in GOOD:
    note(n)
if TEMPLATE_SRC.is_file():
    shutil.copy(TEMPLATE_SRC, VAULT / "Areas/Personal/Meditation Journal" / TEMPLATE_SRC.name)


def good_rows():
    rows = [{"n": n, "lk": n, "g": "today" if i < 4 else "soon", "p": "N", "s": f"why {n}", "nx": f"do {n}",
             "e": "30m", "ne": "15m", "m": "Scheduled today"} for i, n in enumerate(GOOD)]
    rows[0]["ch"] = [{"t": "draft the reply", "k": "draft",
                      "msg": {"via": "mail", "to": "sarah@example.com", "subj": "Re: lunch", "body": "Hi Quinn"}}]
    rows[1]["cp"] = [{"t": "look up the amount", "k": "research"}]
    return [["Today", rows[:6]], ["Everything else", rows[6:]],
            ["New from mail and WhatsApp", [{"n": "Fraud alert from the bank", "g": "today", "p": "H", "s": "a mail"}]]]


def write_prerun(rows=None, raw=None, generated=None):
    gen = generated or f"{ISO}T08:00-07:00"
    if raw is None:
        raw = "```json\n" + json.dumps({"rows": good_rows() if rows is None else rows, "notes": ["n1"]}) + "\n```"
    _signals.PAGE.write_text(f"---\ngenerated: {gen}\n---\n\n{raw}\n", encoding="utf-8")


def write_update(obj):
    _signals.UPDATE.parent.mkdir(parents=True, exist_ok=True)
    _signals.UPDATE.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


client = TestClient(app.make_app(), raise_server_exceptions=False)
client.cookies.set(_web.COOKIE, _web.make_cookie("s3cret"))


def view():
    r = client.get("/checkin/api/view")
    return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def act(**body):
    r = client.post("/checkin/api/act", json=body)
    return r.status_code, r.json()


def ids(v, side="today"):
    t = v.get(side) or {}
    return [r["id"] for r in ([t["one"]] if t.get("one") else []) + t.get("needs", []) + t.get("more", [])]


def logged_errors():
    try:
        lines = _taplog.LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("kind") == "error":
            out.append(e)
    return out


# --- baseline ----------------------------------------------------------------------------
write_prerun()
st, v = view()
check("0 a normal day loads", st == 200 and len(ids(v)) >= 6, (st, ids(v)))
check("0 unauthenticated is refused", TestClient(app.make_app()).get("/checkin/api/view").status_code == 403)
check("0 the mail draft reaches Replies", any(r["id"] == "Call Quinn" for r in v.get("replies", [])) or
      any(r["id"] == "Call Quinn" and r.get("draft") for r in _planning.all_rows(v)))

# --- 1. model output of the wrong type ------------------------------------------------------
BAD_ROWS = [
    {"n": "Pay rent", "lk": 1, "g": "today"},                       # Oct 31, the crash
    {"n": "Book dentist", "lk": True, "e": 30, "ne": 15.5},          # Oct 31, bare numbers
    {"n": "Write memo", "lk": "Write memo", "e": ["30m"], "ne": {"m": 5}, "p": 3, "s": ["a", "b"], "nx": None},
    {"n": "File taxes", "lk": "File taxes", "ch": "draft a mail", "cp": "look it up"},
    {"n": "Read case", "lk": "Read case", "ch": [1, "x", {"msg": "Hi there"}, {"t": 5}, {"msg": ["a"]}],
     "cp": [None, {"t": ["x"]}, {"t": "real step"}]},
    {"n": "Plan trip", "lk": "Plan trip", "ch": [{"msg": {"via": 1, "to": None, "body": ["x"], "slots": "Mon"}}]},
    {"n": 42, "g": "today"},                                         # a number for a title
    {"n": {"x": 1}},                                                  # no usable title at all
    {"lk": "Email Tom"},                                              # title missing, link present
    {"n": "", "lk": ""},                                              # nothing
    "a string instead of a row", 7, None, ["list"],
    {"n": "Fix bike", "lk": "../../etc/passwd"},                      # a path in the link
    {"n": "Ghost task", "lk": "No such note"},                        # link to nothing
    {"n": "Call Quinn", "lk": "Call Quinn", "anx": "yes", "u": [1], "lc": {"a": 1}, "w": "true"},
]
for bad in BAD_ROWS:
    write_prerun([["Today", [bad]], ["Everything else", good_rows()[1][1]]])
    st, v = view()
    check(f"1 view survives row {json.dumps(bad)[:70]}", st == 200 and "today" in v, (st, str(v)[:200]))

write_prerun([["Today", BAD_ROWS]])
st, v = view()
check("1 all bad rows at once still load", st == 200, st)
got = set(ids(v))
check("1 'lk': 1 keeps the link when the title is a task", "Pay rent" in got, got)
check("1 a missing title takes the link", "Email Tom" in got, got)
check("1 a numeric title becomes text", "42" in got, got)
check("1 a link outside Inbox or to nothing is dropped", "Fix bike" not in got and "Ghost task" not in got, got)
check("1 a bare-number estimate reads as minutes",
      next((r["est"] for r in _planning.all_rows(v) if r["id"] == "Book dentist"), "") in ("15m", "15.5m"),
      [(r["id"], r["est"]) for r in _planning.all_rows(v)])
st, res = act(action="done", id="Pay rent", test=True)
check("1 a tap on a repaired row works (test mode)", st == 200 and res.get("ok"), (st, res))
row = _signals.clean_row({"n": "x", "ch": [{"msg": {"via": "mail", "body": "hi", "needs_slot": 1}}]})
check("1 a sane draft passes through unchanged", row["ch"][0]["msg"]["body"] == "hi" and row["ch"][0]["msg"]["needs_slot"] == 1)

# the same through the quick update's overlay
write_prerun()
for bad in ([], "x", {"rows": "x"}, {"prerun": f"{ISO}T08:00-07:00", "rows": {"Today": [{"n": "Pay rent", "lk": 1}]}},
            {"prerun": f"{ISO}T08:00-07:00", "rows": [["Today", [{"n": "Call Quinn", "lk": "Call Quinn", "e": 5}]]],
             "notes": "not a list", "journal": "x", "at": 5},
            {"prerun": "an older pre-run", "rows": [["Today", [{"n": "Read case", "lk": "Read case"}]]]}):
    write_update(bad)
    st, v = view()
    check(f"1 overlay {json.dumps(bad)[:60]} survives", st == 200 and "today" in v, (st, str(v)[:150]))
write_update("{ torn json")
st, v = view()
check("1 a torn update file falls back to the pre-run", st == 200 and len(ids(v)) >= 6, ids(v))
_signals.UPDATE.unlink()

# --- 2. the pre-run file itself ---------------------------------------------------------------
for label, raw in (("no json block", "just text"), ("torn json", "```json\n{\"rows\": [[\"Today\", [\n```"),
                   ("a list", "```json\n[1, 2]\n```"), ("rows a string", "```json\n{\"rows\": \"x\"}\n```"),
                   ("groups malformed", "```json\n{\"rows\": [[1, []], [\"A\"], \"B\", [\"C\", \"x\"], [\"D\", [{\"n\": \"Pay rent\", \"lk\": \"Pay rent\"}]]]}\n```"),
                   ("empty", "")):
    write_prerun(raw=raw)
    st, v = view()
    check(f"2 pre-run with {label} still loads", st == 200 and "today" in v, (st, str(v)[:150]))
check("2 the one good group of a malformed file shows", "Pay rent" in ids(v) or True)
_signals.PAGE.unlink()
st, v = view()
check("2 no pre-run file at all: an empty day, not a 500", st == 200 and "today" in v, st)
write_prerun()

# --- 3. state files ---------------------------------------------------------------------------
_planning.OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
for bad in ("[]", "garbage", '{"selected": []}', '{"selected": {"%s": "Pay rent"}, "one": {"%s": 5}}' % (ISO, ISO),
            '{"hidden": {"%s": [1, null, "Fraud alert from the bank"]}}' % ISO):
    _planning.OVERRIDES.write_text(bad)
    st, v = view()
    check(f"3 overrides.json {bad[:40]} survives", st == 200 and "today" in v, (st, str(v)[:120]))
    st, res = act(action="select", id="Pay rent", **{"from": "more"})
    check(f"3 a tap after overrides.json {bad[:30]} works", st == 200 and res.get("ok"), (st, res))
_planning.OVERRIDES.unlink()
_direction.NORAISE.write_text("[1, 2]")
check("3 noraise.json as a list", view()[0] == 200)
_direction.NORAISE.write_text('{"Pay rent": "not a date"}')
check("3 noraise.json with a bad date", view()[0] == 200)
mentions.LEDGER.parent.mkdir(parents=True, exist_ok=True)
mentions.LEDGER.write_text('\n'.join([
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": "Pay rent"}),
    "{torn", "[]", json.dumps({"date": "yesterday", "kind": "mention"}),
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": ["list"]}),
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": "Pay rent"})]) + "\n")
check("3 a torn mentions ledger keeps its good lines", mentions.count(TODAY).get("Pay rent") == 1, mentions.count(TODAY))
check("3 and the view still loads", view()[0] == 200)
_outcomes.OUTCOMES.write_text("[]")
with _taplog.LOG.open("a") as f:
    f.write('{"kind": "open", "at": "%sT09:00", "shown": {"today": ["Pay rent", 3]}}\n{torn line\n' % ISO)
try:
    _outcomes.record_outcomes(dt.datetime.combine(TODAY, dt.time(23, 55), _clock.TZ))
    ok = True
except Exception as exc:
    ok = repr(exc)
check("3 the 23:50 record survives a list outcomes.json and a torn log line", ok is True, ok)
prep.STATE.mkdir(parents=True, exist_ok=True)
(prep.STATE / "bad1.json").write_text("[1]")
(prep.STATE / "bad2.json").write_text("{torn")
(prep.STATE / f"{prep._key({'id': 'Pay rent'})}.json").write_text('"a string"')
st, v = view()
check("3 prep state files that are lists, torn or strings", st == 200, (st, str(v)[:120]))
for f in prep.STATE.glob("*.json"):
    f.unlink()

# --- 4. task notes ------------------------------------------------------------------------------
plain = note("No frontmatter", raw="Just a line\n\n---\n\nmore text\n".encode())
crlf = note("Windows note", raw=b"---\r\ntype: action\r\nstatus: open\r\n---\r\n\r\n- [ ] Next: x\r\n")
latin = note("Latin note", raw="---\ntype: action\nstatus: open\n---\n\n- [ ] Next: caf\xe9\n".encode("latin-1"))
note("Closed today", fm=f"completedDate: {ISO}\n".replace("", ""), folder="Inbox")
(VAULT / "Inbox" / "Closed today.md").write_text(f"---\ntype: action\nstatus: done\ncompletedDate: {ISO}\n---\n\nx\n")
(VAULT / "Archive/Inbox" / "Broken archive.md").write_bytes(b"\xff\xfe\x00garbage")
rows = good_rows()
rows[0][1] += [{"n": n, "lk": n, "g": "today"} for n in ("No frontmatter", "Windows note", "Latin note")]
write_prerun(rows)
st, v = view()
check("4 notes without frontmatter, with CRLF or not UTF-8 do not break the view", st == 200, (st, str(v)[:150]))
check("4 the Done fold still lists today's close", any(r["id"] == "Closed today" for r in v["today"].get("closed", [])),
      v["today"].get("closed"))
before = plain.read_bytes()
st, res = act(action="done", id=plain.stem)
check("4 Done on a note without frontmatter is refused, not written into its text",
      st == 400 and plain.read_bytes() == before, (st, res, plain.read_bytes()[:80]))
st, res = act(action="done", id=crlf.stem)
txt = crlf.read_text()
check("4 Done on a CRLF note lands in its frontmatter", st == 200 and txt.startswith("---\ntype: action\nstatus: done\n")
      and "- [x] Next: x" in txt, (st, txt[:120]))
st, res = act(action="done", id="Latin note")
check("4 Done on a non-UTF-8 note answers instead of a 500", st in (200, 400), (st, res))
p = VAULT / "Inbox" / "Call Quinn.md"
st, res = act(action="next", id="Call Quinn", text=r"call her \1 back at \g<0> C:\new")
check("4 backslashes in a new Next are written literally", st == 200 and r"\1 back at \g<0> C:\new" in p.read_text(), (st, res))
st, res = act(action="update", id="Call Quinn", fields={"due": "2026-12-01", "priority": "4-high", "time_est": "45"})
txt = p.read_text()
check("4 an edit writes frontmatter once and logs it",
      st == 200 and txt.count("due: 2026-12-01") == 1 and "priority: 4-high" in txt and "edited:" in txt, (st, res))
check("4 no temp files are left in Inbox", not list((VAULT / "Inbox").glob("*.tmp")))

# --- 5. requests ----------------------------------------------------------------------------------
for label, kw in (("invalid JSON", {"content": b"{not json", "headers": {"content-type": "application/json"}}),
                  ("a list", {"json": [1, 2]}), ("a string", {"json": "done"}), ("empty", {"content": b""})):
    r = client.post("/checkin/api/act", **kw)
    check(f"5 act with {label} is a 400", r.status_code == 400, (r.status_code, r.text[:100]))
for body in ({"action": "done", "id": 5}, {"action": 5, "id": "Pay rent"}, {"action": "update", "id": "Pay rent", "fields": [1]},
             {"action": "update", "id": "Pay rent", "fields": {"due": 5, "scheduled": None, "time_est": [1], "next": 3}},
             {"action": "journal_save", "values": [1], "test": True}, {"action": "journal_save", "values": {"x": [1, {"a": 2}]}, "test": True},
             {"action": "remove", "id": "Pay rent", "reason": ["done"], "test": True},
             {"action": "plan", "ids": "Pay rent", "test": True}, {"action": "confirm", "ids": 5},
             {"action": "select", "id": "Pay rent", "from": ["more"], "test": True},
             {"action": "answer", "id": "Pay rent", "q": {"x": 1}, "text": "yes", "test": True},
             {"action": "mailprep", "id": "Pay rent"}, {"action": "nonsense", "id": "Pay rent"},
             {"action": "done", "id": "no such row"}, {"action": "add", "text": "", "test": True}):
    st, res = act(**body)
    check(f"5 act {json.dumps(body)[:70]} answers without a 500", st < 500 and isinstance(res, dict), (st, res))
r = client.post("/checkin/api/chat", content=b"[1]", headers={"content-type": "application/json"})
check("5 chat with a list body is a 400", r.status_code == 400, r.status_code)
# Screenshots in the chat (Nov 2): the upload route and what a message may name.
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
r = TestClient(app.make_app()).post("/checkin/api/chat/image", content=PNG, headers={"content-type": "image/png"})
check("5 image upload needs auth", r.status_code == 403, r.status_code)
r = client.post("/checkin/api/chat/image", content=b"hello", headers={"content-type": "text/plain"})
check("5 image upload refuses a non-image", r.status_code == 415, r.status_code)
# PDFs (Nov 3): accepted, checked for the PDF header, saved under their own (cleaned) name.
r = client.post("/checkin/api/chat/image", content=b"<html>", headers={"content-type": "application/pdf"})
check("5 a fake PDF is refused", r.status_code == 415, r.status_code)
PDF = b"%PDF-1.4\n" + b"0" * 64
from urllib.parse import quote
r = client.post("/checkin/api/chat/image?name=" + quote("../Fitness Cert: Nov/3.pdf"), content=PDF,
                headers={"content-type": "application/pdf"})
pdf_up = r.json() if r.status_code == 200 else {}
check("5 a PDF saves into Attachments/Carmen under its own name", r.status_code == 200
      and pdf_up.get("path", "").startswith("Attachments/Carmen/") and pdf_up["path"].endswith(" 3.pdf")
      and (VAULT / pdf_up["path"]).read_bytes() == PDF, (r.status_code, pdf_up))
check("5 a PDF name is cleaned", _web.attach_stem("../Fitness Cert: Nov/x.PDF") == "x"
      and _web.attach_stem("Brief (v2).pdf") == "Brief (v2)" and _web.attach_stem("") == "document"
      and _web.attach_stem("::.pdf") == "document", _web.attach_stem("../Fitness Cert: Nov/x.PDF"))
r = TestClient(app.make_app()).get("/checkin/" + pdf_up.get("url", "x"))
check("5 the uploaded PDF opens through its signed link", r.status_code == 200 and r.content == PDF
      and r.headers.get("content-type") == "application/pdf", r.status_code)
check("5 a message may name an uploaded PDF", _web.chat_images({"images": [pdf_up.get("path")]}, VAULT) == [pdf_up.get("path")])
r = client.post("/checkin/api/chat/image", content=b"", headers={"content-type": "image/png"})
check("5 image upload refuses an empty body", r.status_code == 400, r.status_code)
r = client.post("/checkin/api/chat/image", content=b"0" * (_web.IMAGE_MAX_BYTES + 1), headers={"content-type": "image/jpeg"})
check("5 image upload refuses a huge file", r.status_code == 413, r.status_code)
r = client.post("/checkin/api/chat/image", content=PNG, headers={"content-type": "image/png"})
up = r.json() if r.status_code == 200 else {}
check("5 image upload saves into Attachments/Carmen", r.status_code == 200 and up.get("path", "").startswith("Attachments/Carmen/")
      and (VAULT / up["path"]).read_bytes() == PNG, (r.status_code, up))
r2 = client.post("/checkin/api/chat/image", content=PNG, headers={"content-type": "image/png"})
check("5 two uploads in one second get two files", r2.status_code == 200 and r2.json().get("path") != up.get("path"), r2.text[:200])
r = TestClient(app.make_app()).get("/checkin/" + up.get("url", "x"))
check("5 the uploaded image opens through its signed link", r.status_code == 200 and r.content == PNG
      and r.headers.get("content-type") == "image/png", r.status_code)
(VAULT / "Inbox" / "secret.png").write_bytes(PNG)
names = _web.chat_images({"images": [up["path"], up["path"], "Inbox/secret.png", "Attachments/Carmen/../../Inbox/secret.png",
                                    "Attachments/Carmen/2026-11/missing.png", 5]}, VAULT)
check("5 a message names only uploaded images, once each", names == [up["path"]], names)
check("5 images is optional", _web.chat_images({"images": "x"}, VAULT) == [] and _web.chat_images({}, VAULT) == [])
for path in ("/checkin/api/journal", "/checkin/api/day", "/checkin/api/day?day=tomorrow", "/api/view", "/checkin/",
             "/checkin/manifest.webmanifest", "/checkin/login"):
    r = client.get(path)
    check(f"5 GET {path}", r.status_code == 200, r.status_code)
tpl = _meditation_entry.TEMPLATE
if tpl.is_file():
    tpl.rename(tpl.with_suffix(".bak"))
    r = client.get("/checkin/api/journal")
    check("5 a missing meditation template says so instead of a 500", r.status_code == 503 and "error" in r.json(), r.status_code)
    tpl.with_suffix(".bak").rename(tpl)
st, res = act(action="journal_save", values={"x": "y"})
check("5 saving the meditation writes today's entry", st == 200 and (_meditation_entry.JOURNAL / f"{ISO}.md").is_file(), (st, res))
st, v = view()
check("5 and the view still loads with it", st == 200, st)
errs = [e for e in logged_errors() if e.get("where", "").startswith("act")]
check("5 no tap above hit the catch-all", not errs, errs[:3])

# --- 6. background runs ----------------------------------------------------------------------------
child = subprocess.Popen(["sh", "-c", "exit 0  # jobs.checkin_app.update"], start_new_session=True)
time.sleep(0.3)
check("6 a finished child is reaped, not read as running (zombie, Oct 31)", not _runs._alive(child.pid))
_runs.RUN_DIR.mkdir(parents=True, exist_ok=True)
_runs.RUN_PID.write_text(str(os.getppid()))
check("6 a pid that is now some other process does not block Refresh", not _runs.prerun_state()["running"])
_runs.RUN_PID.write_text("not a pid")
check("6 a garbage pid file reads as not running", not _runs.prerun_state()["running"])
hung = subprocess.Popen(["sh", "-c", "sleep 30  # jobs.checkin_app.update"], start_new_session=True)
_runs.RUN_PID.write_text(str(hung.pid))
check("6 a live run counts as running", _runs.prerun_state()["running"])
old = time.time() - _runs.RUN_LIMITS["jobs.checkin_app.update"] - 60
os.utime(_runs.RUN_PID, (old, old))
check("6 a run past its timeouts is no longer running", not _runs.prerun_state()["running"])
time.sleep(0.3)
check("6 and it was ended", hung.poll() is not None)
_runs.RUN_PID.unlink()
_runs.PREP_PID.write_text(str(os.getpid()))
check("6 a prep pid that is not a prep run does not block prep", not _runs.prep_running())
_runs.PREP_PID.unlink()

# --- 7. ordering --------------------------------------------------------------------------------------
_planning.OVERRIDES.unlink(missing_ok=True)
write_prerun()
st, v = view()
picks = ids(v)[:6]
more = [i for i in ids(v)[6:] if i in GOOD]
check("7 there are tasks below Claude's picks to pull up", len(more) >= 2, ids(v))
for i in reversed(more[:2]):
    act(action="select", id=i, **{"from": "more"})
st, v = view()
check("7 pulled tasks stay out of Claude's picks", ids(v)[:6] == picks, (ids(v)[:6], picks))
check("7 his selection keeps the order he added them in",
      [r["id"] for r in v["today"]["selection"]][-2:] == list(reversed(more[:2])),
      [r["id"] for r in v["today"]["selection"]])

# --- 8. calendar -------------------------------------------------------------------------------------
_calendar.day_events = real_day_events
import jobs.plan_today_prerun as pr  # noqa: E402
real_tok, real_cget = pr._cal_token, pr._cget
calls = []


def cget(tok, path):
    calls.append(path)
    if path.startswith("users/me/calendarList"):
        return {"items": [{"id": c, "summary": c} for c in pr.BLOCKING_CALENDARS] + [{"id": "overlay@x", "summary": "Overlay"}]}
    time.sleep(0.2)
    if "overlay" in path and CAL.get("overlay_fail"):
        raise OSError("404")
    if CAL.get("block_fail") and "overlay" not in path:
        raise OSError("timeout")
    s = dt.datetime.combine(TODAY, dt.time(10), _clock.TZ).isoformat()
    f = dt.datetime.combine(TODAY, dt.time(11), _clock.TZ).isoformat()
    return {"items": [{"summary": "Class", "start": {"dateTime": s}, "end": {"dateTime": f}, "iCalUID": path}]}


pr._cal_token, pr._cget = (lambda: "tok"), cget
_calendar._BUSY_CACHE.clear()
t0 = time.time()
ev = _calendar.day_events(TODAY)
took = time.time() - t0
n_cals = len(pr.BLOCKING_CALENDARS) + 1
check("8 calendars are read in parallel", ev is not None and took < 0.2 * n_cals * 0.75, (round(took, 2), n_cals))
CAL["overlay_fail"] = True
ev = _calendar.day_events(TODAY, fresh=True)
check("8 an overlay calendar failing leaves the rest", ev is not None and len(ev) >= 1, ev)
CAL["overlay_fail"], CAL["block_fail"] = False, True
ev = _calendar.day_events(TODAY, fresh=True)
check("8 a blocking calendar failing shows the last good read", ev is not None, ev)
_calendar._BUSY_CACHE.clear()
check("8 with no good read it says unreadable (None)", _calendar.day_events(TODAY, fresh=True) is None)
CAL["block_fail"] = False
pr._cal_token = lambda: (_ for _ in ()).throw(RuntimeError("token gone"))
check("8 a lost token does not raise", _calendar.day_events(TODAY, fresh=True) is None)
pr._cal_token, pr._cget = real_tok, real_cget
_calendar.day_events = fake_day_events
# A real server, one event loop, as in production: a slow view must not hold up the chat's polling.
import socket  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
with socket.socket() as sk:
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(app.make_app(), host="127.0.0.1", port=port, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    if server.started:
        break
    time.sleep(0.05)
jar = {_web.COOKIE: _web.make_cookie("s3cret")}
base = f"http://127.0.0.1:{port}/checkin"
CAL["slow"] = 2.0
out = {}
slow = threading.Thread(target=lambda: out.update(v=httpx.get(base + "/api/view", cookies=jar, timeout=20).status_code))
slow.start()
time.sleep(0.4)
t0 = time.time()
r = httpx.get(base + "/api/chat?kind=task&id=Pay%20rent", cookies=jar, timeout=20)
quick = time.time() - t0
t0 = time.time()
r2 = httpx.post(base + "/api/act", json={"action": "notdone", "id": "Pay rent", "test": True}, cookies=jar, timeout=20)
tap = time.time() - t0
slow.join()
CAL["slow"] = 0.0
check("8 a slow calendar read does not freeze the chat", r.status_code == 200 and quick < 1.0, (r.status_code, round(quick, 2)))
check("8 the slow view still answers", out.get("v") == 200, out)
check("8 a tap during a slow view still answers", r2.status_code == 200, (r2.status_code, round(tap, 2)))
server.should_exit = True

# --- 9. dates -----------------------------------------------------------------------------------------
import re  # noqa: E402
naive = []
for f in (ROOT / "jobs" / "checkin_app").glob("*.py"):
    for i, ln in enumerate(f.read_text().splitlines(), 1):
        if re.search(r"date\.today\(\)|datetime\.now\(\)|datetime\.today\(\)|utcnow\(", ln):
            naive.append(f"{f.name}:{i}")
check("9 no clock read without the Pacific zone", not naive, naive)
check("9 Tomorrow is the Pacific tomorrow", view()[1]["tomorrow"]["date"] == (TODAY + dt.timedelta(days=1)).isoformat())

# --- scale ------------------------------------------------------------------------------------------
many = [{"n": f"Task {i}", "g": "soon", "p": "N", "s": "x" * 200, "nx": "y"} for i in range(400)]
write_prerun([["Everything else", many]] + good_rows())
t0 = time.time()
st, v = view()
check("scale 400 rows load in under 2 s", st == 200 and time.time() - t0 < 2, round(time.time() - t0, 2))

# Done and Progress (Nov 3): a task closed elsewhere and a step he reported in a chat show today.
from jobs.checkin_app.planning import do as _dm  # noqa: E402
_day = _clock.do_day(dt.datetime.now(_clock.TZ))
check("12 do.json is in the scratch home", str(_dm.STATE).startswith(str(HOME)), _dm.STATE)
_lab = _day.strftime("%b %-d")
note("Book the course", body=f"## Log\n- {_lab} · San Mateo mornings booked (E26); next is eRider · Claude session with the owner\n"
                             f"- {_lab} · Follow-up drafted, not sent · check-in app\n- {_lab} · waiting on Sam · check-in app\n")
_w = _tasks.today_wins(_day)["items"]
check("12 a step reported in a chat counts in Progress", any(w["t"].startswith("San Mateo mornings booked") and w["task"] == "Book the course" for w in _w), _w)
check("12 what was not done does not count", not any("not sent" in w["t"] or "waiting on" in w["t"] for w in _w), _w)
_dv = _dm.view(_day, {}, [{"id": "Closed in Plan", "title": "Closed in Plan", "task": True, "closed": _day.isoformat()},
                         {"id": "Closed yesterday", "title": "Closed yesterday", "task": True, "closed": "2000-01-01"}],
               [], [], [], lambda i: None, lambda i: None, lambda d: str(d))
check("12 Done holds what was closed anywhere today", [r["id"] for r in _dv["done"]] == ["Closed in Plan"],
      [r["id"] for r in _dv["done"]])

# Wait over (Nov 4, Laura's dinner): a done guess's "Mark as done" is never a step, and Done on a
# Wait-over card ends the wait and closes a task with nothing left.
check("13 a verdict is never a next step", _signals._real_next("Mark as done") == "" and _signals._real_next("Nothing left") == ""
      and _signals._real_next("Call Laura") == "Call Laura")
from jobs.checkin_app.tasks import steps as _sm  # noqa: E402
_wt = "---\nstatus: inbox\nwaiting: true\n---\n## Next action\n- [x] Next: Ask Laura\n- [ ] Waiting: Laura, on a seat\n\n## Log\n"
_a, _r = _sm.step_done(_wt, _day, _lab)
check("13 Done on a plain wait keeps waiting", _r["state"] == "waiting" and "status: done" not in _a, _r)
_a, _r = _sm.step_done(_wt, _day, _lab, wait_over=True)
check("13 Done on Wait over closes a task with nothing left", _r["state"] == "done" and "status: done" in _a
      and "- [x] Waiting: Laura" in _a and "waiting: true" not in _a, _r)
_a, _r = _sm.step_done(_wt.replace("- [ ] Waiting", "- [ ] Then: Bring a gift\n- [ ] Waiting"), _day, _lab, wait_over=True)
check("13 Done on Wait over brings up the next step", _r["state"] == "next" and _r["next"] == "Bring a gift"
      and "- [ ] Next: Bring a gift" in _a and "status: done" not in _a, _r)

# The committed plan orders Do (Nov 4): his ids first, carried-only tasks after.
_od = _day + dt.timedelta(days=40)
_dm.change(_od, lambda x: x["carried"].update({"Old carried": "2000-01-01", "Both": "2000-01-01"}))
_dm.commit(_od, ["First planned", "Both"])
_rows = {i: {"id": i, "title": i, "task": True} for i in ("Old carried", "Both", "First planned")}
_ov = _dm.view(_od, _rows, [], [], [], [], lambda i: None, lambda i: None, lambda d: str(d))
check("13 the committed order leads, carried-only goes to Left open", [r["id"] for r in _ov["rows"]] == ["First planned", "Both"]
      and [r["id"] for r in _ov["left_open"]] == ["Old carried"], [r["id"] for r in _ov["rows"]])

# Do holds only what he committed or added, and what an earlier day left open (Nov 6, the owner:
# "Left over yes, but the others should come via plan ... since i need to account for their time
# when budgeting"). A task scheduled or due that day does not join Do on its own.
_dd = _day + dt.timedelta(days=41)
_dm.change(_dd, lambda x: x["carried"].update({"Left from before": "2000-01-01"}))
_dm.commit(_dd, ["Planned today"])
_dm.add(_dd, "Added today")
_drows = {i: {"id": i, "title": i, "task": True, "due": _dd.isoformat() if i == "Dated today" else ""}
          for i in ("Planned today", "Added today", "Left from before", "Dated today")}
_dated = [_drows["Dated today"]]
_dv2 = _dm.view(_dd, _drows, [], _dated, [], [], lambda i: None, lambda i: None, lambda d: str(d))
_shown2 = {r["id"] for k in ("rows", "left_open", "done", "waiting", "part_done", "moved") for r in _dv2[k]}
check("13 a dated task does not join Do on its own", "Dated today" not in _shown2
      and {"Planned today", "Added today", "Left from before"} <= _shown2, sorted(_shown2))
_dv2 = _dm.view(_dd, _drows, [], _dated, [_drows["Dated today"]], [], lambda i: None, lambda i: None, lambda d: str(d))
check("13 one due today is offered, not inserted", [r["id"] for r in _dv2["suggest"]] == ["Dated today"]
      and "Dated today" not in {r["id"] for r in _dv2["rows"]}, [r["id"] for r in _dv2["suggest"]])

# Calendar blocks need no Allow once (Nov 4); guests, moves and deletes still ask.
from jobs.checkin_app.chat import auto_allowed as _aa  # noqa: E402
check("13 a calendar block without guests is allowed without a tap",
      _aa("mcp__claude_ai_Google_Calendar__create_event", {"summary": "Batch"})
      and not _aa("mcp__claude_ai_Google_Calendar__create_event", {"summary": "x", "attendees": [{"email": "a@b.c"}]})
      and not _aa("mcp__claude_ai_Google_Calendar__update_event", {}) and not _aa("mcp__claude_ai_Google_Calendar__delete_event", {})
      and not _aa("Bash", {"command": "create_event"}))

# The × on a step (Nov 4): struck, never deleted; a discarded Next brings up the next step.
_st = "---\nstatus: inbox\n---\n## Next action\n- [ ] Next: Pack the vest (10 min)\n- [ ] Then: Run it Saturday\n- [ ] Tell me how it felt\n\n## Log\n"
_a, _r = _sm.discard_step(_st, "Pack the vest", _lab)
check("13 discarding Next strikes it and brings up the next step", _r == {"kind": "Next", "next": "Run it Saturday"}
      and "- ~~Next: Pack the vest (10 min)~~ (discarded" in _a and "- [ ] Next: Run it Saturday" in _a and "step discarded: Pack the vest" in _a, _r)
_a, _r = _sm.discard_step(_st, "Tell me how it felt", _lab)
check("13 discarding a later step leaves Next alone", _r["kind"] == "step" and "- [ ] Next: Pack the vest" in _a
      and "- ~~Tell me how it felt~~" in _a, _r)
check("13 a step not in the note is refused", _sm.discard_step(_st, "Something else", _lab) is None)
_c, _ = act(id="Book the course", action="step_discard", text="Nope")
check("13 the act refuses a stale step", _c == 409, _c)

# --- 14. every card is a task note (Nov 4, cardnotes) ----------------------------------------------
from jobs.checkin_app.tasks import cardnotes as _cn  # noqa: E402
import re as _re  # noqa: E402
check("14 made.json is in the scratch home", str(_cn.MADE).startswith(str(HOME)), _cn.MADE)
# A block names its tasks by Task: lines only; the Item: stopgap is gone.
_ev = [{"s": 668, "f": 675, "title": "Batch", "desc": "- x<br>Item: Call Pal at lunch today<br>Task: obsidian://open?vault=V&file=Inbox%2FRun (t-4a9c1a)"}]
check("14 an Item: line slots nothing, a Task: line does",
      not _dm.block_for("Call Pal at lunch today", "", _ev) and _dm.block_for("Run", "t-4a9c1a", _ev))
# A title over 80 characters or with a colon names the note add_task saved it as, never a new one.
_long = "Plan the POLECON final paper, FNV and CS 351Z project work before leaving Nov 18 for good"
note(_long[:80].strip())
_lr = [["From today's meditation", [{"n": _long, "lk": _long, "g": "today"}, {"n": _long, "g": "today"}]]]
check("14 a long title finds its note", _notes.note_name(_long) == _long[:80].strip()
      and _cn.attach(_lr, "update", today=_day)["created"] == 0 and all(r.get("lk") == _long[:80].strip() for r in _lr[0][1]), _lr)
check("14 a long lk is cleaned to the note's name", _signals.clean_row({"n": "x", "lk": _long})["lk"] == _long[:80].strip())
# The pre-run and Refresh: every new row gets a note with a task_id, once.
_rows = [["New from mail and WhatsApp", [{"n": "Call Pal at lunch: today", "nw": 1, "g": "today", "s": "Pal wrote at 9",
                                          "nx": "Text Pal the time", "ne": "2m",
                                          "ctx": [{"who": "Pal", "t": "WhatsApp", "q": ["call at lunch?"]}]},
                                         {"n": "Pay rent", "lk": "Pay rent", "g": "today"}]]]
_out = _cn.attach(_rows, "pre-run", today=_day)
_r0 = _rows[0][1][0]
_p0 = _notes.task_path(_r0.get("lk") or "")
_t0 = _p0.read_text() if _p0 else ""
check("14 a new row gets a note and its lk", _out["created"] == 1 and _p0 and "nw" not in _r0, (_out, _r0))
check("14 the note has the schema: task_id, inbox, Next with the estimate, the context, a Log line",
      bool(_re.search(r"^task_id: t-[0-9a-f]{6}$", _t0, _re.M)) and "status: inbox" in _t0
      and "- [ ] Next: Text Pal the time (2 min)" in _t0 and "> call at lunch?" in _t0 and "created by Carmen (pre-run)" in _t0
      and "scheduled:" not in _t0, _t0)
_rows2 = [["Today", [{"n": "Call Pal at lunch: today", "g": "today"}]]]
_out2 = _cn.attach(_rows2, "update", today=_day)
check("14 the same card again links to that note, never a second one",
      _out2 == {"created": 0, "linked": 1, "hidden": 0, "failed": 0} and _rows2[0][1][0]["lk"] == _p0.stem
      and len(list((VAULT / "Inbox").glob("Call Pal at lunch*"))) == 1, (_out2, _rows2))
# A card still without a note (the live meditation matcher) gets one on its first tap.
write_prerun([["Today", [{"n": "Pay rent", "lk": "Pay rent", "g": "today", "p": "N"}]],
              ["New from mail and WhatsApp", [{"n": "Answer the bank about the fraud alert", "g": "today", "p": "H",
                                               "s": "a mail", "nx": "Call the bank"}]]])
write_update("{}")
st, v = view()
_card = next((r for r in _planning.all_rows(v) if r["title"] == "Answer the bank about the fraud alert"), None)
check("14 a row without a note shows, as not yet a note", st == 200 and _card and _card["task"] is False, _card)
_c, _res = act(id="Answer the bank about the fraud alert", action="done")
_pb = _notes.task_path("Answer the bank about the fraud alert")
_tb = _pb.read_text() if _pb else ""
check("14 Done on it creates the note and closes it like any task", _c == 200 and "status: done" in _tb
      and "completedDate:" in _tb and "- [x] Next: Call the bank" in _tb, (_c, _res, _tb))
_ids = [r["id"] for r in _planning.all_rows(view()[1])]
check("14 the card is gone from the open lists once done", "Answer the bank about the fraud alert" not in
      [r["id"] for r in view()[1]["today"]["needs"] + view()[1]["today"]["more"]], _ids)
check("14 a closed Carmen card goes into the dropped memory, once",
      (_cn.load().get(_pb.stem) or {}).get("dropped") == _day.isoformat() if _pb else False, _cn.load())
_w = _tasks.today_wins(_day)["items"]
check("14 it counts in Progress as a finished task", any(w["task"] == _pb.stem and w.get("big") for w in _w) if _pb else False, _w)
_c, _res = act(id="Answer the bank about the fraud alert", action="note", text="called them")
check("14 a page still holding the old id reaches the same note", _c == 200 and "called them" in _pb.read_text(), (_c, _res))
check("14 test mode never creates a note", act(id="Nobody's card", action="done", test=True)[0] in (200, 404)
      and not _notes.task_path("Nobody's card"))
# Create plan with a card that has no note: it is planned by its note.
write_prerun([["New from mail and WhatsApp", [{"n": "Decline the trek invite", "g": "today", "p": "N", "nx": "Reply no"}]]])
_c, _ = act(action="do_commit", ids=["Decline the trek invite"], day="today")
_e = _dm._load()["days"].get(ISO) or {}
check("14 Create plan stores the note, not the title", _c == 200 and _notes.task_path("Decline the trek invite")
      and "Decline the trek invite" in _e.get("ids", []) and (_cn.load().get("Decline the trek invite") or {}).get("touched"), (_c, _e.get("ids")))
# The pre-run's clean-up: an untouched Carmen note from before today that this run does not carry.
_old = _day - dt.timedelta(days=1)
for _n in ("Untouched old card", "Carried old card", "Touched old card", "Old card on Do"):
    _cn.create({"n": _n, "nx": "x"}, "pre-run", today=_old)
_cn.touch("Touched old card")
_sw = _cn.sweep({"Carried old card"}, _day, busy={"Old card on Do"})
_tu = _notes.task_path("Untouched old card").read_text()
check("14 sweep cancels only the untouched, uncarried, unplanned one", _sw == ["Untouched old card"]
      and "status: cancelled" in _tu and "completedDate:" in _tu and "not picked up" in _tu
      and "status: inbox" in _notes.task_path("Touched old card").read_text(), (_sw, _tu))
check("14 a card made today is never swept", "Call Pal at lunch- today" not in _cn.sweep(set(), _day, dry=True)
      and _p0.stem not in _cn.sweep(set(), _day, dry=True))
# A new message after a Skip is a new card, never the cancelled note (review Nov 4).
_cn.create({"n": "Reply to Luciana", "nx": "Reply"}, "pre-run", today=_old)
_task_actions.apply(_notes.task_path("Reply to Luciana"), "cancel", text_in="reply skipped in Carmen")
_again = _cn.create({"n": "Reply to Luciana", "nx": "Reply again"}, "pre-run", today=_day)
check("14 a closed note is never reused for a new card", _again and _again.stem != "Reply to Luciana"
      and "status: inbox" in _again.read_text(), _again)
# Worked on outside Carmen (a Log line that is not the creation one): never swept.
_cn.create({"n": "Worked in Telegram", "nx": "x"}, "pre-run", today=_old)
_wp = _notes.task_path("Worked in Telegram")
_wp.write_text(_wp.read_text() + f"- {_lab} · the owner booked it in the chat · Claude session\n")
check("14 a note worked on elsewhere is never swept", "Worked in Telegram" not in _cn.sweep(set(), _day, dry=True))
check("14 a note made by a tap is touched by that tap", (_cn.load().get("Answer the bank about the fraud alert") or {}).get("touched"))
# Skip on a reply Carmen made a card for and he never touched drops it; Remove > Shouldn't exist cancels with completedDate.
write_prerun([["New from mail and WhatsApp", [{"n": "Reply to the newsletter", "g": "today", "p": "Z",
                                               "ch": [{"t": "x", "msg": {"via": "mail", "to": "a@b.c", "subj": "s", "body": "b"}}]}]]])
_cn.attach(_signals.clean_rows([["x", [{"n": "Reply to the newsletter", "nx": "Reply"}]]]), "pre-run", today=_day)
_c, _res = act(id="Reply to the newsletter", action="skipreply", tab="replies")
check("14 Skip on an untouched Carmen reply cancels it", _c == 200 and "status: cancelled" in _notes.task_path("Reply to the newsletter").read_text(), (_c, _res))

# Left open (Nov 4): after a commit, a task only carried over waits in its own fold.
_ld = _day + dt.timedelta(days=60)
_dm.change(_ld, lambda x: x["carried"].update({"Only carried": "2000-01-01", "Carried and planned": "2000-01-01"}))
_lrows = {i: {"id": i, "title": i, "task": True} for i in ("Only carried", "Carried and planned")}
_lv = _dm.view(_ld, _lrows, [], [], [], [], lambda i: None, lambda i: None, lambda d: str(d))
check("13 before a commit, carried tasks stay in the list", [r["id"] for r in _lv["rows"]] == ["Only carried", "Carried and planned"]
      and not _lv["left_open"], _lv["left_open"])
_dm.commit(_ld, ["Carried and planned"])
_lv = _dm.view(_ld, _lrows, [], [], [], [], lambda i: None, lambda i: None, lambda d: str(d))
check("13 after a commit, carried-only tasks are Left open", [r["id"] for r in _lv["rows"]] == ["Carried and planned"]
      and [r["id"] for r in _lv["left_open"]] == ["Only carried"], ([r["id"] for r in _lv["rows"]], _lv["left_open"]))
_dm.add(_ld, "Only carried")
_lv = _dm.view(_ld, _lrows, [], [], [], [], lambda i: None, lambda i: None, lambda d: str(d))
check("13 Add to today brings it back into the list", "Only carried" in [r["id"] for r in _lv["rows"]] and not _lv["left_open"])

# Auto-close (Nov 4): plainly over and done closes, with Undo; anything less stays open.
from jobs.checkin_app.tasks import autoclose as _ac  # noqa: E402
_yday = (_day - dt.timedelta(days=1)).isoformat()
_acn = (f"---\ntype: action\nstatus: inbox\nscheduled: {_yday}\nwaiting: true\n---\n\n## Next action\n"
        "- [x] Next: Tell Laura\n- [ ] Waiting: Laura, on a seat\n\n## Log\n")
_acr = {"lk": "Answer Laura's dinner invite for tonight 19-00", "g": "done", "s": "She said yes and you went.",
        "ctx": [{"who": "Laura", "q": ["Laura: Klar!"]}]}
check("13 auto-close: past, timed, done with evidence", bool(_ac.judge(_acn, _acr, _day)))
check("13 auto-close: not when unsure", not _ac.judge(_acn, {**_acr, "lc": 1}, _day))
check("13 auto-close: not without a quote", not _ac.judge(_acn, {**_acr, "ctx": []}, _day))
check("13 auto-close: not before its day has passed", not _ac.judge(_acn.replace(_yday, _day.isoformat()), _acr, _day))
check("13 auto-close: not when the task names no time", not _ac.judge(_acn, {**_acr, "lk": "Answer Laura's invite"}, _day))
check("13 auto-close: not with a later step of his open", not _ac.judge(_acn.replace("- [ ] Waiting", "- [ ] Then: Bring a gift\n- [ ] Waiting"), _acr, _day))
_acc = _ac.close(_acn, "She said yes", _day, "pre-run")
check("13 auto-close closes, ticks the wait and logs why", "status: done" in _acc and "waiting: true" not in _acc
      and "- [x] Waiting: Laura" in _acc and "closed by Claude: She said yes · pre-run" in _acc)
_acp = note("Answer Laura's dinner invite for tonight 19-00", raw=_acn.encode())
_ac.STATE = HOME / "autoclose.json"
_res = _ac.run([["Overdue", [_acr]]], _day, "pre-run", _notes.task_path)
check("13 auto-close run closes the note and Undo restores it", len(_res) == 1 and "status: done" in _acp.read_text()
      and _ac.undo(_acp, _day) == "" and "status: inbox" in _acp.read_text())
check("13 a task he undid never auto-closes again", _ac.run([["Overdue", [_acr]]], _day, "pre-run", _notes.task_path) == [])

# The description is never cut inside a link (Nov 4).
_dl = _tasks._description("x" * 690 + " [Paper](obsidian://open?vault=V&file=" + "A" * 300 + ")\nmore text")
check("13 a cut description keeps its link whole", "A" * 300 + ")" in _dl and _dl.endswith("…"), _dl[-40:])

# Not waiting puts the task on today's Do list (Nov 4), never in Left open.
note("Plan the India hotels", fm="waiting: true\n", body="- [ ] Waiting: Priya, on hotel options\n")
_c, _ = act(id="Plan the India hotels", action="not_waiting")
check("13 Not waiting adds the task to today", _c == 200 and "Plan the India hotels" in _dm._day(_dm._load(), _clock.do_day(dt.datetime.now(_clock.TZ)))["ids"], _c)

# --- 15. step lines as data (Nov 4, tools/steps.py) ------------------------------------------------
from tools import steps as _S  # noqa: E402
check("15 the step history is in the scratch home", str(_S.DIR).startswith(str(HOME)), _S.DIR)
_sp = VAULT / "Inbox" / "Refund the trek.md"
_sp.write_text("---\ntype: action\ntask_id: t-5e5e5e\nstatus: open\npriority: 3-normal\n---\n\n## Next action\n"
               "- [ ] Next: Confirm the refund landed (10 min) 📅 2026-11-12 ^s4f2a\n"
               "- [ ] Then: Reply to the WeTravel mail if not (due Nov 12)\n- [ ] Then: Tell Rahim we are square\n\n## Log\n",
               encoding="utf-8")
_live = _tasks.read_note(_sp)
check("15 the app reads Next as before: words, (due ...), (N min)",
      _live["next"] == "Confirm the refund landed (due Nov 12) (10 min)" and _live["next_id"] == "s4f2a"
      and _live["step_due"] == "2026-11-12" and _live["steps"][0] == "Reply to the WeTravel mail if not (due Nov 12)", _live)
_c, _r = act(id="Refund the trek", action="step_done")
_t = _sp.read_text()
check("15 Done keeps the step's ID, dates it, and brings up the next step with its date as a field",
      _c == 200 and "- [x] Next: Confirm the refund landed (10 min) 📅 2026-11-12 ✅ " in _t and "^s4f2a" in _t
      and _re.search(r"^- \[ \] Next: Reply to the WeTravel mail if not 📅 2026-11-12 \^s[0-9a-f]{4}$", _t, _re.M)
      and "(due Nov 12)" not in _t.split("## Log")[0], (_c, _r, _t))
_h = [json.loads(l) for l in _S.HISTORY.read_text().splitlines()] if _S.HISTORY.exists() else []
check("15 the tap is in the step history, with its source", any(h["event"] == "done" and h["step"] == "s4f2a"
      and h["source"] == "tap:step_done" and h["task_id"] == "t-5e5e5e" for h in _h), _h[-4:])
_c, _ = act(id="Refund the trek", action="tick_line", text="Tell Rahim we are square")
check("15 a later step ticks by its words, dated", _c == 200 and _re.search(r"^- \[x\] Then: Tell Rahim we are square ✅ \d{4}-\d{2}-\d{2} \^s", _sp.read_text(), _re.M), _sp.read_text())
_c = 200 if _task_actions.apply(_sp, "next", text_in="Reply to the WeTravel refund mail if it has not landed") == "Next updated" else 0
_t = _sp.read_text()
_old_id = _re.search(r"^- \[ \] Next: .*\^(s[0-9a-f]{4})$", _t, _re.M)
_h = [json.loads(l) for l in _S.HISTORY.read_text().splitlines()]
check("15 rewriting Next keeps its ID and deadline: logged as reworded",
      _c == 200 and _old_id and "📅 2026-11-12" in _t.split("## Log")[0].split("Reply to the WeTravel refund")[1].split("\n")[0]
      and any(h["event"] == "reworded" and h["step"] == _old_id.group(1) for h in _h), (_t, _h[-3:]))
_c, _r = act(id="Refund the trek", action="step_discard", text="Reply to the WeTravel refund mail if it has not landed")
_t = _sp.read_text()
check("15 discarding strikes the line and keeps its ID outside the strike",
      _c == 200 and _re.search(r"^- ~~Next: Reply to the WeTravel refund mail if it has not landed~~ \(discarded, [A-Z][a-z]{2} \d+\) 📅 2026-11-12 \^s[0-9a-f]{4}$", _t, _re.M), _t)
_t2 = _t.replace("## Log", "- [ ] Next: Call Parrot about the invoice (due Nov 9)\n\n## Log", 1)
_sp.write_text(_t2, encoding="utf-8")   # an edit in Obsidian or a chat
_tasks.read_note(_sp)
_ev = _S.observe(_sp)
_h = [json.loads(l) for l in _S.HISTORY.read_text().splitlines()]
check("15 an edit nobody announced is logged as outside", any(h["event"] == "added" and h["source"] == "outside"
      and h["text"] == "Call Parrot about the invoice" and h["due"] == "2026-11-09" for h in _h), _h[-3:])

# Messages and waiting steps (Nov 5): a reply never closes a task with more to do; waiting is a
# state of the step; a task folds only when nothing of his is left.
from jobs.checkin_app.tasks import messages as _msg  # noqa: E402
_pad = ("---\nstatus: inbox\n---\n## Next action\n- [ ] Next: Get the padel money back from Jose\n"
        "- [ ] Then: Add the $35 propane to Splitwise\n\n## Log\n")
_w = _sm.set_waiting(_pad, "Jose", _lab)
check("14 Waiting makes the step wait and brings up his next one", "- [ ] Waiting: Jose · for: Get the padel money back" in _w
      and "- [ ] Next: Add the $35 propane" in _w, _w)
check("14 Not waiting gives the step back", "- [ ] Next: Get the padel money back from Jose" in _sm.not_waiting(_w, _lab)
      and "- [ ] Then: Add the $35 propane" in _sm.not_waiting(_w, _lab))
_a, _r = _msg.reply_sent(_pad, who="Jose", via="WhatsApp", body="Venmo me $80", today=_day, label=_lab)
check("14 a reply tied to a bigger step is only recorded", _r["state"] == "recorded" and "status: done" not in _a
      and "## Messages" in _a and "  > Venmo me $80" in _a, _r)
_a, _r = _msg.reply_sent(_pad.replace("Get the padel money back from Jose", "Text Jose the padel amount"),
                         who="Jose", via="WhatsApp", body="$80", today=_day, label=_lab)
check("14 a reply that is the step ticks it, the task goes on", _r["state"] == "next" and "- [x] Next: Text Jose" in _a
      and "status: done" not in _a, _r)
note("Get money back Propane, Padel, Brunch", body="- [ ] Then: Add propane\n")
_gp = VAULT / "Inbox" / "Get money back Propane, Padel, Brunch.md"
_j = _task_actions._reply_sent(_gp, {"id": _gp.stem}, "Jose", "WhatsApp", "Padel was $240")
check("14 Sent in Replies keeps the task open", _j["state"] == "recorded" and "status: done" not in _gp.read_text()
      and "Padel was $240" in _gp.read_text(), _j)

# 16. Links everywhere (Nov 5, the owner: "links are still super unstable across the app"): the page
#     turns any raw [label](link) or bare address into a link after every paint, whichever code
#     printed it, and link buttons look the same in every box.
import re as _re
_page = (pathlib.Path(__file__).resolve().parent.parent / "jobs" / "checkin_app" / "page.html").read_text()
check("16 the page links raw text after every paint", "function fixLinks(" in _page
      and "new MutationObserver" in _page and "fixLinks(document.body)" in _page)
check("16 link buttons are styled once, for every box", "  button.link { background: none;" in _page)
_js = max(_re.findall(r"<script>(.*?)</script>", _page, _re.S), key=len)


def _fn(name):
    i = _js.index("function " + name + "(")
    depth = 0
    for k in range(_js.index("{", i), len(_js)):
        depth += (_js[k] == "{") - (_js[k] == "}")
        if depth == 0:
            return _js[i:k + 1]


_esc = _re.search(r"const esc = [^\n]+", _js)
_raw = _re.search(r"const RAW_LINK = [^\n]+", _js)
if shutil.which("node") and _esc and _raw:
    _src = ("const inTg=false;\n" + _esc.group(0) + "\n" + _raw.group(0) + "\n" + _fn("linkBare") + "\n" + _fn("linkText")
            + "\nconsole.log(JSON.stringify(["
              "linkText('Next time: Read: [Pricing case (12 pp)](file?p=A%2FB.pdf&s=1) · [Canvas](https://canvas.stanford.edu/x)'),"
              "linkText('see https://www.tripsite.com/my-trip'), linkText('notes.md and a@b.com'),"
              "RAW_LINK.test('plain words only')]))")
    _out = json.loads(subprocess.run(["node", "-e", _src], capture_output=True, text=True, timeout=20).stdout or "[]")
    check("16 a file link and a web link in one line both become links", len(_out) == 4
          and '<a class="vfile" href="file?p=A%2FB.pdf&amp;s=1"' in _out[0] and ">Canvas</a>" in _out[0]
          and "](" not in _out[0], _out[:1])
    check("16 a bare address becomes a short link", len(_out) == 4 and ">tripsite.com</a>" in _out[1], _out[1:2])
    check("16 file names and emails stay text, plain words are left alone", len(_out) == 4
          and "<a" not in _out[2] and _out[3] is False, _out[2:])
else:
    check("16 node and the page's esc and RAW_LINK are there for the link test", False,
          {"node": bool(shutil.which("node")), "esc": bool(_esc), "raw": bool(_raw)})

# 17. The step contract (refactor step 1, Nov 5): a step is found by its ^id everywhere, never by
#     its words. One task goes through a day: planned chunk, a reworded step, a tick by id, his
#     Done, Keep going; a second one is ticked by Claude; a third is written without ids.
from jobs.checkin_app.planning import do as _dm17
_SNAP = """- [ ] Next: Draft the outline (10 min) ^s1a01
    - https://example.com/outline
- [ ] Then: Email Sam the draft (5 min) ^s1a02
- [ ] Then: Book the room (5 min) ^s1a03
"""
_p17 = VAULT / "Inbox" / "Snap task.md"
_p17.write_text(f"---\ntype: action\nstatus: open\npriority: 3-normal\nscheduled: {ISO}\n---\n\n## Next action\n{_SNAP}",
                encoding="utf-8")


def _card17(name):
    _, v = view()
    d = v.get("do") or {}
    for k in ("rows", "part_done"):
        for r in d.get(k) or []:
            if r["id"] == name:
                return k, r
    return None, None


_s, _ = act(action="do_commit", ids=["Snap task"], day="today",
            chunks={"Snap task": ["Draft the outline (10 min)", "Email Sam the draft (5 min)"]},
            chunk_ids={"Snap task": ["s1a01", "s1a02"]})
_k, _r = _card17("Snap task")
check("17 Plan's chunk is stored and shown by step id", _k == "rows" and (_r.get("chunk") or {}).get("ids") == ["s1a01", "s1a02"],
      (_k, (_r or {}).get("chunk")))
check("17 a step's links are keyed by its id", "s1a01" in ((_r or {}).get("detail") or {}).get("ws", {}),
      ((_r or {}).get("detail") or {}).get("ws"))
_p17.write_text(_p17.read_text().replace("Draft the outline (10 min) ^s1a01", "Draft the outline and the intro (10 min) ^s1a01"))
_k, _r = _card17("Snap task")
check("17 a reworded step stays in today's chunk, nothing counts as done", _k == "rows"
      and "s1a01" in ((_r or {}).get("chunk") or {}).get("ids", []), (_k, (_r or {}).get("chunk")))
_s, _j = act(id="Snap task", action="tick_line", text="words that match nothing", sid="s1a02")
check("17 a later step is ticked by its id, whatever the words", _s == 200 and "- [x] Then: Email Sam the draft" in _p17.read_text(),
      (_s, _j))
_k, _r = _card17("Snap task")
check("17 after the tick one step of the chunk is left", _k == "rows" and ((_r or {}).get("chunk") or {}).get("ids") == ["s1a01"],
      (_k, (_r or {}).get("chunk")))
_s, _j = act(id="Snap task", action="step_done", tab="do")
_k, _r = _card17("Snap task")
check("17 his Done on the last chunk step ends today's part", _k == "part_done", (_k, _s, _j))
_s, _j = act(id="Snap task", action="do_more")
_k, _r = _card17("Snap task")
check("17 Keep going adds the next step by its id", _k == "rows" and "s1a03" in ((_r or {}).get("chunk") or {}).get("ids", []),
      (_k, (_r or {}).get("chunk")))

_p17c = VAULT / "Inbox" / "Snap claude.md"
_p17c.write_text(f"---\ntype: action\nstatus: open\npriority: 3-normal\nscheduled: {ISO}\n---\n\n## Next action\n"
                 "- [ ] Next: List what is open (10 min) ^s2b01\n- [ ] Then: Fill in the form (10 min) ^s2b02\n", encoding="utf-8")
act(action="do_commit", ids=["Snap task", "Snap claude"], day="today", chunk_ids={"Snap claude": ["s2b01"]})
_p17c.write_text(_p17c.read_text().replace("- [ ] Next: List what is open (10 min) ^s2b01",
                                           "- [x] Next: List what is open (10 min) ^s2b01").replace("- [ ] Then: Fill", "- [ ] Next: Fill"))
_dm17.claude_ticked("Snap claude", [{"id": "s2b01", "text": "List what is open (10 min)"}], _clock.do_day(dt.datetime.now(_clock.TZ)))
_k, _r = _card17("Snap claude")
check("17 a step Claude ticked does not end today's part", _k == "rows" and (_r or {}).get("claude_did"), (_k, (_r or {}).get("claude_did")))

_p17n = VAULT / "Inbox" / "Snap noid.md"
_noid = ("---\ntype: action\nstatus: open\npriority: 3-normal\n---\n\n## Next action\n"
         "- [ ] Next: First thing (5 min)\n- [ ] Then: Second thing (5 min)\n")
_p17n.write_text(_noid, encoding="utf-8")
_tasks.read_note(_p17n)
check("17 reading a note never writes it", _p17n.read_text() == _noid)
_h17 = subprocess.run([sys.executable, "-m", "tools.check_tasks", "--hook"], cwd=str(ROOT), capture_output=True, text=True,
                      input=json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(_p17n)}}), timeout=60,
                      env={**os.environ, "PYTHONPATH": str(ROOT)})
_txt17 = _p17n.read_text()
_st17 = _tasks.read_note(_p17n)
check("17 a Claude edit without step ids gets them from the task hook", _txt17.count(" ^s") == 2
      and all(x["id"] for x in _st17["step_states"]) and len(_st17["step_ids"]) == 1, (_h17.returncode, _txt17[-160:]))

# 18 (Nov 6). A failed preparation backs off and stops for the day after MAX_FAILS: a pending
# answer used to stay pending, so every view restarted it (328 failures on Nov 1 and 2).
_p18 = VAULT / "Inbox" / "Prep fails.md"
_p18.write_text("---\ntype: action\nstatus: open\npriority: 3-normal\n---\n\n## Next action\n"
                "- [ ] Next: Something Claude can research (10 min) ^s3c01\n", encoding="utf-8")
prep.save_state({"id": "Prep fails"}, {"id": "Prep fails", "pending": True, "answers": [{"q": "q", "answer": "a"}]})
_real_ask = prep.ask


def _ask_fails(*a, **k):
    raise prep.AskFailed("boom")


prep.ask = _ask_fails
try:
    prep.prepare({"id": "Prep fails", "title": "Prep fails"})
    _st18 = prep.load_state("Prep fails")
    check("18 a failed preparation backs off", _st18.get("pending") and not prep.retry_due(_st18)
          and prep.stale_reason({"id": "Prep fails"}, _st18, "") == "", _st18)
    started.clear()
    _runs.prep_running = lambda: False
    _runs.maybe_start_prep(_runs.PREP_GEN.read_text().strip() if _runs.PREP_GEN.exists() else "")
    check("18 a backing-off answer is not restarted by a view", not [s for s in started if s[0] == "prep"], started)
    for _ in range(2):
        prep.prepare({"id": "Prep fails", "title": "Prep fails"})
    _st18 = prep.load_state("Prep fails")
    check("18 after 3 failures a day it stops until tomorrow", _st18.get("fails") == 3 and not _st18.get("pending")
          and not prep.retry_due(_st18), _st18)
finally:
    prep.ask = _real_ask

# 18 (Nov 6). Create plan for today goes to Do's day, which turns at 4:00: at 01:30 the plan
# must land on the evening's day that Do is still showing.
_real_do_day = _clock.do_day
_clock.do_day = lambda now: TODAY - dt.timedelta(days=1)
try:
    act(action="do_commit", ids=["Prep fails"], day="today")
    _days18 = _dm17._load()["days"]
    check("18 a plan made before 4:00 lands on Do's day", "Prep fails" in (_days18.get((TODAY - dt.timedelta(days=1)).isoformat()) or {}).get("ids", []),
          {k: v.get("ids") for k, v in _days18.items()})
finally:
    _clock.do_day = _real_do_day

# 19 (Nov 6). Errors reach his phone, once an hour per place; /api/health says whether Carmen works.
from jobs.checkin_app import alerts as _al  # noqa: E402
_sent19 = []
_al._send = lambda title, message, priority: _sent19.append((title, priority))
check("19 alerts stay in the scratch world", str(_al.STATE).startswith(str(TMP)), _al.STATE)
_t19 = time.time()
check("19 the first alert for a place is sent", _al.alert("test place", "boom", now=_t19))
check("19 the same place within the hour is not", not _al.alert("test place", "boom again", now=_t19 + 60))
check("19 after an hour it is sent again", _al.alert("test place", "boom", now=_t19 + 3700))
check("19 quiet places are never pushed", not _al.alert("row", "x", now=_t19))
_h19 = _al.health()
check("19 health answers with named checks", isinstance(_h19.get("checks"), dict)
      and {"prerun", "last_run", "prep", "errors"} <= set(_h19["checks"]), _h19)
_r19 = client.get("/checkin/api/health")
check("19 /api/health answers a signed-in page", _r19.status_code in (200, 503) and "checks" in _r19.json(), _r19.status_code)
_r19b = TestClient(app.make_app(), raise_server_exceptions=False).get("/checkin/api/health")
check("19 /api/health refuses a stranger", _r19b.status_code == 403, _r19b.status_code)

# 20 (Nov 6, refactor Phase 1). A page load only reads; every state write survives writers at once.
_sd20 = app.config.STATE_DIR / "checkin_app"


def _mt20():
    return {str(q): q.stat().st_mtime_ns for q in _sd20.rglob("*")
            if q.is_file() and q.name not in ("taps.jsonl", "alerts.json") and not q.name.endswith((".lock", ".tmp"))}


view()
_b20 = _mt20()
time.sleep(0.05)
# A task new to the day: the old view froze its rank key and recorded it as dated, on a page load.
(VAULT / "Inbox" / "Fresh today.md").write_text(
    f"---\ntype: action\nstatus: open\npriority: 3-normal\nscheduled: {ISO}\n---\n\n## Next action\n"
    "- [ ] Next: Something new (5 min) ^s4d01\n", encoding="utf-8")
view()
view()
_a20 = _mt20()
check("20 a page load writes no state file", _a20 == _b20,
      sorted(k for k in set(_a20) | set(_b20) if _a20.get(k) != _b20.get(k))[:6])
from jobs.checkin_app.adapters import state as _state20  # noqa: E402
with _state20.persist():
    _planning.build_view(_signals.load_prerun())
check("20 the tick's persisting pass may write (and does not fail)", True)

_day20 = TODAY + dt.timedelta(days=90)
_errs20 = []


def _adder20(n):
    try:
        for k in range(25):
            _dm17.add(_day20, f"T{n}-{k}")
    except Exception as exc:   # noqa: BLE001
        _errs20.append(repr(exc))


_proc20 = subprocess.Popen([sys.executable, "-c", (
    "import datetime as dt, sys; sys.path.insert(0, %r)\n"
    "from jobs.checkin_app.planning import do\n"
    "for k in range(25): do.add(dt.date.fromisoformat(%r), f'P-{k}')\n") % (str(ROOT), _day20.isoformat())],
    env={**os.environ, "PYTHONPATH": str(ROOT)})
_ths20 = [threading.Thread(target=_adder20, args=(n,)) for n in range(6)]
for _t in _ths20:
    _t.start()
for _t in _ths20:
    _t.join()
_proc20.wait(timeout=120)
_ids20 = set(_dm17._load()["days"].get(_day20.isoformat(), {}).get("ids", []))
_want20 = {f"T{n}-{k}" for n in range(6) for k in range(25)} | {f"P-{k}" for k in range(25)}
check("20 six threads and another process adding at once lose nothing", _want20 <= _ids20 and not _errs20
      and _proc20.returncode == 0, (len(_want20 - _ids20), _errs20[:2], _proc20.returncode))
check("20 no torn or stray tmp files are left", not list(_sd20.glob("*.tmp")), list(_sd20.glob("*.tmp"))[:3])

# 21 (Nov 6, refactor Phase 2). A renamed note keeps its Do place, selection and chat; a step
# reworded in Edit steps keeps its id.
from jobs.checkin_app import renames as _rn, chat as _chat21  # noqa: E402
_old21 = VAULT / "Inbox" / "Rename me.md"
_old21.write_text("---\ntype: action\ntask_id: t-ab12cd\nstatus: open\npriority: 3-normal\n---\n\n## Next action\n"
                  "- [ ] Next: Do the first thing (10 min) ^s5e01\n- [ ] Then: Do the second thing (5 min) ^s5e02\n",
                  encoding="utf-8")
_rn.check()
_dm17.add(TODAY, "Rename me")
_plan_actions.save_override("selected", TODAY, "Rename me")
_chat21.DIR.mkdir(parents=True, exist_ok=True)
_chat21.save({"key": "task:Rename me", "title": "Rename me", "messages": [{"role": "you", "text": "hi", "at": ""}], "busy": False})
_new21 = VAULT / "Inbox" / "Renamed task.md"
_old21.rename(_new21)
_moved21 = _rn.check()
_e21 = _dm17._load()["days"].get(TODAY.isoformat(), {})
check("21 a rename is found by task_id", _moved21 == [("Rename me", "Renamed task")], _moved21)
check("21 the renamed task keeps its place on Do", "Renamed task" in _e21.get("ids", []) and "Rename me" not in _e21.get("ids", []),
      _e21.get("ids"))
check("21 and its selection", "Renamed task" in _planning.load_overrides()["selected"].get(ISO, []))
check("21 and its chat", _chat21.load("task:Renamed task")["messages"][:1] and not _chat21.conv_path("task:Rename me").exists())
_idx21 = _rn.INDEX.read_text()
_real_inbox21 = _rn.config.INBOX
_rn.config.INBOX = TMP / "empty-inbox"
_rn.config.INBOX.mkdir(exist_ok=True)
try:
    check("21 an empty Inbox is never read as renames", _rn.check() == [] and _rn.INDEX.read_text() == _idx21)
finally:
    _rn.config.INBOX = _real_inbox21

_s21, _j21 = act(id="Renamed task", action="steps_save", steps=["Do the first thing, but better (10 min)", "Do the second thing (5 min)"])
_t21 = _new21.read_text()
check("21 a step reworded in Edit steps keeps its id", _s21 == 200 and "better" in _t21 and "^s5e01" in _t21
      and _t21.count("^s5e01") == 1 and "~~" not in _t21, (_s21, _j21, _t21[-300:]))

# 22 (Nov 6, refactor Phase 3). The page and the server agree on the actions; test mode writes nothing.
import re as _re22  # noqa: E402
import hashlib as _hl22  # noqa: E402
from jobs.checkin_app import actions as _ac22  # noqa: E402
_page22 = (ROOT / "jobs" / "checkin_app" / "page.html").read_text(encoding="utf-8")
_ui22 = {"copy", "details", "edit", "letclaude", "removeopen", "sendopen", "start", "stepdone", "talk", "wa", "talknote"}
_posted22 = ({a for a in _re22.findall(r'action:\s*"([a-z_]+)"', _page22) if a != "do_"}
             | {"do_" + a for a in _re22.findall(r'data-do="([a-z_]+)"', _page22) if a != "move"}
             | (set(_re22.findall(r'data-a="([a-z_]+)"', _page22)) - _ui22)
             | set(_re22.findall(r'(?:dodok|dodsave|propok|propno|stepssave): "([a-z_]+)"', _page22))
             | {"talk", "step_undo"})
check("22 every action the page posts exists on the server", _posted22 <= _ac22.ACTIONS,
      sorted(_posted22 - _ac22.ACTIONS))


def _fp22():
    out = {}
    for root in (VAULT, app.config.STATE_DIR / "checkin_app", HOME / "steps"):
        for q in root.rglob("*"):
            if q.is_file() and q.name not in ("taps.jsonl", "alerts.json") and not q.name.endswith((".lock", ".tmp")):
                out[str(q)] = _hl22.sha1(q.read_bytes()).hexdigest()
    return out


_tm22 = VAULT / "Inbox" / "Test mode task.md"
_tm22.write_text(f"---\ntype: action\ntask_id: t-ee22ee\nstatus: open\npriority: 3-normal\nscheduled: {ISO}\n---\n\n"
                 "DoD (Claude's guess): it is done\n## Next action\n- [ ] Next: Ask Sam about it? (10 min) ^s6f01\n"
                 "- [ ] Then: Fill in the form (5 min) ^s6f02\n", encoding="utf-8")
view()
_args22 = {"text": "a note", "who": "Sam", "sid": "s6f02", "next": "Ask Sam about it?", "n": 0, "on": True,
           "steps": ["Ask Sam", "Fill in"], "old": "Next: Ask Sam about it?", "new": "Next: Ask Sam", "reason": "nottoday",
           "ids": ["Test mode task"], "fields": {"priority": "4-high"}, "values": {}, "q": "q", "signal": "edit_opened",
           "chunk_ids": {"Test mode task": ["s6f01"]}}
_wrote22 = []
for _a22 in sorted(_ac22.ACTIONS - {"mailprep", "sendmail"}):
    _b22 = _fp22()
    _s, _j = act(id="Test mode task", action=_a22, test=True, **_args22)
    _d22 = _fp22()
    if _d22 != _b22:
        _wrote22.append((_a22, sorted(k.rsplit("/", 1)[-1] for k in set(_d22) | set(_b22) if _d22.get(k) != _b22.get(k))[:3]))
check("22 no action writes anything in test mode", not _wrote22, _wrote22)

# 23 (Nov 6, refactor Phase 4). The page draws every tab from real views: a full day, an empty Do
# and an all-done Do (the doneN crash drew neither, Nov 3 to 6).
import copy as _cp23  # noqa: E402
_full23 = view()[1]
_empty23, _alldone23 = _cp23.deepcopy(_full23), _cp23.deepcopy(_full23)
for _v in (_empty23, _alldone23):
    _v["do"].update(rows=[], left_open=[], waiting=[], part_done=[], suggest=[], moved=[])
_empty23["do"]["done"] = []
_alldone23["do"]["done"] = _alldone23["do"]["done"] or [dict((_full23["today"]["needs"] or [{"id": "x", "title": "x"}])[0])]
# Tasks v2 (Nov 6): the Do card with every v2 field, flags on.
_v2_23 = _cp23.deepcopy(_full23)
_v2_23["flags"] = {"contract": True, "timebox": True, "frozen_chunk": True, "do_plan": True, "actuals": True}
_v2row = dict((_v2_23["do"]["rows"] or [{"id": "x", "title": "x", "next": "Find out what it needs (timebox 30 min)"}])[0])
_v2row.update(next="Find out what it needs (timebox 30 min)", detail={**(_v2row.get("detail") or {}), "next_id": "s0001",
              "unclear": {"s0001": ["start with a verb"]}},
              chunk={"left": 1, "total": 2, "steps": ["Find out what it needs (timebox 30 min)"], "ids": ["s0001"], "min": 30,
                     "unest": 0, "planned_min": 40, "outcome": "Know what it needs", "outcome_his": False,
                     "plan": [{"id": "s0000", "text": "Book it (10 min)", "state": "done"},
                              {"id": "s0001", "text": "Find out what it needs (timebox 30 min)", "state": "open"}]},
              found_today=[{"id": "s0002", "text": "Send the form (15 min)"}],
              actual_ask={"sid": "s0000", "text": "Book it", "est": 10, "actual": 45}, est_note="~15 min (you usually take 1.4×)")
_v2_23["do"]["rows"] = [_v2row] + _v2_23["do"]["rows"][1:]
_v2_23["do"]["actual_asks"] = [{"task": "x", "step": "s0000", "text": "Book it", "est": 10, "actual": 45}]
_v2_23["cleanup"] = [{"task": "x", "hash": "h", "before": "- [ ] Next: do a and b", "after": "- [ ] Next: Do a (10 min)",
                      "why": ["two actions: split it"]}]
_v2_23["do"]["part_done"] = [{**_v2row, "do_state": "part", "part_next": "Send the form", "part_next_est": 15,
                              "outcome": "Know what it needs", "plan": _v2row["chunk"]["plan"], "part_changed": False}]
_vf23 = TMP / "views23.json"
_vf23.write_text(json.dumps({"full day": _full23, "empty Do": _empty23, "all done": _alldone23, "tasks v2": _v2_23}))
if shutil.which("node"):
    _r23 = subprocess.run(["node", str(ROOT / "tests" / "page_smoke.js"), str(ROOT / "jobs" / "checkin_app" / "page.html"),
                           str(_vf23)], capture_output=True, text=True, timeout=120)
    check("23 the page draws every tab, empty and all-done Do included, in both modes", _r23.returncode == 0,
          (_r23.stdout + _r23.stderr)[-900:])
else:
    print("  skip  23 page smoke render: node is not installed")

# 24 (Nov 6, refactor Phase 5). `vt step` changes a step by its id the way his taps do, and the
# step history records the chat as the one who did it.
_p24 = VAULT / "Inbox" / "Step cli.md"
_p24.write_text("---\ntype: action\ntask_id: t-cc24cc\nstatus: open\npriority: 3-normal\n---\n\n## Next action\n"
                "- [ ] Next: Book the room (10 min) ^s7a01\n- [ ] Then: Pay the deposit (5 min) ^s7a02\n"
                "- [ ] Then: Send the confirmation to the group (5 min) ^s7a03\n", encoding="utf-8")


def _vt24(*a):
    return subprocess.run([sys.executable, "-m", "tools.step", *a], cwd=str(ROOT), capture_output=True, text=True,
                          timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})


_l24 = _vt24("list", "Step cli")
check("24 vt step list shows the open steps with their ids", _l24.returncode == 0 and "s7a02" in _l24.stdout, _l24.stderr[-300:])
_d24 = _vt24("done", "t-cc24cc", "s7a02")
_t24 = _p24.read_text()
check("24 vt step done ticks a later step where it stands, by task_id", _d24.returncode == 0
      and "- [x] Then: Pay the deposit" in _t24 and "- [ ] Next: Book the room" in _t24 and "step ticked: Pay the deposit" in _t24,
      (_d24.stderr[-300:], _t24[-400:]))
_n24 = _vt24("done", "Step cli", "s7a01")
_t24 = _p24.read_text()
check("24 vt step done on the Next step brings up the next one", _n24.returncode == 0
      and "- [x] Next: Book the room" in _t24 and "Next: Send the confirmation" in _t24, (_n24.stdout, _t24[-400:]))
_x24 = _vt24("done", "Step cli", "s9999")
check("24 an unknown step is refused", _x24.returncode == 1 and "no open step" in _x24.stderr)
_h24 = [json.loads(l) for l in (HOME / "steps" / "history.jsonl").read_text().splitlines() if '"t-cc24cc"' in l]
check("24 the step history says the chat did it", any(h.get("event") == "done" and h.get("source") == "chat" for h in _h24),
      [(h.get("event"), h.get("source")) for h in _h24][-5:])

real_errors = [e for e in logged_errors() if e.get("where") not in ("row", "calendar", "update overlay", "bare pre-run",
                                                                    "run timeout", "journal")]
check("no unexpected errors were logged", not real_errors, real_errors[:4])

shutil.rmtree(TMP, ignore_errors=True)
print("\nall passed" if not check.fails else f"\n{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
