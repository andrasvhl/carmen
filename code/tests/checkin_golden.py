#!/usr/bin/env python3
"""Carmen's behaviour pinned (Nov 6, the server split): a frozen clock, fixed ids, a fake calendar
and a scratch vault, then the view, every tap action, the plan prompt, the budget and the day's
outcome, recorded with the code as it was before the split. The split is a move, so every later
run must give the same record. Exit 1 on any difference, with the first lines of each.

    .venv/bin/python -m tests.checkin_golden            # compare with tests/fixtures/checkin_golden.json
    .venv/bin/python -m tests.checkin_golden --record   # write it (only when behaviour changes on purpose)

It finds every name by searching the app's modules, so it holds before, during and after a move.
"""
import datetime as _real_dt
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import types

# Sets iterate by hash; a fixed seed keeps the record stable. Re-run under it if needed.
if os.environ.get("PYTHONHASHSEED") != "0":
    sys.exit(subprocess.call([sys.executable, "-m", "tests.checkin_golden", *sys.argv[1:]],
                             env={**os.environ, "PYTHONHASHSEED": "0"}))

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "checkin_golden.json"
REAL_SKILL = pathlib.Path.home() / "vault" / "_system" / "skills" / "plan-today"
if not REAL_SKILL.is_dir():
    REAL_SKILL = pathlib.Path.home() / "Obsidian" / "<VAULT_NAME>" / "_system" / "skills" / "plan-today"
TEMPLATE_SRC = REAL_SKILL.parents[2] / "Areas/Personal/Meditation Journal/_Daily Meditation Template.md"

# --- a scratch world before the app is imported (its paths are fixed at import) ---------------
TMP = pathlib.Path(tempfile.mkdtemp(prefix="carmen-golden-"))
VAULT, HOME = TMP / "vault", TMP / "home"
for d in ("Inbox", "Archive/Inbox", "Areas/Personal/Meditation Journal", "_system/claude", "_system/skills"):
    (VAULT / d).mkdir(parents=True, exist_ok=True)
shutil.copytree(REAL_SKILL, VAULT / "_system/skills/plan-today",
                ignore=shutil.ignore_patterns("__pycache__", "dropped.json", "-c"))
shutil.copy(TEMPLATE_SRC, VAULT / "Areas/Personal/Meditation Journal" / TEMPLATE_SRC.name)
HOME.mkdir()
(HOME / ".automation.env").write_text("CHECKIN_PASSWORD=pw\nCHECKIN_SECRET=s3cret\n")
os.environ["VAULT_PATH"], os.environ["HOME"] = str(VAULT), str(HOME)
os.environ["STEPS_DIR"] = str(HOME / "steps")
os.environ["PT_DROPPED"] = str(TMP / "dropped.json")
sys.path.insert(0, str(ROOT))

# --- fixed ids and a frozen clock ---------------------------------------------------------------
import secrets  # noqa: E402

_n = {"i": 0}


def _hex(nbytes=16):
    _n["i"] += 1
    return f"{_n['i']:0{2 * nbytes}x}"[-2 * nbytes:]


secrets.token_hex = _hex
from zoneinfo import ZoneInfo  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
NOW = _real_dt.datetime(2026, 11, 4, 10, 30, tzinfo=PT)   # a Wednesday morning
TODAY = NOW.date()


class _FDate(_real_dt.date):
    @classmethod
    def today(cls):
        return TODAY


class _FDT(_real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return NOW.replace(tzinfo=None)


FDT = types.ModuleType("datetime")
FDT.__dict__.update({k: getattr(_real_dt, k) for k in dir(_real_dt) if not k.startswith("__")})
FDT.datetime, FDT.date = _FDT, _FDate


def freeze():
    """Every loaded module of the app, its tools and the skill's, on the frozen clock."""
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name.startswith(("jobs.checkin_app", "tools.", "runner."))
                               or str(getattr(mod, "__file__", "") or "").startswith(str(VAULT))):
            continue
        for attr in ("dt", "datetime"):
            if getattr(mod, attr, None) is _real_dt:
                setattr(mod, attr, FDT)


import pkgutil  # noqa: E402
import importlib  # noqa: E402

import jobs.checkin_app as app  # noqa: E402
from runner import config, notify  # noqa: E402

assert str(config.STATE_DIR).startswith(str(TMP)) and str(config.VAULT).startswith(str(TMP)), \
    "golden suite is not isolated: refusing to touch live state"
for m in pkgutil.walk_packages(app.__path__, app.__name__ + "."):
    if not m.name.endswith("__main__"):
        importlib.import_module(m.name)
import tools.steps, tools.todos, tools.taskids  # noqa: E401,E402
freeze()


def mods():
    return [m for n, m in sorted(sys.modules.items()) if m is not None and n.startswith("jobs.checkin_app")]


def find(name):
    """The object the app knows by this name, wherever it lives now."""
    for m in mods():
        if name in vars(m):
            obj = vars(m)[name]
            if getattr(obj, "__module__", m.__name__) == m.__name__ or not callable(obj):
                return obj
    for m in mods():
        if name in vars(m):
            return vars(m)[name]
    raise LookupError(name)


def patch(name, value):
    """Replace the object every module holds under this name (a bound import included)."""
    orig, hit = find(name), 0
    for m in mods():
        if vars(m).get(name) is orig:
            setattr(m, name, value)
            hit += 1
    assert hit, name


STARTED, SENT, PUSHED = [], [], []
patch("start_prep", lambda task=None: STARTED.append(["prep", task]) or True)
patch("start_update", lambda: STARTED.append(["update"]) or True)
patch("start_prerun", lambda: STARTED.append(["prerun"]) or True)
patch("maybe_start_prep", lambda generated: STARTED.append(["maybe_prep", generated]))
notify.send_telegram = lambda text, silent=True, **k: SENT.append(text)
patch("_send", lambda title, message, priority: PUSHED.append([title, priority]))   # alerts.py


def fake_day_events(day, fresh=False):
    if day != TODAY:
        return []
    return [
        {"s": 0, "f": 1440, "title": "Election day", "allday": True, "busy": False, "kind": "main",
         "cal": "Stanford", "loc": "", "desc": "", "tasks": [], "task_ids": [], "key": "ev-allday"},
        {"s": 660, "f": 740, "title": "STRAMGT 211", "allday": False, "kind": "main", "cal": "Stanford",
         "loc": "GSB", "desc": "", "tasks": [], "task_ids": [], "key": "ev-class", "tentative": False,
         "slot": False, "busy": True},
        {"s": 840, "f": 960, "title": "❗ Big rocks", "allday": False, "kind": "focus", "cal": "Focus",
         "loc": "", "desc": "", "tasks": [], "task_ids": [], "key": "ev-rocks", "tentative": False,
         "slot": True, "busy": False},
        {"s": 990, "f": 1020, "title": "Batch: Pay rent", "allday": False, "kind": "focus", "cal": "Focus",
         "loc": "", "desc": "Task: obsidian://open?vault=<VAULT_NAME>&file=Inbox%2FPay%20rent (t-00000a)",
         "tasks": ["Pay rent"], "task_ids": ["t-00000a"], "key": "ev-batch", "tentative": False,
         "slot": False, "busy": False},
        {"s": 1080, "f": 1110, "title": "Coffee with Ana", "allday": False, "kind": "main", "cal": "Stanford",
         "loc": "", "desc": "", "tasks": [], "task_ids": [], "key": "ev-coffee", "tentative": True,
         "slot": False, "busy": False},
    ]


patch("day_events", fake_day_events)

ISO, TOM = TODAY.isoformat(), (TODAY + _real_dt.timedelta(days=1)).isoformat()
YEST = (TODAY - _real_dt.timedelta(days=1)).isoformat()


def note(name, fm="", next_block="", body="", folder="Inbox"):
    (VAULT / folder / f"{name}.md").write_text(
        f"---\ntype: action\n{fm}---\n\n## Next action\n{next_block}{body}", encoding="utf-8")


note("Call Quinn", f"status: open\npriority: 4-high\nscheduled: {ISO}\ntask_id: t-00000b\ntimeEstimate: 30\n",
     "DoD (Claude's guess): Quinn has a lunch date\n- [ ] Next: Draft the reply to Quinn (10 min) ^s0b01\n"
     "    - [Thread](https://mail.google.com/x)\n    - [[Call Quinn#Notes|notes]]\n"
     "- [ ] Then: Book the table ^s0b02\n- [ ] Pick the place 📅 2026-11-06 ^s0b03\n",
     "\n## Notes\nShe likes Thai.\n\n## Log\n- Nov 3 · step done: Find her number · check-in app\n")
note("Pay rent", f"status: open\npriority: 3-normal\ndue: {ISO}\ntask_id: t-00000a\n",
     "- [ ] Next: Transfer the rent (5 min) ^s0a01\n- [ ] Then: Save the receipt ^s0a02\n- [x] Look up the amount ✅ 2026-11-03 ^s0a03\n",
     "\n## Log\n- Nov 4 · paid the deposit · Claude\n")
note("Book dentist", "status: open\npriority: 3-normal\nwaiting: true\n",
     "- [ ] Waiting: dentist office, on a slot (since Nov 1) ^s0c01\n- [ ] Next: Pick a slot ^s0c02\n",
     "\n## Log\n- Nov 1 · waiting on dentist office · check-in app\n")
note("Write memo", f"status: open\npriority: 2-low\nscheduled: {TOM}\n", "- [ ] Next: Outline the memo ^s0d01\n")
note("File taxes", "status: open\npriority: 3-normal\n",
     "DoD: Return filed (owner decision, Nov 2)\n- [ ] Next: Collect the W-2 (due Nov 13) ^s0e01\n- [ ] Ask the accountant ^s0e02\n")
note("Read case", f"status: open\npriority: 1-none\nscheduled: {ISO}\n", "- [ ] Next: Read pages 1 to 10 ^s0f01\n")
note("Plan trip", f"status: done\ncompletedDate: {ISO}\npriority: 3-normal\n", "- [x] Next: Book flights ^s0g01\n",
     f"\n## Log\n- Nov 4 · done · check-in app\n")
note("Email Tom", "status: open\npriority: 3-normal\n", "- [ ] Next: Ask Tom about the deck ^s0h01\n")
note("Fix bike", f"status: open\npriority: 3-normal\nscheduled: {YEST}\n", "- [ ] Next: Buy a tube ^s0i01\n")
note("Old thing", f"status: done\ncompletedDate: {YEST}\npriority: 3-normal\n", "- [x] Next: Finish ^s0j01\n",
     folder="Archive/Inbox")

ROWS = [
    ["Today", [
        {"n": "Call Quinn", "lk": "Call Quinn", "g": "today", "p": "H", "s": "lunch this week", "nx": "Draft the reply",
         "e": "30m", "ne": "10m", "anx": 1,
         "ch": [{"t": "draft the reply", "k": "draft",
                 "msg": {"via": "mail", "to": "sarah@example.com", "subj": "Re: lunch", "body": "Hi Quinn, Thursday?",
                         "acct": "<YOUR_EMAIL>"}}]},
        {"n": "Pay rent", "lk": "Pay rent", "g": "today", "p": "N", "s": "due today", "nx": "Transfer", "e": "10m",
         "cp": [{"t": "look up the amount", "k": "research"}], "u": 1},
        {"n": "Read case", "lk": "Read case", "g": "today", "p": "Z", "s": "class at 11", "e": "45m"},
        {"n": "Email Tom", "lk": "Email Tom", "g": "soon", "p": "N", "s": "deck", "e": 20,
         "prop": [{"old": "Next: Ask Tom about the deck", "new": "Next: Send Tom the deck draft", "why": "he asked"}],
         "ch": [{"t": "text Tom", "msg": {"via": "WhatsApp", "to": "Tom", "body": "Deck tonight?"}}]},
    ]],
    ["Overdue", [{"n": "Fix bike", "lk": "Fix bike", "g": "today", "p": "N", "s": "flat", "e": "1h"}]],
    ["Waiting", [{"n": "Book dentist", "lk": "Book dentist", "g": "wait", "p": "N", "wf": "dentist office", "gw": 0}]],
    ["Everything else", [{"n": "File taxes", "lk": "File taxes", "g": "soon", "p": "N", "s": "April", "e": "2h"},
                         {"n": "Write memo", "lk": "Write memo", "g": "later", "p": "Z"}]],
    ["New from mail and WhatsApp", [{"n": "Fraud alert from the bank", "g": "today", "p": "H", "s": "a mail",
                                     "nx": "Call the bank", "ne": "5m"},
                                    {"n": "Reply to Luciana", "g": "today", "p": "N", "s": "WhatsApp",
                                     "ch": [{"t": "reply", "msg": {"via": "WhatsApp", "to": "Luciana", "body": "Yes!"}}]}]],
]
(VAULT / "_system/claude/plan-today-prerun.md").write_text(
    f"---\ngenerated: {ISO}T08:00-07:00\n---\n\n```json\n" + json.dumps({"rows": ROWS, "notes": ["Busy day"]}) + "\n```\n",
    encoding="utf-8")
STATE = config.STATE_DIR / "checkin_app"
STATE.mkdir(parents=True, exist_ok=True)
(STATE / "overrides.json").write_text(json.dumps({"selected": {ISO: ["Pay rent", "Read case"]},
                                                  "pulled": {ISO: ["Read case"]}}))

# --- the record ---------------------------------------------------------------------------------
from starlette.testclient import TestClient  # noqa: E402

client = TestClient(find("make_app")(), raise_server_exceptions=False)
client.cookies.set(find("COOKIE"), find("make_cookie")("s3cret"))
REC: dict = {}


def norm(obj):
    """What may differ between two runs of the same code: signed link expiries, the scratch path."""
    s = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    s = re.sub(r"&e=\d+&s=[0-9a-f]+", "&e=E&s=S", s)
    s = s.replace(str(TMP), "<TMP>")
    s = re.sub(r"\b1[6-9]\d{8}\.\d+", "0.0", s)   # wall-clock timestamps (time.time())
    return json.loads(s)


def view():
    freeze()
    r = client.get("/checkin/api/view?open=1")
    return norm({"status": r.status_code, "body": r.json()})


def tap(section, **body):
    freeze()
    r = client.post("/checkin/api/act", json=body)
    try:
        out = r.json()
    except ValueError:
        out = r.text[:300]
    REC.setdefault(section, []).append(norm({"tap": body, "status": r.status_code, "body": out}))


REC["view.before"] = view()
r = client.get("/checkin/api/day")
REC["calendar.day"] = norm({"status": r.status_code, "body": r.json()})
r = client.get("/checkin/api/journal")
REC["signals.journal_entry"] = norm({"status": r.status_code, "body": r.json()})

# Tasks: steps, DoD, proposals, the note's fields
tap("tasks", action="subtick", id="Call Quinn", n=1, on=True, next="Draft the reply to Quinn (10 min)", sid="s0b01")
tap("tasks", action="tick_line", id="Call Quinn", text="Book the table", sid="s0b02")
tap("tasks", action="step_done", id="Pay rent")
tap("tasks", action="step_undo", id="Pay rent")
tap("tasks", action="step_done", id="Pay rent")
tap("tasks", action="step_wait", id="Pay rent", text="landlord")
tap("tasks", action="edit_waiting", id="Pay rent", text="landlord Joe")
tap("tasks", action="not_waiting", id="Pay rent")
tap("tasks", action="set_waiting", id="File taxes", text="accountant")
tap("tasks", action="step_discard", id="File taxes", text="Ask the accountant", sid="s0e02")
tap("tasks", action="step_discard", id="File taxes", text="gone", sid="zzzz")
tap("tasks", action="dod_set", id="File taxes", text="Return filed and paid")
tap("tasks", action="dod_confirm", id="Call Quinn")
tap("tasks", action="prop_ignore", id="Email Tom", old="Next: Ask Tom about the deck")
tap("tasks", action="prop_accept", id="Email Tom", old="Next: Ask Tom about the deck", new="Next: Send Tom the deck draft")
tap("tasks", action="steps_save", id="Read case", steps=["Read pages 1 to 12", "Write two questions"])
tap("tasks", action="next", id="Read case", text="Read pages 1 to 15")
tap("tasks", action="note", id="Read case", text="the case is long")
tap("tasks", action="update", id="Read case", fields={"priority": "3-normal", "due": TOM, "time_est": "45",
                                                       "next_min": "20"})
tap("tasks", action="update", id="Read case", fields={})
tap("tasks", action="notdone", id="Call Quinn")
tap("tasks", action="done", id="Email Tom", why="sent it")
tap("tasks", action="reopen", id="Email Tom")
tap("tasks", action="remove", id="Fix bike", reason="nottoday", why="no time")
tap("tasks", action="remove", id="Fix bike", reason="bogus")
tap("tasks", action="add", text="Buy stamps")
tap("tasks", action="add", text="  ")
tap("tasks", action="step_done", id="Write memo", test=True)
tap("tasks", action="auto_close_undo", id="Read case")
tap("tasks", action="next", id="No such note", text="x")

# Planning: selection, the one thing, Do
tap("planning", action="select", id="File taxes", **{"from": "more"})
tap("planning", action="select", id="Fraud alert from the bank", **{"from": "needs"})
tap("planning", action="unselect", id="Read case", **{"from": "selection"})
tap("planning", action="swap", id="File taxes", previous="Call Quinn")
tap("planning", action="notone", id="File taxes", why="not today")
tap("planning", action="confirm", ids=["Pay rent", "File taxes"])
tap("planning", action="plan", ids=["Pay rent", "File taxes", "Gone task"])
tap("planning", action="plan", ids=[])
tap("planning", action="do_commit", ids=["Pay rent", "File taxes", "Call Quinn"],
    chunks={"Pay rent": ["Transfer the rent (5 min)"], "Call Quinn": ["Draft the reply to Quinn (10 min)", "Book (15 min)"]},
    chunk_ids={"Pay rent": ["s0a01"]})
tap("planning", action="do_add", id="Write memo", **{"from": "more"})
tap("planning", action="do_more", id="Write memo")
tap("planning", action="do_drop", id="Write memo")
tap("planning", action="do_unmove", id="Write memo")
tap("planning", action="do_keep", id="Pay rent")
tap("planning", action="do_dismiss", id="Book dentist")
tap("planning", action="do_bogus", id="Pay rent")
tap("planning", action="tomorrow", id="Call Quinn")
tap("planning", action="schedule", id="Call Quinn")
tap("planning", action="select", id="Write memo", day="tomorrow", **{"from": "more"})

# Direction: priority
tap("direction", action="raise", id="File taxes")
tap("direction", action="noraise", id="File taxes")

# Signals and the agents' taps
tap("signals", action="mark", id="Call Quinn", signal="reply_copied", text="Hi Quinn, Thursday at 12?")
tap("signals", action="mark", id="Call Quinn", signal="edit_opened")
tap("signals", action="meditation")
tap("agents", action="answer", id="Pay rent", q="Which account?", text="Chase")
tap("agents", action="answer", id="Pay rent", q="x", text="")
tap("agents", action="prepare", id="File taxes")
tap("agents", action="talk", id="Call Quinn", tab="claude", text="ask about Thursday")

# Outbox: replies
tap("outbox", action="replied", id="Email Tom", text="Deck tonight? Sent it")
tap("outbox", action="skipreply", id="Reply to Luciana")
tap("outbox", action="sendmail", id="Pay rent")
tap("outbox", action="mailprep", id="Read case")

tap("misc", action="nope", id="Pay rent")
tap("misc", action="nope", id="Not a row at all")
tap("misc")

REC["view.after"] = view()

# The functions other code calls directly
rows = [r for r in find("all_rows")(REC["view.after"]["body"]) if r["id"] in ("Pay rent", "File taxes", "Call Quinn")][:3]
freeze()
REC["planning.plan_text"] = norm(find("plan_text")(TODAY, rows, ending=False))
REC["planning.budget"] = norm(find("budget")(TODAY, rows))
REC["planning.plan_pending"] = norm(find("plan_pending")(TODAY))
REC["tasks.today_wins"] = norm(find("today_wins")(TODAY))
REC["tasks.closed_recently"] = norm(find("closed_recently")(TODAY))
REC["tasks.read_note"] = norm(find("read_note")(VAULT / "Inbox" / "Call Quinn.md"))
REC["signals.load_prerun"] = norm(find("load_prerun")())
REC["outcomes.day_outcome"] = norm(find("day_outcome")(TODAY))
REC["outcomes.record"] = norm(find("record_outcomes")(NOW.replace(hour=23, minute=55)))
REC["talk.talk_text"] = norm(find("talk_text")(rows[0], "a note", ending=False))
tap("signals", action="journal_save", values={}, update=True)   # last: today's entry changes every view after it

REC["agents.started"], REC["agents.telegram"], REC["alerts.pushed"] = STARTED, norm(SENT), PUSHED
REC["notes"] = norm({str(p.relative_to(VAULT)): p.read_text(encoding="utf-8")
                     for p in sorted(VAULT.rglob("*.md")) if "_system/skills" not in str(p) and "_Daily" not in p.name})
REC["state"] = norm({str(p.relative_to(config.STATE_DIR)): p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted(config.STATE_DIR.rglob("*")) if p.is_file() and p.suffix not in (".log", ".pid")
                     and p.name != "taps.jsonl"})
REC["log"] = norm([json.loads(ln) for ln in (STATE / "taps.jsonl").read_text(encoding="utf-8").splitlines()])
REC["steps"] = norm({str(p.relative_to(HOME / "steps")): p.read_text(encoding="utf-8")
                     for p in sorted((HOME / "steps").rglob("*")) if p.is_file()})

if "--record" in sys.argv:
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(REC, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"recorded {len(REC)} sections to {FIXTURE.relative_to(ROOT)}")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0)

want = json.loads(FIXTURE.read_text(encoding="utf-8"))
got = json.loads(json.dumps(REC, ensure_ascii=False, sort_keys=True))
bad = 0
import difflib  # noqa: E402

for k in sorted(set(want) | set(got)):
    if want.get(k) == got.get(k):
        print(f"ok    {k}")
        continue
    bad += 1
    print(f"FAIL  {k}")
    a = json.dumps(want.get(k), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    b = json.dumps(got.get(k), ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    for ln in list(difflib.unified_diff(a, b, "recorded", "now", n=1, lineterm=""))[:30]:
        print("      " + ln)
shutil.rmtree(TMP, ignore_errors=True)
print(f"{len(want) - bad}/{len(want)} sections unchanged" if not bad else f"{bad} section(s) changed")
sys.exit(1 if bad else 0)
