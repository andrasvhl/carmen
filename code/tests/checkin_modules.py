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
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG, PKG_DIR = "jobs.checkin_app", ROOT / "jobs" / "checkin_app"
MODULES = ("tasks", "planning", "direction", "signals", "outbox")
INFRA_FOR_MODULES = ("adapters", "runs", "chat", "alerts")   # what a module may use besides modules
sys.path.insert(0, str(ROOT))

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
          "do_more", "do_unmove", "dod_confirm", "dod_set", "done", "edit_waiting", "journal_save", "mailprep", "mark",
          "meditation", "next", "noraise", "not_waiting", "notdone", "note", "notone", "plan", "prepare",
          "prop_accept", "prop_ignore", "raise", "remove", "reopen", "replied", "schedule", "select", "sendmail",
          "set_waiting", "skipreply", "step_discard", "step_done", "step_undo", "step_wait", "steps_save", "subtick",
          "swap", "talk", "tick_line", "tomorrow", "unselect", "update"}
# Tasks v2 (Nov 6) adds its own taps, each in the module that owns what it changes.
V2 = {"step_box", "do_outcome", "step_start", "actual_check", "cleanup_apply", "cleanup_skip", "step_fine", "step_feedback", "do_chunk_feedback", "cleanup_keep", "cleanup_close"}
check("3 the same actions as before the split, plus Tasks v2's", set(dispatch.ACTIONS) == BEFORE | V2,
      sorted(set(dispatch.ACTIONS) ^ (BEFORE | V2)))
OWNER = {"tasks": {"step_discard", "subtick", "tick_line", "step_done", "step_undo", "not_waiting", "step_wait",
                   "set_waiting", "edit_waiting", "dod_confirm", "dod_set", "prop_accept", "prop_ignore", "steps_save",
                   "notdone", "remove", "update", "auto_close_undo", "reopen", "done", "tomorrow", "next", "note",
                   "schedule", "step_box", "step_start", "actual_check",
                   "cleanup_apply", "cleanup_skip", "step_fine", "step_feedback", "cleanup_keep", "cleanup_close"},
         "planning": {"do_", "confirm", "plan", "swap", "select", "unselect", "notone"},
         "direction": {"raise", "noraise"}, "signals": {"journal_save", "meditation", "mark"},
         "outbox": {"mailprep", "sendmail", "replied", "skipreply"}, "": {"answer", "prepare", "talk"}}
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
    ({"action": "set_waiting", "text": "Jose"}, "Waiting: Jose"),
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

shutil.rmtree(TMP, ignore_errors=True)
print("checks passed" if not check.fails else f"{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
