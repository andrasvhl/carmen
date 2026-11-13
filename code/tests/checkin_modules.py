#!/usr/bin/env python3
"""Carmen's module boundaries (Nov 6, the server split along the module map of Nov 5).

    .venv/bin/python -m tests.checkin_modules

  1. Imports. Modules (tasks, planning, direction, signals, outbox) own their data. A module reaches
     another only through its read.py (reads) or actions.py (writes), never its other files, and
     never the package root, the tap dispatcher or the web layer. Across modules a top-level import
     is a module import (`from ..tasks import read as tasks`), so module files can import each
     other without a cycle failing. Adapters (adapters/) import no module.
  2. __init__.py holds only the app factory and its routes.
  3. The tap dispatcher: the same 50 actions as before the split, each handled in the module that
     owns what it changes.
  4. Planning: Do never re-ranks. Today's committed order changes only through Do's own taps, not
     through anything that changes the ranking (select, the one thing, not the one thing, priority).
  5. Notes: compare-and-swap on every task note write. A note changed underneath gets the change
     recomputed once on what is there now, and refused ("the note changed; refresh") if it changed
     again; nothing writes a task note around the Notes adapter.
"""
import ast
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG, PKG_DIR = "jobs.checkin_app", ROOT / "jobs" / "checkin_app"
MODULES = ("tasks", "planning", "direction", "signals", "outbox")
INFRA_FOR_MODULES = ("adapters", "runs", "chat", "alerts")   # what a module may use besides modules
sys.path.insert(0, str(ROOT))

# Run from a git hook, GIT_DIR and GIT_INDEX_FILE point at the real repo: a fixture `git init` then
# rewrote its config (Nov 12, core.bare). Fixture repos get a clean git environment.
for _k in [k for k in os.environ if k.startswith("GIT_")]:
    os.environ.pop(_k)
# A scratch HOME and vault before anything imports runner.config (its paths are fixed at import).
TMP = pathlib.Path(tempfile.mkdtemp(prefix="carmen-modules-"))
VAULT, HOME = TMP / "vault", TMP / "home"
for d in ("Inbox", "Archive/Inbox", "Areas/Personal/Meditation Journal", "_system/claude", "_system/skills"):
    (VAULT / d).mkdir(parents=True, exist_ok=True)
REAL_SKILL = pathlib.Path.home() / "vault" / "_system" / "skills" / "plan-today"
if not REAL_SKILL.is_dir():
    REAL_SKILL = pathlib.Path.home() / "Obsidian" / "<VAULT_NAME>" / "_system" / "skills" / "plan-today"
shutil.copytree(REAL_SKILL, VAULT / "_system/skills/plan-today", ignore=shutil.ignore_patterns("__pycache__", "dropped.json", "-c"))
HOME.mkdir()
(HOME / ".automation.env").write_text("CHECKIN_PASSWORD=pw\nCHECKIN_SECRET=s3cret\n")
os.environ["VAULT_PATH"], os.environ["HOME"] = str(VAULT), str(HOME)
os.environ["STEPS_DIR"] = str(HOME / "steps")
os.environ["PT_DROPPED"] = str(TMP / "dropped.json")

from tests.tg_bridge._fakes import Checks  # noqa: E402

check = Checks("Carmen: module boundaries")


def mod_of(p):
    parts = list(p.relative_to(ROOT).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def resolve(node, mod, path):
    if not node.level:
        return node.module or ""
    base = (mod if path.name == "__init__.py" else mod.rpartition(".")[0]).split(".")
    base = base[:len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def is_module(full):
    p = ROOT / pathlib.Path(*full.split("."))
    return p.with_suffix(".py").exists() or (p / "__init__.py").exists()


def imports(path):
    """(line, module imported from or as, the names, at top level?) for every import into the package."""
    mod, tree = mod_of(path), ast.parse(path.read_text())
    top = set(map(id, tree.body))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = resolve(node, mod, path)
            if base.startswith(PKG):
                for a in node.names:
                    full = f"{base}.{a.name}"
                    yield node.lineno, (full if is_module(full) else base), (None if is_module(full) else a.name), id(node) in top
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith(PKG):
                    yield node.lineno, a.name, None, id(node) in top



# --- 1. imports ----------------------------------------------------------------------------------
bad = []
for path in sorted(PKG_DIR.rglob("*.py")):
    rel = path.relative_to(PKG_DIR)
    own = rel.parts[0] if len(rel.parts) > 1 else None
    if own not in MODULES + ("adapters",):
        continue
    for line, target, name, at_top in imports(path):
        parts = target[len(PKG) + 1:].split(".") if target != PKG else []
        where = f"{rel}:{line} imports {target}" + (f".{name}" if name else "")
        if not parts:
            bad.append(f"{where}: the package root (its names live in modules now)")
            continue
        head = parts[0]
        if own == "adapters":
            if head in MODULES or head not in ("adapters", "alerts"):
                bad.append(f"{where}: an adapter uses no module")
            continue
        if head == own or head in INFRA_FOR_MODULES:
            continue
        if head not in MODULES:
            bad.append(f"{where}: a module does not use {head}")
            continue
        if parts[1:] not in (["read"], ["actions"]):
            bad.append(f"{where}: only {head}/read.py or {head}/actions.py, not its internals")
        elif at_top and name is not None:
            bad.append(f"{where}: across modules a top-level import is a module import (cycles)")
check("1 a module reaches another only through its read.py or actions.py", not bad, "\n      ".join(bad[:20]))

# --- 2. __init__.py --------------------------------------------------------------------------------
tree = ast.parse((PKG_DIR / "__init__.py").read_text())
defined = set()
for st in tree.body:
    if isinstance(st, (ast.FunctionDef, ast.ClassDef)):
        defined.add(st.name)
    elif isinstance(st, ast.Assign):
        defined |= {t.id for t in st.targets if isinstance(t, ast.Name)}
check("2 __init__.py holds only the app factory and routes", defined <= {"HTML", "PORT", "make_app", "main"},
      sorted(defined - {"HTML", "PORT", "make_app", "main"}))

import jobs.checkin_app as app  # noqa: E402
from jobs.checkin_app import actions as dispatch, runs  # noqa: E402
from jobs.checkin_app.adapters import calendar, clock  # noqa: E402
from jobs.checkin_app.planning import do as do_store  # noqa: E402

assert str(app.config.STATE_DIR).startswith(str(TMP)) and str(app.config.VAULT).startswith(str(TMP)), \
    "modules suite is not isolated: refusing to touch live state"
runs.start_prep = lambda task=None: True
runs.start_update = lambda: True
runs.start_prerun = lambda: True
runs.maybe_start_prep = lambda generated: None
calendar.day_events = lambda day, fresh=False: []
# Do's day as the calendar date: between 00:00 and 04:00 Plan's taps date a task for the calendar
# date while Do is still on the evening's day, which is another matter than the order checked here.
clock.do_day = lambda now: now.date()

# --- 3. the dispatcher ---------------------------------------------------------------------------------
BEFORE = {"add", "answer", "auto_close_undo", "confirm", "do_add", "do_commit", "do_dismiss", "do_drop", "do_keep",
          "do_more", "do_moveok", "do_partclose", "do_unmove", "dod_confirm", "dod_set", "done", "edit_waiting", "journal_save", "mailprep", "mark",
          "meditation", "next", "noraise", "not_waiting", "notdone", "note", "notone", "plan", "prepare",
          "prop_accept", "prop_ignore", "raise", "remove", "reopen", "replied", "schedule", "select", "sendmail",
          "set_waiting", "skipreply", "step_discard", "step_done", "step_undo", "step_wait", "steps_save", "subtick",
          "swap", "talk", "tick_line", "tomorrow", "unselect", "update"}
# Tasks v2 (Nov 6) adds its own taps, each in the module that owns what it changes.
V2 = {"step_box", "do_outcome", "step_start", "actual_check", "cleanup_apply", "cleanup_skip", "step_fine", "step_feedback", "do_chunk_feedback", "cleanup_keep", "cleanup_close"}
V2 |= {"task_finish"}   # Close task on the flipped Do card (Nov 8)
V2 |= {"skipreply_undo"}   # Undo on Skip in Replies (Nov 9)
V2 |= {"trail_ok", "trail_undo"}   # the chat's step changes in the open card: OK / Undo (Nov 9)
V2 |= {"remove_undo"}   # r8 F5: Undo on Not today's toast
V2 |= {"step_untick", "do_clear", "do_back"}   # one step open again; Clear and Back to today (Nov 9)
check("3 the same actions as before the split, plus Tasks v2's", set(dispatch.ACTIONS) == BEFORE | V2,
      sorted(set(dispatch.ACTIONS) ^ (BEFORE | V2)))
OWNER = {"tasks": {"step_discard", "subtick", "tick_line", "step_done", "step_undo", "not_waiting", "step_wait",
                   "set_waiting", "edit_waiting", "dod_confirm", "dod_set", "prop_accept", "prop_ignore", "steps_save",
                   "notdone", "remove", "update", "auto_close_undo", "reopen", "done", "tomorrow", "next", "note",
                   "schedule", "step_box", "step_start", "actual_check",
                   "cleanup_apply", "cleanup_skip", "step_fine", "step_feedback", "cleanup_keep", "cleanup_close", "task_finish",
                   "trail_ok", "trail_undo", "step_untick", "remove_undo"},
         "planning": {"do_", "confirm", "plan", "swap", "select", "unselect", "notone"},
         "direction": {"raise", "noraise"}, "signals": {"journal_save", "meditation", "mark"},
         "outbox": {"mailprep", "sendmail", "replied", "skipreply", "skipreply_undo"}, "": {"answer", "prepare", "talk"}}
wrong = []
for name, fn in {**dispatch.CARD, **dispatch.ROW}.items():
    want = next(m for m, names in OWNER.items() if name in names)
    have = fn.__module__
    if have != (f"{PKG}.{want}.actions" if want else f"{PKG}.actions"):
        wrong.append(f"{name}: {have}")
check("3 each action is handled by the module that owns it", not wrong, wrong)

# --- 4. Do never re-ranks --------------------------------------------------------------------------------
from starlette.testclient import TestClient  # noqa: E402

TODAY = clock.do_day(__import__("datetime").datetime.now(clock.TZ))
ISO = TODAY.isoformat()
for n, prio in (("Alpha", "4-high"), ("Bravo", "3-normal"), ("Charlie", "2-low"), ("Delta", "3-normal")):
    (VAULT / "Inbox" / f"{n}.md").write_text(
        f"---\ntype: action\nstatus: open\npriority: {prio}\nscheduled: {ISO}\n---\n\n## Next action\n"
        f"- [ ] Next: do {n} (10 min)\n", encoding="utf-8")
rows = [{"n": n, "lk": n, "g": "today", "p": p, "s": f"why {n}", "e": "10m"}
        for n, p in (("Alpha", "H"), ("Bravo", "N"), ("Charlie", "Z"), ("Delta", "N"))]
(VAULT / "_system/claude/plan-today-prerun.md").write_text(
    f"---\ngenerated: {ISO}T08:00-07:00\n---\n\n```json\n" + json.dumps({"rows": [["Today", rows]]}) + "\n```\n")
client = TestClient(app.make_app(), raise_server_exceptions=False)
from jobs.checkin_app import web  # noqa: E402
client.cookies.set(web.COOKIE, web.make_cookie("s3cret"))


def act(**body):
    r = client.post("/checkin/api/act", json=body)
    return r.status_code


def do_ids():
    v = client.get("/checkin/api/view").json()
    return [r["id"] for r in (v.get("do") or {}).get("rows", [])]


committed = ["Charlie", "Alpha", "Bravo"]
st = act(action="do_commit", ids=committed)
first = do_ids()
check("4 Do shows the committed order", st == 200 and first[:3] == committed, (st, first))
ranked = [act(action="select", id="Delta", **{"from": "more"}), act(action="swap", id="Bravo"),
          act(action="notone", id="Charlie"), act(action="raise", id="Charlie"),
          act(action="update", id="Alpha", fields={"priority": "1-none"})]
after = do_ids()
check("4 ranking changes leave Do's order alone", all(s == 200 for s in ranked) and after[:3] == committed,
      (ranked, after))
st = act(action="do_add", id="Delta")
added = do_ids()
check("4 Do's own Add puts a task after the committed ones", st == 200 and added[:3] == committed and "Delta" in added,
      (st, added))
check("4 do.json holds the commitment", do_store._day(do_store._load(), TODAY)["ids"][:3] == committed)

# --- 5. compare-and-swap on every task note write ------------------------------------------------
# Every write passes the text its change was computed from; a note changed in between gets the
# change recomputed once on what is there now, and refused ("the note changed; refresh") if it
# changed again. notes.BEFORE_CHECK is the seam: called right before each compare.
import re  # noqa: E402
from jobs.checkin_app.adapters import notes  # noqa: E402

bad = []
for path in sorted(PKG_DIR.rglob("*.py")) + [ROOT / "tools" / "step.py"]:
    if path.name == "notes.py" and path.parent.name == "adapters":
        continue
    for n, ln in enumerate(path.read_text().splitlines(), 1):
        if re.search(r"\b(S|steps)\.write\(", ln) and not ln.lstrip().startswith("#"):
            bad.append(f"{path.relative_to(ROOT)}:{n}")
check("5 every task note write goes through the Notes adapter", not bad, bad)


def cas_note(name, extra=""):
    (VAULT / "Inbox" / f"{name}.md").write_text(
        f"---\ntype: action\nstatus: open\npriority: 3-normal\nscheduled: {ISO}\n---\n\n## Next action\n"
        f"DoD (Claude's guess): it is done\n- [ ] Next: first step of {name} (10 min) ^s7a01\n"
        f"- [ ] Then: second step ^s7a02\n- [ ] third step ^s7a03\n{extra}", encoding="utf-8")


def outside(times):
    left = {"n": times}

    def edit(p):
        if left["n"] > 0:
            left["n"] -= 1
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\nOutside edit {left['n']}.\n")
    return edit


CASES = [  # (tap, what the note shows when the tap's own change landed)
    ({"action": "next", "text": "a new next step"}, "Next: a new next step"),
    ({"action": "note", "text": "hello there"}, 'the owner: "hello there"'),
    ({"action": "dod_set", "text": "Truly done"}, "DoD: Truly done (the owner"),
    ({"action": "steps_save", "steps": ["first step edited", "second step"]}, "first step edited (the owner"),
    ({"action": "step_done"}, "[x] Next: first step of"),
    ({"action": "step_discard", "text": "third step", "sid": "s7a03"}, "~~third step"),
    ({"action": "tick_line", "text": "second step", "sid": "s7a02"}, "[x] Then: second step"),
    ({"action": "set_waiting", "text": "Dan"}, "Waiting: Dan"),
    ({"action": "answer", "q": "Which one?", "text": "the blue one"}, "the owner answered Claude"),
    ({"action": "replied", "text": "Sent it"}, "## Messages"),
    ({"action": "done"}, "status: done"),
]
lines = []
for k, (tap, want) in enumerate(CASES):
    name = f"Cas {k} {tap['action']}"
    cas_note(name)
    (VAULT / "_system/claude/plan-today-prerun.md").write_text(
        f"---\ngenerated: {ISO}T08:00-07:00\n---\n\n```json\n" + json.dumps({"rows": [["Today", rows + [
            {"n": name, "lk": name, "g": "today", "p": "N"}]]]}) + "\n```\n")
    notes.BEFORE_CHECK = outside(1)
    st = act(id=name, **tap)
    notes.BEFORE_CHECK = None
    text = (VAULT / "Inbox" / f"{name}.md").read_text()
    if not (st == 200 and want in text and "Outside edit 0." in text):
        lines.append(f"{tap['action']}: {st}, landed={want in text}, outside kept={'Outside edit 0.' in text}")
check("5 a note changed once underneath: the change is recomputed and both edits stay", not lines, lines)

lines = []
for k, (tap, want) in enumerate(CASES[:2] + CASES[4:7] + CASES[8:11]):
    name = f"Cas refuse {k} {tap['action']}"
    cas_note(name)
    (VAULT / "_system/claude/plan-today-prerun.md").write_text(
        f"---\ngenerated: {ISO}T08:00-07:00\n---\n\n```json\n" + json.dumps({"rows": [["Today", rows + [
            {"n": name, "lk": name, "g": "today", "p": "N"}]]]}) + "\n```\n")
    notes.BEFORE_CHECK = outside(99)
    r = client.post("/checkin/api/act", json={"id": name, **tap})
    notes.BEFORE_CHECK = None
    text = (VAULT / "Inbox" / f"{name}.md").read_text()
    if not (r.status_code == 409 and r.json().get("error") == "the note changed; refresh" and want not in text):
        lines.append(f"{tap['action']}: {r.status_code} {r.text[:80]}, landed={want in text}")
check("5 a note that keeps changing: refused, nothing of the tap written", not lines, lines)

p0 = VAULT / "Inbox" / "Cas direct.md"
cas_note("Cas direct")
base = p0.read_text()
try:
    notes._write(p0, base + "\nx\n")
    ok = False
except RuntimeError:
    ok = True
check("5 a write without the text it was computed from is refused", ok and p0.read_text() == base)
p0.write_text(base + "\nchanged\n")
out = notes.write_from(p0, base, base + "\nmine\n", lambda cur: cur + "\nmine\n")
check("5 write_from recomputes on what is there now", "changed" in out and "mine" in out, out[-40:])

# 6 (Nov 11, flag step_order): the computed step order changes the plan only, never the note. Reading
# a task with optional steps and a date gate, and proposing its piece, leaves the file byte for byte.
os.environ["CARMEN_FLAGS"] = "step_order"
from jobs.checkin_app.tasks import actions as _ta6  # noqa: E402
from jobs.checkin_app.tasks import read as _tr6  # noqa: E402
_p6 = VAULT / "Inbox" / "Order only plans.md"
_t6 = ("---\ntype: action\nstatus: open\ntask_id: t-0b6060\n---\n\n## Next action\n"
       "- [ ] Next: Get the blazer (optional) (2 h) ^s6001\n- [ ] Then: Pack the bag (30 min) ^s6002\n"
       "- [ ] Then: Send the ticket to Holiday Craft (10 min) ^s6003\n"
       "- [ ] Then: Fill in the e-Arrival card from Dec 30 (45 min) ^s6004\n\n## Log\n")
_p6.write_text(_t6, encoding="utf-8")
_o6 = _tr6.read_note(_p6).get("order") or {}
_pc6 = _ta6.piece(_p6)
check("6 the computed order puts the send first, the note untouched",
      _pc6[:1] == ["s6003"] and (_o6.get("plan") or [""])[0] == "s6003" and _p6.read_text(encoding="utf-8") == _t6, (_pc6, _o6))
os.environ.pop("CARMEN_FLAGS", None)

# 7 (Nov 11, audit A13): only committed code runs. run-job skips a job (exit 0, one push) and
# checkin-restart refuses while tracked code in the live tree differs from HEAD.
import subprocess  # noqa: E402
_lt = TMP / "livetree"
(_lt / "jobs").mkdir(parents=True)
(_lt / "bin").mkdir()
for _f in ("run-job", "live-tree-check"):
    shutil.copy(ROOT / "bin" / _f, _lt / "bin" / _f)
(_lt / "jobs" / "__init__.py").write_text("")
(_lt / "jobs" / "hello.py").write_text("print('ran')\n")
(_lt / "jobs" / "heartbeat.py").write_text("print('heartbeat ran')\n")
_g = lambda *a: subprocess.run(["git", "-C", str(_lt), *a], capture_output=True, text=True)  # noqa: E731
_g("init", "-q"); _g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"); _g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
_env = {**os.environ, "AUTOMATION_ROOT": str(_lt), "LIVE_TREE_CHECK_ANY_OS": "1", "LIVE_TREE_PUSH_LOG": str(TMP / "pushes.log"),
        "XDG_STATE_HOME": str(TMP / "xdg")}
_run = lambda job: subprocess.run([str(_lt / "bin" / "run-job"), job], capture_output=True, text=True, env=_env, cwd=str(_lt))  # noqa: E731
_r = _run("hello")
check("7 a clean tree runs the job", _r.returncode == 0 and "ran" in _r.stdout, (_r.stdout, _r.stderr))
(_lt / "jobs" / "hello.py").write_text("print('changed')\n")
_r = _run("hello")
_push = (TMP / "pushes.log").read_text() if (TMP / "pushes.log").exists() else ""
check("7 a dirty tree skips the job, exit 0, and pushes once", _r.returncode == 0 and "changed" not in _r.stdout
      and "skipped" in _r.stderr and "jobs/hello.py" in _push, (_r.stdout, _r.stderr, _push))
_run("hello")
check("7 the push is not repeated for the same change within the hour",
      (TMP / "pushes.log").read_text().count("push:") == 1, (TMP / "pushes.log").read_text())
_r = _run("heartbeat")
check("7 the heartbeat still runs on a dirty tree (it reports it)", "heartbeat ran" in _r.stdout, (_r.stdout, _r.stderr))
_r = subprocess.run([str(_lt / "bin" / "live-tree-check"), "checkin-restart"], capture_output=True, text=True, env=_env)
check("7 checkin-restart's check refuses a dirty tree", _r.returncode == 1 and "differs from HEAD" in _r.stderr, _r.stderr)
check("7 checkin-restart calls it before anything else",
      'bin/live-tree-check "checkin-restart" || fail' in (ROOT / "bin" / "checkin-restart").read_text())
from jobs import heartbeat as _hb  # noqa: E402


class _FakeJob:
    def __init__(self):
        self.checks = {}

    def record(self, *a, **k):
        pass

    def check(self, name, ok, **k):
        self.checks[name] = ok
        return ok


(_lt / "oneoff_push.py").write_text("print(1)\n")
_st = TMP / "lt_state.json"
check("7 heartbeat sees a tracked change and an untracked script, not .venv",
      any("hello.py" in x for x in _hb.live_tree_dirty(_lt)) and any("oneoff_push.py" in x for x in _hb.live_tree_dirty(_lt)))
_j = _FakeJob(); _hb._check_live_tree(_j, _lt, _st, now=1000.0)
_j2 = _FakeJob(); _hb._check_live_tree(_j2, _lt, _st, now=1000.0 + 29 * 60)
_j3 = _FakeJob(); _hb._check_live_tree(_j3, _lt, _st, now=1000.0 + 31 * 60)
_name = "the live code tree is the last commit"
check("7 heartbeat: dirty under 30 min passes (a deploy in progress), over 30 min fails",
      _j.checks[_name] and _j2.checks[_name] and not _j3.checks[_name], (_j.checks, _j2.checks, _j3.checks))
_g("checkout", "-q", "--", "jobs/hello.py"); (_lt / "oneoff_push.py").unlink()
_j4 = _FakeJob(); _hb._check_live_tree(_j4, _lt, _st, now=1000.0 + 40 * 60)
check("7 heartbeat: clean again passes and forgets", _j4.checks[_name] and not _st.exists())

# 8 (Nov 11, audit A4): his day is the Pacific date, whatever the box's clock. From 16:00 PT the box
# (UTC) is on tomorrow: the hook asked for tomorrow's Log label and vt todos showed tomorrow's tasks.
from runner import clock as _rclock  # noqa: E402
from tools import check_tasks as _CT  # noqa: E402
_note8 = ("---\ntype: action\nstatus: open\n---\n\n## Next action\n- [ ] Next: Send it (10 min) ^s8801\n"
          "- [ ] Then: Book it (10 min) ^s8802\n\n## Log\n")
for _frozen in ("2026-11-11T23:30:00Z", "2026-11-12T00:30:00Z"):   # 15:30 and 16:30 PT on Nov 11
    os.environ["AUTOMATION_NOW"], os.environ["TZ"] = _frozen, "UTC"
    check(f"8 {_frozen}: his day is Nov 11", _rclock.today().isoformat() == "2026-11-11", _rclock.today())
    _e8 = _CT.check(_note8 + "- Nov 12 · x · test\n", None, None,
                    {"old": "- [ ] Next: Send it (10 min) ^s8801", "new": "- [ ] Next: Send it (10 min) ^s8801\n- [ ] Then: Book it (10 min) ^s8802"})[0]
    _ok8 = _CT.check(_note8 + "- Nov 11 · x · test\n", None, None,
                     {"old": "- [ ] Next: Send it (10 min) ^s8801", "new": "- [ ] Next: Send it (10 min) ^s8801\n- [ ] Then: Book it (10 min) ^s8802"})[0]
    check(f"8 {_frozen}: the hook asks for today's label, Nov 11", any("`- Nov 11 ·" in e for e in _e8)
          and not any("has no line for" in e for e in _ok8), (_e8, _ok8))
(VAULT / "Inbox" / "Tomorrow task.md").write_text("---\ntype: action\nstatus: open\nscheduled: 2026-11-12\n---\n\n## Next action\n- [ ] Next: Do it (10 min)\n", encoding="utf-8")
(VAULT / "Inbox" / "Today task.md").write_text("---\ntype: action\nstatus: open\nscheduled: 2026-11-11\n---\n\n## Next action\n- [ ] Next: Do it (10 min)\n", encoding="utf-8")
_r8 = subprocess.run([sys.executable, "-m", "tools.todos", "--json"], cwd=str(ROOT), capture_output=True, text=True,
                     env={**os.environ, "AUTOMATION_NOW": "2026-11-12T00:30:00Z", "TZ": "UTC"})
_todo8 = [r.get("name") or r.get("title") or r.get("path", "") for r in json.loads(_r8.stdout or "{}").get("todo", [])]
check("8 vt todos at 16:30 PT leaves out a task scheduled tomorrow PT, keeps today's",
      any("Today task" in str(x) for x in _todo8) and not any("Tomorrow task" in str(x) for x in _todo8), (_todo8, _r8.stderr[-300:]))
os.environ.pop("AUTOMATION_NOW", None)
os.environ.pop("TZ", None)

# 9 (Nov 11): the decision gate and the git hooks. A change to a decision's test must name the id
# with its new reason and its row; a change next to a mark only warns; "row updated" with the row
# as it was at the last land is refused; an unreadable doc refuses everything (fail closed); master
# moves only by bin/land; a commit that skipped the hooks is found.
_docs9 = TMP / "docs9"
_docs9.mkdir()
_HEAD9 = "| ID | Rule | Why | Reasoning | Since | His words | Code |\n|---|---|---|---|---|---|---|\n"
(_docs9 / "checkin-app.md").write_text("## Decisions\n\n" + _HEAD9 + "| D-2 | The budget is a ceiling. | why | r | Nov 11 | w | `jobs/rule.py` |\n")
(_docs9 / "vps-vault-host.md").write_text("## Decisions\n\n" + _HEAD9 + "| S-1 | Code is edited on the box. | why | r | Nov 5 | w | none |\n")
os.environ["DECISIONS_DOC"], os.environ["DECISIONS_DOC_S"] = str(_docs9 / "checkin-app.md"), str(_docs9 / "vps-vault-host.md")
os.environ["XDG_STATE_HOME"] = str(TMP / "xdg9")
from tools import decision_gate as DG  # noqa: E402
_g9 = TMP / "gaterepo"
(_g9 / "jobs").mkdir(parents=True)
(_g9 / "bin").mkdir()
(_g9 / "tests").mkdir()
for _f in ("git-hook", "land"):   # copied without their marks: the fixture's tables have its own rows only
    shutil.copy(ROOT / "bin" / _f, _g9 / "bin" / _f)
    (_g9 / "bin" / _f).write_text(re.sub(r"(?m)^\s*# [DS]-\d+\n", "", (_g9 / "bin" / _f).read_text()))
_RULE9 = "# D-2\nBUDGET = {}\n\n\n\n\n\nOTHER = {}\n"
_TEST9 = "# D-2\ndecide(\"D-2\", BUDGET == {})\n"
(_g9 / "jobs" / "rule.py").write_text(_RULE9.format(60, 1))
(_g9 / "tests" / "checkin_decisions.py").write_text(_TEST9.format(60))
_G = lambda *a, **k: subprocess.run(["git", "-C", str(_g9), "-c", "user.email=t@t", "-c", "user.name=t", *a],  # noqa: E731
                                    capture_output=True, text=True, env={**os.environ, **k.get("env", {})})
_G("init", "-q", "-b", "master"); _G("add", "-A"); _G("commit", "-qm", "base")
_base9 = _G("rev-parse", "HEAD").stdout.strip()
(_g9 / "jobs" / "rule.py").write_text(_RULE9.format(90, 1))
_G("commit", "-qam", "Raise the budget")
_bad, _warn = DG.range_result(_base9, "HEAD", cwd=str(_g9))
check("9 a change next to # D-2 that does not name it is let through, with the rule shown as a note",
      _bad == [] and len(_warn) == 1 and _warn[0].startswith("D-2") and "budget is a ceiling" in _warn[0], (_bad, _warn))
(_g9 / "tests" / "checkin_decisions.py").write_text(_TEST9.format(90))
_G("commit", "-qam", "Raise the budget test")
_bad = DG.check_range(_base9, "HEAD", cwd=str(_g9))
check("9 a change to D-2's test that does not name it is refused, with the rule shown",
      len(_bad) == 1 and _bad[0].startswith("D-2") and "row updated" in _bad[0] and "ceiling" in _bad[0], _bad)
_G("commit", "-q", "--amend", "-m", "Raise the budget test\n\nD-2: he wants 90 min pieces now (Nov 12). D-2 row unchanged")
check("9 'row unchanged' without its why is refused", len(DG.check_range(_base9, "HEAD", cwd=str(_g9))) == 1)
_G("commit", "-q", "--amend", "-m", "Raise the budget test\n\nD-2: he wants 90 min pieces now (Nov 12). D-2 row updated.")
check("9 named with its reason and its row, it passes", DG.check_range(_base9, "HEAD", cwd=str(_g9)) == [], DG.check_range(_base9, "HEAD", cwd=str(_g9)))
_G("commit", "-q", "--amend", "-m", "Raise the budget test\n\nD-2: 90 min. D-2 row updated, D-2 row unchanged: same words")
_bad = DG.check_range(_base9, "HEAD", cwd=str(_g9))
check("9 a message saying both 'row updated' and 'row unchanged' is refused", len(_bad) == 1 and "both" in _bad[0], _bad)
DG.stamp()
_G("commit", "-q", "--amend", "-m", "Raise the budget test\n\nD-2: he wants 90 min pieces now (Nov 12). D-2 row updated.")
_bad = DG.check_range(_base9, "HEAD", cwd=str(_g9))
check("9 'row updated' while the row reads as at the last land is refused: pull the doc and retry",
      len(_bad) == 1 and "Sync" in _bad[0], _bad)
(_docs9 / "checkin-app.md").write_text((_docs9 / "checkin-app.md").read_text().replace("The budget is a ceiling.", "The budget is a ceiling of 90 min."))
check("9 once the row is edited, 'row updated' passes", DG.check_range(_base9, "HEAD", cwd=str(_g9)) == [], DG.check_range(_base9, "HEAD", cwd=str(_g9)))
DG.stamp(_G("rev-parse", "HEAD").stdout.strip())
check("9 a deploy right after the land checks the same commits against the rows before the land, and passes",
      DG.check_range(_base9, "HEAD", cwd=str(_g9)) == [], DG.check_range(_base9, "HEAD", cwd=str(_g9)))
os.environ["DECISIONS_DOC"] = str(_docs9 / "missing.md")
_bad = DG.check_range(_base9, "HEAD", cwd=str(_g9))
check("9 a decision doc that cannot be read refuses (fail closed)", len(_bad) == 1 and "cannot be read" in _bad[0], _bad)
os.environ["DECISIONS_DOC"] = str(_docs9 / "checkin-app.md")
(_g9 / "jobs" / "rule.py").write_text(_RULE9.format(90, 2))
_G("commit", "-qam", "Other")
check("9 a change far from any mark needs nothing and notes nothing",
      DG.range_result(_G("rev-parse", "HEAD~1").stdout.strip(), "HEAD", cwd=str(_g9)) == ([], []))
check("9 vt decision new gives the next free id, past rows and marks", DG.new_id("D", _g9) == "D-3" and DG.new_id("S", _g9) == "S-2",
      (DG.new_id("D", _g9), DG.new_id("S", _g9)))
_pushes9, _sp9 = [], _hb.notify.send_push
_hb.notify.send_push = lambda t, b, *a, **k: _pushes9.append(b)
_st9 = TMP / "st9"
_st9.mkdir()
_DN9 = "the decision tables and the code agree"
_jd, _jd2 = _FakeJob(), _FakeJob()
_hb._check_decisions(_jd, _g9, _st9)
_hb._check_decisions(_jd2, _g9, _st9)
(_g9 / "tests" / "checkin_decisions.py").write_text(_TEST9.format(90) + "# S-1\ndecide(\"S-1\", True)\n")
_jd3 = _FakeJob()
_hb._check_decisions(_jd3, _g9, _st9)
_hb.notify.send_push = _sp9
check("9 the morning check finds a row without a test and pushes once, not again the next day; fixed, it passes",
      _jd.checks[_DN9] is False and _jd2.checks[_DN9] is False and len(_pushes9) == 1 and "S-1" in _pushes9[0] and _jd3.checks[_DN9],
      (_jd.checks, _pushes9, DG.audit(_g9)))
_bad9 = DG.audit(_g9)
(_g9 / "jobs" / "rule.py").write_text("BUDGET = 90\n")
check("9 the morning check finds a row whose named code lost its mark", any("no D-2 mark" in b for b in DG.audit(_g9)) and _bad9 == [],
      (_bad9, DG.audit(_g9)))
(_g9 / "jobs" / "rule.py").write_text("# D-2 the budget\nBUDGET = 90\n")
check("9 the morning check finds a mark with words after its id", any("words after its id" in b for b in DG.audit(_g9)), DG.audit(_g9))
(_g9 / "jobs" / "rule.py").write_text(_RULE9.format(90, 2))
# A row written before its code lands: pending on its branch for 48 h, refused only when that branch lands without it.
_today9 = DG._pacific_today()
_doc9 = (_docs9 / "checkin-app.md").read_text()
_pend9 = lambda since: _doc9 + f"| D-3 | Parts keep their order. | why | r | Nov 12 | w | pending: feat3, {since:%b} {since.day}; `jobs/part.py` |\n"  # noqa: E731
(_docs9 / "checkin-app.md").write_text(_pend9(_today9))
_aud9 = DG.audit(_g9)
check("9 a fresh pending row needs no test or mark yet", not any("D-3" in b for b in _aud9), _aud9)
_G("checkout", "-q", "-b", "other")
(_g9 / "y.txt").write_text("1"); _G("add", "y.txt"); _G("commit", "-qm", "unrelated")
_G("checkout", "-q", "-b", "feat3", "master")
(_g9 / "z.txt").write_text("1"); _G("add", "z.txt"); _G("commit", "-qm", "the part rule, not yet")
check("9 other work lands past a pending row", DG.check_range("master", "other", cwd=str(_g9)) == [], DG.check_range("master", "other", cwd=str(_g9)))
_bad9 = DG.check_range("master", "feat3", cwd=str(_g9))
check("9 the branch a pending row names is refused without its test and mark", len(_bad9) == 1 and "pending on feat3" in _bad9[0], _bad9)
(_g9 / "jobs" / "part.py").write_text("# D-3\nPARTS = 1\n")
(_g9 / "tests" / "checkin_decisions.py").write_text(_TEST9.format(90) + "# D-3\ndecide(\"D-3\", True)\n")
_G("add", "-A"); _G("commit", "-qm", "D-3 test and mark\n\nD-3 row unchanged: it lands as written.")
check("9 with its test and mark the branch lands", DG.check_range("master", "feat3", cwd=str(_g9)) == [], DG.check_range("master", "feat3", cwd=str(_g9)))
_G("checkout", "-q", "master")
(_docs9 / "checkin-app.md").write_text(_pend9(_today9 - dt.timedelta(days=3)))
check("9 a pending row older than 48 h is flagged by the morning check", any("pending on feat3" in b for b in DG.audit(_g9)), DG.audit(_g9))
(_docs9 / "checkin-app.md").write_text(_doc9)
# The hooks, as bin/install-hooks installs them (here through core.hooksPath on the fixture).
_hk9 = TMP / "hooks9"
_hk9.mkdir()
for _h in ("pre-commit", "commit-msg", "post-commit", "pre-merge-commit", "post-merge", "pre-receive", "reference-transaction"):
    (_hk9 / _h).write_text(f'#!/bin/sh\nexec "{_g9}/bin/git-hook" {_h} "$@"\n')
    (_hk9 / _h).chmod(0o755)
_G("config", "core.hooksPath", str(_hk9))
_env9 = {"XDG_STATE_HOME": str(TMP / "xdg9"), "GIT_HOOK_SUITES": "", "PYTHONPATH": str(ROOT)}
shutil.copytree(ROOT / "tools", _g9 / "tools", ignore=shutil.ignore_patterns("__pycache__", "canvas", "browser", "carmen_qa"))
(_g9 / "x.txt").write_text("1")
_G("add", "x.txt")
_r9 = _G("commit", "-qm", "on master", env=_env9)
check("9 a commit on master is refused", _r9.returncode != 0 and "worktree branch" in _r9.stderr, _r9.stderr)
_G("checkout", "-q", "-b", "work", env=_env9)
_r9 = _G("commit", "-qm", "on a branch", env=_env9)
_stamps9 = (TMP / "xdg9" / "automation" / "gate_stamps")
_h9 = _G("rev-parse", "HEAD").stdout.strip()
check("9 a commit on a branch passes the hooks and is stamped", _r9.returncode == 0 and _stamps9.exists() and _h9 in _stamps9.read_text(),
      (_r9.stderr, _stamps9.read_text() if _stamps9.exists() else ""))
(_g9 / "tests" / "checkin_decisions.py").write_text(_TEST9.format(120))
_G("add", "tests/checkin_decisions.py")
_r9 = _G("commit", "-qm", "Bigger budget", env=_env9)
check("9 the commit-msg hook refuses a decision change that does not name it", _r9.returncode != 0 and "D-2" in _r9.stderr, _r9.stderr)
_r9 = _G("commit", "-q", "--no-verify", "-m", "Bigger budget, skipping the hooks", env=_env9)
_nv = _G("rev-parse", "HEAD").stdout.strip()
check("9 --no-verify gets through but leaves no stamp", _r9.returncode == 0 and _nv not in _stamps9.read_text())
from jobs import heartbeat as _hb9  # noqa: E402
_G("checkout", "-q", "master", env=_env9)
_G("merge", "-q", "--ff-only", "work", env={**_env9, "LAND": "1"})
check("9 the heartbeat finds the unstamped commit on master",
      _nv in _hb9.unstamped(_g9, _stamps9, _base9) and _h9 not in _hb9.unstamped(_g9, _stamps9, _base9),
      _hb9.unstamped(_g9, _stamps9, _base9))
_pr9 = subprocess.run([str(_g9 / "bin" / "git-hook"), "pre-receive"], input=f"{_base9} {_h9} refs/heads/master\n",
                      capture_output=True, text=True, env={**os.environ, **_env9}, cwd=str(_g9))
check("9 a push to master is refused", _pr9.returncode == 1 and "bin/land" in _pr9.stderr, _pr9.stderr)

shutil.rmtree(TMP, ignore_errors=True)
print("checks passed" if not check.fails else f"{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
