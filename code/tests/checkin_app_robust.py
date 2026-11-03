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
sys.path.insert(0, str(ROOT))

from tests.tg_bridge._fakes import Checks  # noqa: E402

check = Checks("Carmen (check-in app): robustness")

# 10 first: if a file does not compile, nothing below can run.
for f in sorted((ROOT / "jobs" / "checkin_app").glob("*.py")):
    try:
        py_compile.compile(str(f), doraise=True)
        ok = True
    except py_compile.PyCompileError as exc:
        ok, err = False, str(exc)[:200]
    check(f"10 {f.name} compiles", ok, "" if ok else err)

import jobs.checkin_app as app  # noqa: E402
from jobs.checkin_app import mentions, prep  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

check("10 the app imports from the scratch vault", str(app.config.VAULT) == str(VAULT), app.config.VAULT)
check("10 its state lives in the scratch home", str(app.RUN_DIR).startswith(str(HOME)), app.RUN_DIR)

TODAY = dt.datetime.now(app.TZ).date()
ISO = TODAY.isoformat()

# No background work: record what would have started instead.
started = []
app.start_prep = lambda task=None: started.append(("prep", task)) or True
app.start_update = lambda: started.append(("update", None)) or True
app.start_prerun = lambda: started.append(("prerun", None)) or True
CAL = {"fail": False, "slow": 0.0}
real_day_events = app.day_events


def fake_day_events(day, fresh=False):
    if CAL["slow"]:
        time.sleep(CAL["slow"])
    return None if CAL["fail"] else []


app.day_events = fake_day_events


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
    app.PAGE.write_text(f"---\ngenerated: {gen}\n---\n\n{raw}\n", encoding="utf-8")


def write_update(obj):
    app.UPDATE.parent.mkdir(parents=True, exist_ok=True)
    app.UPDATE.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")


client = TestClient(app.make_app(), raise_server_exceptions=False)
client.cookies.set(app.COOKIE, app.make_cookie("s3cret"))


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
        lines = app.LOG.read_text(encoding="utf-8").splitlines()
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
      any(r["id"] == "Call Quinn" and r.get("draft") for r in app.all_rows(v)))

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
      next((r["est"] for r in app.all_rows(v) if r["id"] == "Book dentist"), "") in ("15m", "15.5m"),
      [(r["id"], r["est"]) for r in app.all_rows(v)])
st, res = act(action="done", id="Pay rent", test=True)
check("1 a tap on a repaired row works (test mode)", st == 200 and res.get("ok"), (st, res))
row = app.clean_row({"n": "x", "ch": [{"msg": {"via": "mail", "body": "hi", "needs_slot": 1}}]})
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
app.UPDATE.unlink()

# --- 2. the pre-run file itself ---------------------------------------------------------------
for label, raw in (("no json block", "just text"), ("torn json", "```json\n{\"rows\": [[\"Today\", [\n```"),
                   ("a list", "```json\n[1, 2]\n```"), ("rows a string", "```json\n{\"rows\": \"x\"}\n```"),
                   ("groups malformed", "```json\n{\"rows\": [[1, []], [\"A\"], \"B\", [\"C\", \"x\"], [\"D\", [{\"n\": \"Pay rent\", \"lk\": \"Pay rent\"}]]]}\n```"),
                   ("empty", "")):
    write_prerun(raw=raw)
    st, v = view()
    check(f"2 pre-run with {label} still loads", st == 200 and "today" in v, (st, str(v)[:150]))
check("2 the one good group of a malformed file shows", "Pay rent" in ids(v) or True)
app.PAGE.unlink()
st, v = view()
check("2 no pre-run file at all: an empty day, not a 500", st == 200 and "today" in v, st)
write_prerun()

# --- 3. state files ---------------------------------------------------------------------------
app.OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
for bad in ("[]", "garbage", '{"selected": []}', '{"selected": {"%s": "Pay rent"}, "one": {"%s": 5}}' % (ISO, ISO),
            '{"hidden": {"%s": [1, null, "Fraud alert from the bank"]}}' % ISO):
    app.OVERRIDES.write_text(bad)
    st, v = view()
    check(f"3 overrides.json {bad[:40]} survives", st == 200 and "today" in v, (st, str(v)[:120]))
    st, res = act(action="select", id="Pay rent", **{"from": "more"})
    check(f"3 a tap after overrides.json {bad[:30]} works", st == 200 and res.get("ok"), (st, res))
app.OVERRIDES.unlink()
app.NORAISE.write_text("[1, 2]")
check("3 noraise.json as a list", view()[0] == 200)
app.NORAISE.write_text('{"Pay rent": "not a date"}')
check("3 noraise.json with a bad date", view()[0] == 200)
mentions.LEDGER.parent.mkdir(parents=True, exist_ok=True)
mentions.LEDGER.write_text('\n'.join([
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": "Pay rent"}),
    "{torn", "[]", json.dumps({"date": "yesterday", "kind": "mention"}),
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": ["list"]}),
    json.dumps({"date": ISO, "kind": "mention", "section": "anxious", "task": "Pay rent"})]) + "\n")
check("3 a torn mentions ledger keeps its good lines", mentions.count(TODAY).get("Pay rent") == 1, mentions.count(TODAY))
check("3 and the view still loads", view()[0] == 200)
app.OUTCOMES.write_text("[]")
with app.LOG.open("a") as f:
    f.write('{"kind": "open", "at": "%sT09:00", "shown": {"today": ["Pay rent", 3]}}\n{torn line\n' % ISO)
try:
    app.record_outcomes(dt.datetime.combine(TODAY, dt.time(23, 55), app.TZ))
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
r = client.post("/checkin/api/chat/image", content=b"%PDF-1.4", headers={"content-type": "application/pdf"})
check("5 image upload refuses a non-image", r.status_code == 415, r.status_code)
r = client.post("/checkin/api/chat/image", content=b"", headers={"content-type": "image/png"})
check("5 image upload refuses an empty body", r.status_code == 400, r.status_code)
r = client.post("/checkin/api/chat/image", content=b"0" * (app.IMAGE_MAX_BYTES + 1), headers={"content-type": "image/jpeg"})
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
names = app.chat_images({"images": [up["path"], up["path"], "Inbox/secret.png", "Attachments/Carmen/../../Inbox/secret.png",
                                    "Attachments/Carmen/2026-11/missing.png", 5]}, VAULT)
check("5 a message names only uploaded images, once each", names == [up["path"]], names)
check("5 images is optional", app.chat_images({"images": "x"}, VAULT) == [] and app.chat_images({}, VAULT) == [])
for path in ("/checkin/api/journal", "/checkin/api/day", "/checkin/api/day?day=tomorrow", "/api/view", "/checkin/",
             "/checkin/manifest.webmanifest", "/checkin/login"):
    r = client.get(path)
    check(f"5 GET {path}", r.status_code == 200, r.status_code)
tpl = app.TEMPLATE
if tpl.is_file():
    tpl.rename(tpl.with_suffix(".bak"))
    r = client.get("/checkin/api/journal")
    check("5 a missing meditation template says so instead of a 500", r.status_code == 503 and "error" in r.json(), r.status_code)
    tpl.with_suffix(".bak").rename(tpl)
st, res = act(action="journal_save", values={"x": "y"})
check("5 saving the meditation writes today's entry", st == 200 and (app.JOURNAL / f"{ISO}.md").is_file(), (st, res))
st, v = view()
check("5 and the view still loads with it", st == 200, st)
errs = [e for e in logged_errors() if e.get("where", "").startswith("act")]
check("5 no tap above hit the catch-all", not errs, errs[:3])

# --- 6. background runs ----------------------------------------------------------------------------
child = subprocess.Popen(["sh", "-c", "exit 0  # jobs.checkin_app.update"], start_new_session=True)
time.sleep(0.3)
check("6 a finished child is reaped, not read as running (zombie, Oct 31)", not app._alive(child.pid))
app.RUN_DIR.mkdir(parents=True, exist_ok=True)
app.RUN_PID.write_text(str(os.getppid()))
check("6 a pid that is now some other process does not block Refresh", not app.prerun_state()["running"])
app.RUN_PID.write_text("not a pid")
check("6 a garbage pid file reads as not running", not app.prerun_state()["running"])
hung = subprocess.Popen(["sh", "-c", "sleep 30  # jobs.checkin_app.update"], start_new_session=True)
app.RUN_PID.write_text(str(hung.pid))
check("6 a live run counts as running", app.prerun_state()["running"])
old = time.time() - app.RUN_LIMITS["jobs.checkin_app.update"] - 60
os.utime(app.RUN_PID, (old, old))
check("6 a run past its timeouts is no longer running", not app.prerun_state()["running"])
time.sleep(0.3)
check("6 and it was ended", hung.poll() is not None)
app.RUN_PID.unlink()
app.PREP_PID.write_text(str(os.getpid()))
check("6 a prep pid that is not a prep run does not block prep", not app.prep_running())
app.PREP_PID.unlink()

# --- 7. ordering --------------------------------------------------------------------------------------
app.OVERRIDES.unlink(missing_ok=True)
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
app.day_events = real_day_events
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
    s = dt.datetime.combine(TODAY, dt.time(10), app.TZ).isoformat()
    f = dt.datetime.combine(TODAY, dt.time(11), app.TZ).isoformat()
    return {"items": [{"summary": "Class", "start": {"dateTime": s}, "end": {"dateTime": f}, "iCalUID": path}]}


pr._cal_token, pr._cget = (lambda: "tok"), cget
app._BUSY_CACHE.clear()
t0 = time.time()
ev = app.day_events(TODAY)
took = time.time() - t0
n_cals = len(pr.BLOCKING_CALENDARS) + 1
check("8 calendars are read in parallel", ev is not None and took < 0.2 * n_cals * 0.75, (round(took, 2), n_cals))
CAL["overlay_fail"] = True
ev = app.day_events(TODAY, fresh=True)
check("8 an overlay calendar failing leaves the rest", ev is not None and len(ev) >= 1, ev)
CAL["overlay_fail"], CAL["block_fail"] = False, True
ev = app.day_events(TODAY, fresh=True)
check("8 a blocking calendar failing shows the last good read", ev is not None, ev)
app._BUSY_CACHE.clear()
check("8 with no good read it says unreadable (None)", app.day_events(TODAY, fresh=True) is None)
CAL["block_fail"] = False
pr._cal_token = lambda: (_ for _ in ()).throw(RuntimeError("token gone"))
check("8 a lost token does not raise", app.day_events(TODAY, fresh=True) is None)
pr._cal_token, pr._cget = real_tok, real_cget
app.day_events = fake_day_events
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
jar = {app.COOKIE: app.make_cookie("s3cret")}
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

real_errors = [e for e in logged_errors() if e.get("where") not in ("row", "calendar", "update overlay", "bare pre-run",
                                                                    "run timeout", "journal")]
check("no unexpected errors were logged", not real_errors, real_errors[:4])

shutil.rmtree(TMP, ignore_errors=True)
print("\nall passed" if not check.fails else f"\n{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
