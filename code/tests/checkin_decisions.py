#!/usr/bin/env python3
"""Decision tests: one check per rule the owner decided (checkin-app.md § Decisions, D-n). Offline, fast.

    .venv/bin/python -m tests.checkin_decisions

Every check runs both ways: the rule holds, and the rejected alternative fails (a check that would
pass with the rule deleted checks nothing). Server rules (S-n, vps-vault-host.md § Decisions) are
checked here too. A failing check prints its row from the table: the rule, why, and his words, so whoever is
changing the behaviour sees the reason first (Nov 11, the owner: "regressions happening, often because
you don't remember the original rule why things were implemented (and sometimes its right to change
them, but you need to know the context)"). Changing a decision is fine: change its row, its # D-n
code and this check in one deploy, and name the D-n with the new reason in the commit message
(tools/decision_gate.py enforces that).
"""
import datetime as dt
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
# Run from a git hook, GIT_DIR and GIT_INDEX_FILE point at the real repo: a fixture `git init` then
# rewrote its config (Nov 12, core.bare). Fixture repos get a clean git environment.
for _k in [k for k in os.environ if k.startswith("GIT_")]:
    os.environ.pop(_k)
sys.path.insert(0, str(ROOT))
# The tables, read from the real vault before the scratch HOME below, through the gate's one
# parser (no copy of the rows here). A doc that cannot be read fails the run (fail closed).
from tools import decision_gate as DG  # noqa: E402
try:
    _T, _RETIRED, _DUPS = DG.table_rows()
    ROWS = {k: {"rule": v.get("rule", ""), "why": v.get("why", ""), "since": v.get("since", ""), "words": v.get("his words", "")}
            for k, v in _T.items()}
    _DOC_ERR = ""
except DG.DocMissing as _e:
    ROWS, _RETIRED, _DUPS, _DOC_ERR = {}, set(), [], str(_e)
_MARKS, _PROSE = DG.all_marks(ROOT), DG.prose_marks(ROOT)
# plan-today's panel rows (a vault file), for D-22's one hour.
PANEL_ROWS = next((p for p in (os.path.expanduser("~/vault/_system/skills/plan-today/panel_rows.py"),
                               os.path.expanduser("~/Obsidian/<VAULT_NAME>/_system/skills/plan-today/panel_rows.py"))
                   if os.path.isfile(p)), "")
TMP = pathlib.Path(tempfile.mkdtemp(prefix="carmen-decisions-"))
for d in ("vault/Inbox", "home"):
    (TMP / d).mkdir(parents=True, exist_ok=True)
os.environ["VAULT_PATH"], os.environ["HOME"] = str(TMP / "vault"), str(TMP / "home")
os.environ["STEPS_DIR"] = str(TMP / "home" / "steps")
os.environ["CARMEN_FLAGS"] = "estimator"

from runner import config  # noqa: E402
assert str(config.STATE_DIR).startswith(str(TMP)), "not isolated"
from tools import steps as S  # noqa: E402

FAILS = []
T = dt.date(2026, 11, 11)


def decide(did, ok, detail=""):
    if ok:
        print(f"  ok    {did}")
        return
    r = ROWS.get(did, {})
    FAILS.append(did)
    print(f"  FAIL  {did} {r.get('rule', '(row not found in checkin-app.md § Decisions)')}\n"
          f"        why: {r.get('why', '')}\n        since: {r.get('since', '')}  his words: {r.get('words', '')}\n"
          f"        got: {str(detail)[:300]}")


def steps(body):
    return S.steps_of("## Next action\n" + "\n".join(body))


def dicts(body, today=T):
    return [{"id": x.id, "kind": x.kind, "state": x.state, "text": x.display(), "after": x.after,
             **({"min": x.minutes} if x.minutes is not None else {}), **({"due": x.due} if x.due else {}),
             **({"optional": True} if x.optional else {}), **({"gate": x.gate} if x.gate else {})}
            for x in S.steps_of("## Next action\n" + "\n".join(body), today) if x.id]


NOTE = "---\ntype: action\nstatus: open\ntask_id: t-dddddd\n---\n\n## Next action\n{}\n\n## Log\n"

# D-1 a write re-estimates only the lines it adds or rewords.
from jobs.checkin_app.tasks import estimates as E  # noqa: E402
_t0 = NOTE.format("- [ ] Next: Read the case (40 min) ^s0001\n- [ ] Then: Pay the deposit (10 min) ^s0002\n- [ ] Then: Book the room (20 min) ^s0003")
_prev = S._snap(S.steps_of(_t0.partition("\n---\n")[2], T))
_t1 = _t0.replace("Pay the deposit (10 min)", "Pay the deposit by card (10 min)")
_out, _ev = E.on_write(_prev, _t1, "chat", T, stem="Decisions D1")
_est = {x.id: x.est for x in S.steps_of(_out.partition("\n---\n")[2], T)}
# Both ways: the untouched lines keep theirs, the reworded one is estimated again.
decide("D-1", _est["s0001"] == "40 min" and _est["s0003"] == "20 min" and {e["step"] for e in _ev} == {"s0002"}, (_est, _ev))

# D-2, D-3, D-4, D-5 the piece.
_fin = ["- [ ] Next: Join the Lakeside video visit (20 min) 📅 2026-11-13 ^s859c",
        "- [ ] Then: Pick up at Walgreens at Lakeside (after: ^s859c) (20 min) 📅 2026-11-16 ^s2b29",
        "- [ ] Then: Pack it in the original labelled bottle (10 min) ^s5459"]
_due2 = [_fin[0], "- [ ] Then: Pay the Lakeside bill (20 min) 📅 2026-11-11 ^s2b29", _fin[2]]
# Both ways: with room left nothing joins without a reason; a step due today joins for its reason.
decide("D-2", E.piece_from(dicts(_fin), T)["piece"] == ["s859c"] and E.piece_from(dicts(_due2), T)["piece"] == ["s2b29", "s859c"],
       (E.piece_from(dicts(_fin), T)["piece"], E.piece_from(dicts(_due2), T)["piece"]))
_ride = ["- [ ] Next: Buy travel insurance (65 min) 📅 2026-11-17 ^s6e28",
         "- [ ] Then: Send the IndiGo ticket to Holiday Craft (10 min) ^se8d0",
         "- [ ] Then: Answer the Jaipur train or road poll by 17:00 (5 min) 📅 2026-11-27 ^s060d"]
_pr = E.piece_from(dicts(_ride), T)
decide("D-3", _pr["piece"] == ["s6e28", "se8d0"], _pr["piece"])
_long = ["- [ ] Next: the e-bike online classroom (6 h) 📅 2026-11-13 ^s2dbe"]
decide("D-4", E.piece_from(dicts(_long), T)["parts"] == {"s2dbe": 60}
       and E.piece_from(dicts(_long), T, room=400, high=True)["parts"].get("s2dbe", 360) > 60, E.piece_from(dicts(_long), T, room=400, high=True))
# Both ways: the computed order differs from the file's (soonest date first), and the lines stay put.
_rev5 = ["- [ ] Next: Answer the Jaipur train or road poll by 17:00 (5 min) 📅 2026-11-27 ^s060d",
         "- [ ] Then: Book the room (30 min) ^se8d0", "- [ ] Then: Buy travel insurance (65 min) 📅 2026-11-17 ^s6e28"]
_in5 = dicts(_rev5)
_before = [dict(x) for x in _in5]
_o5 = E.rank(_in5, T)["order"]
decide("D-5", _o5 == ["s6e28", "s060d", "se8d0"] and _in5 == _before, _o5)

# D-6 optional.
_o = S.parse_line("- [ ] Then: Get the blazer (optional) (2 h) ^s3d97", T)
_closed = S.normalize(NOTE.replace("status: open", "status: done").format("- [ ] Then: Get the blazer (optional) (2 h) ^s3d97"), T)
decide("D-6", _o.optional and not S.parse_line("- [ ] Then: Get the blazer (2 h) ^s3d97", T).optional
       and "(not needed, Nov 11)" in _closed, _closed)

# D-7 his own Done promotes a step: it never joins the frozen chunk.
from jobs.checkin_app.planning import do as D  # noqa: E402
_e = {"at_commit": {"T": {"next": "s1", "due": []}}}
_st = {"status": "open", "next_id": "s2", "blocked": [], "step_states": [{"id": "s1", "state": "done", "kind": "Next"},
                                                                          {"id": "s2", "state": "open", "kind": "Next"}]}
# Both ways: a new Next that came up while the part is open joins, said as added after the plan.
_st7 = {"status": "open", "next_id": "s3", "blocked": [], "step_states": [{"id": "s1", "state": "open", "kind": "Then"},
                                                                          {"id": "s3", "state": "open", "kind": "Next"}]}
decide("D-7", D._after_plan(_e, "T", _st, ["s1"], T) == [] and D._after_plan(_e, "T", _st7, ["s1"], T) == ["s3"],
       (D._after_plan(_e, "T", _st, ["s1"], T), D._after_plan(_e, "T", _st7, ["s1"], T)))

# D-8 the card's time follows the live event.
_eb = {"block_steps": [{"task": "T", "s": 780, "f": 825, "sids": ["s1"], "key": "ev-00000001"}]}
decide("D-8", D.live_blocks(_eb, "T", [{"key": "ev-00000001", "s": 840, "f": 885}])[0]["s"] == 840
       and D.live_blocks(_eb, "T", []) == [] and D.live_blocks(_eb, "T", None)[0]["s"] == 780)

# D-9 waits per step.
_w = steps(["- [ ] Waiting: Priya · for: the options ^s9001", "- [ ] Next: Send the ticket (10 min) ^s9002"])
_w2 = steps(["- [ ] Waiting: Priya · for: the options ^s9001", "- [ ] Next: Pick the hotel (after: ^s9001) (20 min) ^s9002"])
decide("D-9", not S.task_waiting(_w) and S.task_waiting(_w2)
       and S.waits_on_someone(S.parse_line("- [ ] Then: Check the portal for Lakeside's reply (5 min) ^s9003")) == "Lakeside")

# D-10 fields in their order, one module.
_line = "- [ ] Then: Pick the hotel (after: ^s9001) (optional) (20 min) 📅 2026-11-13 ^s9004"
# One module owns the rules: no month list, estimate, deadline-word or (due …) pattern outside tools/steps.py
# in tools/ and jobs/, and one NOMIN and one EST_ANY on the page (Nov 11).
_pat = {"month list": re.compile(r"Jan\|Feb|jan\|feb|[\"']Jan[\"'],\s*[\"']Feb|Jan Feb Mar|jan feb mar"),
        "estimate pattern": re.compile(r"min\\\)|\(\?:min\|h\)|\(min\|h\)|min\|mins"),
        "deadline words": re.compile(r"by\|before|before\|until"),
        "(due marker": re.compile(r"\\\(due ")}
_copies = []
for _p in sorted(list((ROOT / "tools").rglob("*.py")) + list((ROOT / "jobs").rglob("*.py"))):
    if _p.relative_to(ROOT).as_posix() in ("tools/steps.py", "tools/decision_gate.py") or "__pycache__" in _p.parts:
        continue
    for _n, _l in enumerate(_p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        _copies += [f"{_p.relative_to(ROOT)}:{_n} {k}" for k, rx in _pat.items() if rx.search(_l)]
_page = (ROOT / "jobs" / "checkin_app" / "page.html").read_text(encoding="utf-8")
decide("D-10", S.format_line(S.parse_line(_line, T)) == _line and not _copies
       and len(re.findall(r"\bconst NOMIN\s*=", _page)) == 1 and len(re.findall(r"\bconst EST_ANY\s*=", _page)) == 1,
       (S.format_line(S.parse_line(_line, T)), _copies[:6]))

# D-11 a hand edit of the step lines is put back.
from tools import check_tasks as CT  # noqa: E402
_p11 = TMP / "vault" / "Inbox" / "D11.md"
_t11 = NOTE.format("- [ ] Next: Send the ticket (10 min) ^s1101")
_e11 = _t11.replace("Send the ticket", "Send the ticket copy")
_r11 = CT._raw_block_edit("Edit", {"old_string": "Send the ticket (10 min)", "new_string": "Send the ticket copy (10 min)"}, _e11, _p11)
_n11 = _t11 + "Some notes\n"
_r11b = CT._raw_block_edit("Edit", {"old_string": "Some notes", "new_string": "Some more notes"}, _n11.replace("Some notes", "Some more notes"), _p11)
decide("D-11", _r11 is not None and _r11[1] == _t11 and _r11b is None, (_r11, _r11b))

# D-12 his line is only proposed.
from jobs import plan_today_prerun as PR  # noqa: E402
_l12 = ["## Next action", "- [ ] Then: Keep the address, 12 Marine Dr (owner decision, Nov 9) (5 min) ^s1201", ""]
_row = {"rf": [{"step": "Keep the address, 12 Marine Dr (owner decision, Nov 9)", "why": "an address"}]}
PR._optional_and_reference(_l12, _row, [])
_l12b = ["## Next action", "- [ ] Then: Keep the address, 12 Marine Dr (5 min) ^s1201", ""]
PR._optional_and_reference(_l12b, {"rf": [{"step": "Keep the address, 12 Marine Dr", "why": "an address"}]}, [])
# Both ways: his line stays and gets a proposal; the same line without (the owner is moved.
decide("D-12", "- [ ] Then: Keep the address" in "\n".join(_l12) and _row.get("prop")
       and "- [ ] Then: Keep the address" not in "\n".join(_l12b), (_l12, _l12b))

# D-13 Done brings up the next free step, never an optional one.
from jobs.checkin_app.tasks import steps as AS  # noqa: E402
_t13 = NOTE.format("- [ ] Next: Send the ticket (10 min) ^s1301\n- [ ] Then: Get the blazer (optional) (2 h) ^s1302\n- [ ] Then: Pack the bag (30 min) ^s1303")
_n13, _res13 = AS.step_done(_t13, T, "Nov 11")
decide("D-13", _res13.get("next", "").startswith("Pack the bag"), _res13)

# D-14 a closed task strikes its open steps; reopening restores them.
_c14 = S.normalize(NOTE.replace("status: open", "status: done").format("- [ ] Then: Pack the bag (30 min) ^s1401"), T)
decide("D-14", "(closed with the task, Nov 11)" in _c14 and "- [ ] Then: Pack the bag" in S.normalize(_c14.replace("status: done", "status: open"), T))

# D-15 the day turns at 04:00.
from jobs.checkin_app.adapters import clock as AC  # noqa: E402
decide("D-15", AC.carmen_day(dt.datetime(2026, 11, 12, 0, 30, tzinfo=AC.TZ)) == dt.date(2026, 11, 11)
       and AC.carmen_day(dt.datetime(2026, 11, 12, 4, 30, tzinfo=AC.TZ)) == dt.date(2026, 11, 12))

# D-16 the DoD line.
from jobs.checkin_app.tasks import read as TR  # noqa: E402
_d16 = TR._dod("## Next action\nDoD (Claude's guess): the ticket sent\n- [ ] Next: Send it (10 min)\n")
_d16b = TR._dod("## Next action\nDoD: the ticket sent (owner decision, Nov 9)\n- [ ] Next: Send it (10 min)\n")
decide("D-16", _d16.get("dod_guess") and "ticket sent" in _d16.get("dod", "") and _d16b.get("dod_guess") is False, (_d16, _d16b))

# D-17 steps live only in the block; the build brief's form is exempt.
_s17 = NOTE.format("- [ ] Next: Send it (10 min) ^s1701") + "\n## Steps\n- [ ] Loose box\n"
decide("D-17", S.stray_boxes(_s17) == ["- [ ] Loose box"]
       and S.stray_boxes(_s17.replace("task_id: t-dddddd\n", "task_id: t-dddddd\ntags:\n  - build-brief\n")) == [])

# D-18 dependencies: a result step and part N need (after:).
_e18 = S.earlier_needed(steps(["- [ ] Next: Join the visit (20 min) ^s1801", "- [ ] Then: Pick up the parcel (20 min) ^s1802",
                               "- [ ] Then: the e-bike part 1 of 2 (2 h) ^s1803", "- [ ] Then: the e-bike part 2 of 2 (2 h) ^s1804",
                               "- [ ] Then: Book the hotel (20 min) ^s1805"]))
decide("D-18", _e18.get("s1802") == "s1801" and _e18.get("s1804") == "s1803" and "s1805" not in _e18 and "s1803" not in _e18, _e18)

# D-19 a chat's write to the live code tree asks.
from jobs.tg_bridge import rulebook_gate as RG  # noqa: E402
decide("D-19", "live code" in RG.edit_reason("Edit", {"file_path": str(RG.HOME / "automation" / "jobs" / "x.py")})
       and RG.code_reason("cat ~/automation/jobs/x.py") == "")

# D-20 his day is the Pacific date.
os.environ["AUTOMATION_NOW"], os.environ["TZ"] = "2026-11-12T00:30:00Z", "UTC"
from runner import clock as RC  # noqa: E402
_t20 = RC.today()
os.environ["AUTOMATION_NOW"] = "2026-11-12T08:30:00Z"
decide("D-20", _t20 == dt.date(2026, 11, 11) and RC.today() == dt.date(2026, 11, 12), (_t20, RC.today()))
os.environ.pop("AUTOMATION_NOW", None)

# D-21 a close deletes future blocks, keeps past and running ones; a reopen restores them.
from jobs.checkin_app.adapters import calendar as CAL  # noqa: E402
from jobs.checkin_app.tasks import blocks as BL  # noqa: E402
_n21 = dt.datetime(2026, 11, 11, 15, 0, tzinfo=AC.TZ)
_live = {"p": {"id": "p", "summary": "past", "description": "Task: x (t-d21d21)", "start": {"dateTime": "2026-11-11T13:00:00-08:00"}},
         "f": {"id": "f", "summary": "future", "description": "Task: x (t-d21d21)", "start": {"dateTime": "2026-11-12T15:00:00-08:00"}}}
_o = (CAL.future_blocks, CAL.delete_event, CAL.insert_event)
CAL.future_blocks = lambda tid, now: [{"cal": "c", "event": e} for e in _live.values() if dt.datetime.fromisoformat(e["start"]["dateTime"]) > now]
CAL.delete_event = lambda cal, eid: _live.pop(eid)
CAL.insert_event = lambda cal, e: _live.__setitem__(e["id"], e)
try:
    BL.on_close("t-d21d21", _n21)
    _gone = sorted(_live)
    BL.on_reopen("t-d21d21", _n21)
    decide("D-21", _gone == ["p"] and sorted(_live) == ["f", "p"], (_gone, sorted(_live)))
finally:
    CAL.future_blocks, CAL.delete_event, CAL.insert_event = _o

# D-22 planning tomorrow starts at 19:00, the same hour for Carmen's page and plan-today.
_pg22 = (ROOT / "jobs" / "checkin_app" / "page.html").read_text(encoding="utf-8")
_rows22 = open(PANEL_ROWS, encoding="utf-8").read() if PANEL_ROWS else ""
decide("D-22", RC.PLAN_TOMORROW_HOUR == 19 and f"const PLAN_TOMORROW_HOUR = {RC.PLAN_TOMORROW_HOUR};" in _pg22
       and "h >= PLAN_TOMORROW_HOUR" in _pg22
       and (not PANEL_ROWS or ("from runner.clock import PLAN_TOMORROW_HOUR" in _rows22 and "EVENING_HOUR = 18" not in _rows22)),
       (RC.PLAN_TOMORROW_HOUR, PANEL_ROWS))

# D-23 Do never re-ranks and holds only what he committed; a task due that day is Suggested, joining only through Plan.
_r23 = {i: {"id": i, "title": i} for i in ("A23", "B23", "X23")}
_r23["X23"]["due"] = T.isoformat()
_n23 = lambda i: {"status": "open", "step_states": [], "next": "Do it", "next_id": ""}  # noqa: E731
D.commit(T, ["B23", "A23"])
_v23 = D.view(T, _r23, [], [_r23["X23"]], [_r23["X23"], _r23["A23"], _r23["B23"]], [], _n23, _r23.get, lambda d: d.isoformat())
decide("D-23", [r["id"] for r in _v23["rows"]] == ["B23", "A23"] and [r["id"] for r in _v23["suggest"]] == ["X23"],
       ([r["id"] for r in _v23["rows"]], [r["id"] for r in _v23["suggest"]]))

# D-24 a block belongs to a task by its task id, never by its title.
from jobs.checkin_app.adapters import calendar as CAL24  # noqa: E402
_d24 = "Task: obsidian://open?vault=V&file=Inbox%2FOther (t-c3c3c3)"
_ev24 = [{"s": 600, "f": 660, "title": "Book the Jaipur hotel", "desc": "", "task_ids": [], "tasks": []},
         {"s": 780, "f": 825, "title": "Focus", "desc": _d24, "task_ids": CAL24.task_ids(_d24), "tasks": []}]
_b24 = D.block_for("Book the Jaipur hotel", "t-c3c3c3", _ev24)
decide("D-24", D.block_for("Book the Jaipur hotel", "t-aaaaaa", _ev24) is None and _b24 and _b24["s"] == 780, _b24)

# D-25 a fully waiting task is never Suggested, today or tomorrow; a partly waiting one is.
from jobs.checkin_app.planning import read as PRD  # noqa: E402
_N25 = "---\ntype: action\nstatus: open\ntask_id: {}\npriority: high\n---\n\n## Next action\n{}\n\n## Log\n"
(config.INBOX / "D25 full.md").write_text(_N25.format("t-d25d01", "- [ ] Waiting: Priya · for: the options ^s2501\n- [ ] Next: Pick the hotel (after: ^s2501) (20 min) ^s2502"))
(config.INBOX / "D25 part.md").write_text(_N25.format("t-d25d02", "- [ ] Waiting: Priya · for: the options ^s2503\n- [ ] Next: Send the ticket (10 min) ^s2504"))
_v25 = PRD.build_view({"rows": [["Today", [{"lk": "D25 full", "n": "D25 full", "p": "H"}, {"lk": "D25 part", "n": "D25 part", "p": "H"}]]]}, T)
_s25 = lambda side: [r["id"] for r in ([_v25[side]["one"]] if _v25[side]["one"] else []) + _v25[side]["needs"]]  # noqa: E731
decide("D-25", _s25("today") == _s25("tomorrow") == ["D25 part"] and [r["id"] for r in _v25["today"]["waiting"]] == ["D25 full"],
       (_s25("today"), _s25("tomorrow")))
for _f25 in ("D25 full.md", "D25 part.md"):
    (config.INBOX / _f25).unlink()

# D-26 rank keys freeze at the first persisting showing that day; a new day ranks afresh.
from jobs.checkin_app.adapters import state as ST26  # noqa: E402
_rows26 = [{"id": "A26"}, {"id": "B26"}]
with ST26.persist():
    _k1 = PRD.frozen_keys(lambda r: (0 if r["id"] == "B26" else 1,), _rows26, "today", T)
_k2 = PRD.frozen_keys(lambda r: (0 if r["id"] == "A26" else 1,), _rows26, "today", T)
_k3 = PRD.frozen_keys(lambda r: (7,), [{"id": "A26"}], "today", T + dt.timedelta(days=1))
decide("D-26", _k1 == _k2 == {"A26": (1,), "B26": (0,)} and _k3 == {"A26": (7,)}, (_k1, _k2, _k3))

# D-27 a page load only reads: building the view without persist writes nothing; with it, it may.
import hashlib  # noqa: E402
_N27 = NOTE.replace("status: open", f"status: open\npriority: high\nscheduled: {T.isoformat()}")
(config.INBOX / "D27 a.md").write_text(_N27.replace("t-dddddd", "t-d27d01").format("- [ ] Next: Send the ticket ^s2701"))
(config.INBOX / "D27 b.md").write_text(_N27.replace("t-dddddd", "t-d27d02").format("- [ ] Next: Pack the bag (30 min)"))
_snap27 = lambda: {str(q): hashlib.sha1(q.read_bytes()).hexdigest() for q in TMP.rglob("*")  # noqa: E731
                   if q.is_file() and q.name not in ("taps.jsonl", "alerts.json") and not q.name.endswith((".lock", ".tmp"))}
_d27 = {"rows": [["Today", [{"lk": "D27 a", "n": "D27 a"}, {"lk": "D27 b", "n": "D27 b"}]]]}
PRD.build_view({"rows": []}, T)
_b27 = _snap27()
PRD.build_view(_d27, T)
PRD.build_view(_d27, T)
_a27 = _snap27()
with ST26.persist():
    PRD.build_view(_d27, T)
_c27 = _snap27()
decide("D-27", _a27 == _b27 and _c27 != _a27, sorted(k for k in set(_a27) | set(_b27) if _a27.get(k) != _b27.get(k))[:5])

# D-28 the pre-run never changes a note's frontmatter or its DoD line: such a change is skipped whole.
import json as _js28, types as _ty28  # noqa: E401,E402
_N28 = ("---\ntype: action\nstatus: open\ntask_id: t-d28d28\npriority: 2-high\nscheduled: 2026-11-12\ndue: 2026-11-14\n---\n\n"
        "## Next action\nDoD: the hotel is booked and paid\n- [ ] Next: Compare three hotels (30 min) ^s2801\n- [ ] Then: Pay the deposit (10 min) ^s2802\n\n## Log\n")
_row28 = {"lk": "D28", "n": "D28", "nx": "Book the Jaipur hotel", "anx": 1, "g": "today",
          "rw": [{"old": "Then: Pay the deposit (10 min)", "new": "Pay the deposit by card"}, {"old": "DoD: the hotel is booked and paid", "new": "x"}]}
_p28 = config.INBOX / "D28.md"
_p28.write_text(_N28)
_sr28 = PR.subprocess.run
PR.subprocess.run = lambda *a, **k: _ty28.SimpleNamespace(stdout=_js28.dumps({"all": [{"name": "D28", "path": str(_p28)}]}))
try:
    _fm28 = PR.apply_to_tasks({"rows": [["T", [{**_row28, "ev": "ev-0000abcd"}]]]}, dt.datetime(2026, 11, 11, 8, tzinfo=PR.TZ), False)
    _same28 = _p28.read_text() == _N28
    _rep28 = PR.apply_to_tasks({"rows": [["T", [dict(_row28)]]]}, dt.datetime(2026, 11, 11, 8, tzinfo=PR.TZ), False)
finally:
    PR.subprocess.run = _sr28
_t28 = _p28.read_text()
decide("D-28", _fm28 == [] and _same28 and _rep28 and _t28 != _N28 and _t28.split("\n---\n")[0] == _N28.split("\n---\n")[0]
       and "DoD: the hotel is booked and paid" in _t28, (_fm28, _rep28))

# S-1 the Mac's ~/automation only fast-forwards: local edits or a diverged branch stop it.
import subprocess  # noqa: E402
_t1 = TMP / "s1"
_o1, _m1 = _t1 / "origin", _t1 / "mac" / "automation"
(_t1 / "fake").mkdir(parents=True)
(_t1 / "fake" / "uname").write_text("#!/bin/sh\necho Darwin\n")
(_t1 / "fake" / "uname").chmod(0o755)
_g1 = lambda d, *a: subprocess.run(["git", "-C", str(d), "-c", "user.email=t@t", "-c", "user.name=t", *a], capture_output=True, text=True)  # noqa: E731
_o1.mkdir()
_g1(_o1, "init", "-q", "-b", "master"); (_o1 / "f").write_text("1"); _g1(_o1, "add", "-A"); _g1(_o1, "commit", "-qm", "base")
subprocess.run(["git", "clone", "-q", str(_o1), str(_m1)], capture_output=True)
(_o1 / "a").write_text("1"); _g1(_o1, "add", "a"); _g1(_o1, "commit", "-qm", "box commit")
_run1 = lambda: subprocess.run(["bash", str(ROOT / "bin" / "mac-sync")], capture_output=True, text=True,  # noqa: E731
                               env={**os.environ, "HOME": str(_t1 / "mac"), "PATH": f"{_t1 / 'fake'}:{os.environ['PATH']}"}).stdout
_head1 = lambda: _g1(_m1, "log", "-1", "--format=%s").stdout.strip()  # noqa: E731
(_m1 / "f").write_text("edited")
_dirty1 = _run1(); _h1 = _head1()
_g1(_m1, "checkout", "-q", "--", "f")
_run1(); _h2 = _head1()
(_m1 / "b").write_text("1"); _g1(_m1, "add", "b"); _g1(_m1, "commit", "-qm", "mac commit")
(_o1 / "c").write_text("1"); _g1(_o1, "add", "c"); _g1(_o1, "commit", "-qm", "box commit 2")
_div1 = _run1(); _h3 = _head1()
decide("S-1", "locally edited" in _dirty1 and _h1 == "base" and _h2 == "box commit" and "cannot fast-forward" in _div1 and _h3 == "mac commit",
       (_dirty1, _h1, _h2, _div1, _h3))

# S-2 packages follow requirements.txt: installed when it changes, not otherwise, retried after a failure.
_t2 = TMP / "s2"
_a2 = _t2 / "automation"
(_a2 / ".venv" / "bin").mkdir(parents=True)
(_a2 / ".venv" / "bin" / "python").write_text('#!/bin/sh\necho "$@" >> "$(dirname "$0")/../calls"\n'
                                              '[ -n "${FAIL_PIP:-}" ] && [ "$3" = install ] && exit 1\nexit 0\n')
(_a2 / ".venv" / "bin" / "python").chmod(0o755)
(_a2 / "requirements.txt").write_text("requests==2.32.3\n")
_ds2 = lambda **e: subprocess.run(["bash", str(ROOT / "bin" / "deps-sync")], capture_output=True, text=True,  # noqa: E731
                                  env={**os.environ, "HOME": str(_t2), **e}).stdout
_calls2 = lambda: (_a2 / ".venv" / "calls").read_text() if (_a2 / ".venv" / "calls").exists() else ""  # noqa: E731
_ds2(); _c21 = _calls2(); (_a2 / ".venv" / "calls").write_text("")
_ds2(); _c22 = _calls2()
(_a2 / "requirements.txt").write_text("requests==2.32.3\ntrafilatura==2.0.0\n")
_ds2(FAIL_PIP="1"); (_a2 / ".venv" / "calls").write_text("")
_ds2(); _c23 = _calls2()
decide("S-2", "-r requirements.txt" in _c21 and "check" in _c21 and _c22 == "" and "-r requirements.txt" in _c23
       and "deps-sync" in (ROOT / "bin" / "run-job").read_text(), (_c21, _c22, _c23))

# S-3 in a Carmen chat a calendar event without guests needs no tap; any other connector write asks.
from jobs.checkin_app import chat as CH3  # noqa: E402
_CE3 = "mcp__claude_ai_Google_Calendar__create_event"
decide("S-3", CH3.auto_allowed(_CE3, {"summary": "Focus", "startTime": "x", "endTime": "y"})
       and not CH3.auto_allowed(_CE3, {"summary": "x", "attendees": [{"email": "a@b.c"}]})
       and not CH3.auto_allowed(_CE3, {"summary": "x", "attendeeEmails": ["a@b.c"]})
       and not any(CH3.auto_allowed(t, {}) for t in ("mcp__claude_ai_Google_Calendar__update_event",
                   "mcp__claude_ai_Google_Calendar__delete_event", "mcp__claude_ai_Gmail__send_gmail_message")))

# D-29 a contradiction is repaired with a Log line, never sent to him as a list or a push.
from jobs import consistency_check as CC29  # noqa: E402
(config.VAULT / "_system" / "maintenance").mkdir(parents=True, exist_ok=True)
_p29 = config.INBOX / "D29 words.md"
_p29.write_text(NOTE.replace("t-dddddd", "t-d29d01").format("- [ ] Next: Send the form for Nov 10 (10 min) 📅 2026-11-13 ^s2901"))
CC29.run(T, repair=True)
_t29 = _p29.read_text()
_src29 = (ROOT / "jobs" / "consistency_check.py").read_text(encoding="utf-8")
decide("D-29", re.search(r"- \[ \] Next: Send the form \(\d+ min\) 📅 2026-11-13 \^s2901", _t29) is not None and "consistency check:" in _t29.split("## Log")[1]
       and not re.search(r"\bnotify\b|send_push|send_mail|runner import ask", _src29), (_t29[-400:], re.findall(r"\bnotify\b|send_push|send_mail|runner import ask", _src29)))
_p29.unlink()
# ...and what it cannot decide is one tap on his row in the morning panel, not a line on a page.
from jobs import plan_today_prerun as PR29  # noqa: E402
_q29 = config.INBOX / "D29 ask.md"
_q29.write_text(NOTE.replace("t-dddddd", "t-d29d02").format("- [ ] Next: Propose a date at the planning lunch (Mon Nov 9) (10 min) ^s2902"))
_d29 = {"rows": []}
PR29._consistency_rows(_d29, T)
_r29 = [r for g, rs in _d29["rows"] for r in rs if r.get("lk") == "D29 ask"]
decide("D-29", len(_r29) == 1 and _d29["rows"][0][0] == "Past its date"
       and [(x["new"], x["yes"], x["no"]) for x in _r29[0]["prop"]] == [("", "Strike", "Keep")], _d29)
_q29.unlink()
# ...and a line carrying "(the owner" is never moved or rewritten by a repair: asked instead (Nov 12).
_h29 = config.INBOX / "D29 his.md"
_h29.write_text(NOTE.replace("t-dddddd", "t-d29d03").format("- [ ] Next: Pay the rent (10 min) ^s2903")
                + "\n## Notes\n- [ ] Call the landlord (owner decision, Nov 9)\n")
CC29.run(T, repair=True)
_ht29 = _h29.read_text()
_a29 = CC29.panel_asks(T).get("D29 his", [])
decide("D-29", "## Notes\n- [ ] Call the landlord (owner decision, Nov 9)" in _ht29 and "Then: Call the landlord" not in _ht29
       and [(x.get("field"), x["old"]) for x in _a29] == [("move_in", "- [ ] Call the landlord (owner decision, Nov 9)")], (_ht29[-300:], _a29))
_h29.unlink()

# D-30 every note write compares and swaps: an edit made after the writer read is never overwritten.
_p30 = config.INBOX / "D30.md"
_b30 = NOTE.replace("t-dddddd", "t-d30d30").format("- [ ] Next: Send it (10 min) ^s3001")
_his30 = _b30.replace("## Log\n", "## Log\n- Nov 11 · his edit\n")
_p30.write_text(_his30)   # his Obsidian edit lands after the writer read _b30
try:
    S.write(_p30, _b30.replace("Send it", "Send it today"), "pre-run", before=_b30)
    _ref30 = False
except S.NoteChanged:
    _ref30 = _p30.read_text() == _his30
S.write(_p30, _b30.replace("Send it", "Send it today"), "telegram", before=_b30, redo=lambda cur: cur.replace("Send it", "Send it today"))
_t30 = _p30.read_text()
_ok30 = S.write(_p30, _t30.replace("today", "now"), "hook", before=_t30)
decide("D-30", _ref30 and "his edit" in _t30 and "Send it today" in _t30 and "Send it now" in _ok30, (_ref30, _t30[-200:]))
_p30.unlink()

# D-8 (before Book) a task added in Plan shows Plan's live piece on Do, never a part stored earlier.
os.environ["CARMEN_FLAGS"] = "estimator,step_order"
_ss8 = dicts(["- [ ] Next: Take the HIPAA course (90 min) 📅 2026-11-16 ^s0801",
              "- [ ] Then: Get the blood draw (60 min) 📅 2026-11-11 ^s0802"])
_n8 = lambda i: {"status": "open", "scheduled": T.isoformat(), "step_states": _ss8, "next": "Take the HIPAA course",  # noqa: E731
                 "next_id": "s0801", "order": E.piece_from(_ss8, T)}
_r8 = {"Y08": {"id": "Y08", "title": "Y08"}}
def _f8(cur):   # a part Do stored earlier that day, before the note changed
    _e = D._day(cur, T); _e["chunk_ids"]["Y08"] = ["s0801"]; _e["chunks"]["Y08"] = ["Take the HIPAA course"]
    return cur
D.state.update(D.STATE, _f8, {})
_v8 = D.view(T, _r8, [], [], [], [], _n8, _r8.get, lambda d: d.isoformat(), selected=["Y08"])
_ids8 = [str(x) for x in next(r for r in _v8["rows"] if r["id"] == "Y08")["chunk"]["steps"]]
D.commit(T, ["Y08"], chunk_ids={"Y08": ["s0801"]})
_v8b = D.view(T, _r8, [], [], [], [], _n8, _r8.get, lambda d: d.isoformat())
_ids8b = [str(x) for x in next(r for r in _v8b["rows"] if r["id"] == "Y08")["chunk"]["steps"]]
os.environ["CARMEN_FLAGS"] = "estimator"
decide("D-8", _ids8[:1] and _ids8[0].startswith("Get the blood draw") and _ids8b[:1] and _ids8b[0].startswith("Take the HIPAA"), (_ids8, _ids8b))

# D-31 today changes only by his taps; a chat's move only proposes.
_r31 = {"Z31": {"id": "Z31", "title": "Z31"}}
_n31 = lambda i: {"status": "open", "scheduled": (T + dt.timedelta(days=1)).isoformat(), "step_states": [], "next": "Do it", "next_id": ""}  # noqa: E731
D.commit(T, ["Z31"])
_his31 = D.view(T, _r31, [], [], [], [], _n31, _r31.get, lambda d: d.isoformat())
D.claude_moved_to("Z31", T.isoformat(), (T + dt.timedelta(days=1)).isoformat(), T)
_chat31 = D.view(T, _r31, [], [], [], [], _n31, _r31.get, lambda d: d.isoformat())
decide("D-31", [m["id"] for m in _his31["moved"]] == ["Z31"] and [r["id"] for r in _chat31["rows"]] == ["Z31"]
       and D._day(D._load(), T)["ids"] == ["Z31"], (_his31["moved"], _chat31["rows"]))

# D-32 Clear never marks done; untick is the exact inverse of tick, also after Done on an ask step.
import types as _ty32  # noqa: E402
from jobs.checkin_app.tasks import actions as TA32  # noqa: E402
from jobs.checkin_app.adapters import notes as NA32  # noqa: E402
_blk32 = lambda t: t.split("## Next action", 1)[1].split("## Log")[0].strip() + [l for l in t.splitlines() if l.startswith("status")][0]  # noqa: E731,E741
_ok32 = []
for _k, _b in (("plain", "- [ ] Next: Send the ticket (10 min) ^s6001\n- [ ] Then: Pack the bag (30 min) ^s6002"),
               ("asks", "- [ ] Next: Ask Priya for the options (5 min) ^s6001\n- [ ] Then: Pick the hotel from the options (30 min) ^s6002"),
               ("closes", "- [ ] Next: Send the ticket (10 min) ^s6001")):
    _p32 = config.INBOX / f"D32 {_k}.md"
    NA32._store(_p32, NOTE.replace("t-dddddd", f"t-d32{_k[:2]}0").format(_b))
    _t0 = _p32.read_text()
    _new, _ = AS.step_done(_t0, AC.carmen_day(), "Nov 11")
    NA32.write_from(_p32, _t0, _new, lambda c: NA32.refuse())
    _mid = _p32.read_text()
    _code = TA32.on_step_tick(_ty32.SimpleNamespace(body={"sid": "s6001"}, action="step_untick", row_id=_p32.stem, text_in="", dry=False)).status_code
    _ok32.append((_k, _code == 200 and _blk32(_p32.read_text()) == _blk32(_t0) and _blk32(_mid) != _blk32(_t0)))
_pc32 = config.INBOX / "D32 clear.md"
_pc32.write_text(NOTE.replace("t-dddddd", "t-d32c00").format("- [ ] Next: Send it (10 min) ^s6101"))
_tc32 = _pc32.read_text()
D.commit(T, ["D32 clear"])
D.clear(T, ["D32 clear"])
decide("D-32", all(o for _, o in _ok32) and _pc32.read_text() == _tc32, _ok32)

# D-33 a sent message ticks only the step that is that message; the task closes only when it was the last.
from jobs.checkin_app.tasks import messages as MS33  # noqa: E402
_st33 = lambda t: [l for l in t.splitlines() if l.startswith(("status", "- ["))]  # noqa: E731,E741
_a33, _r33a = MS33.reply_sent(NOTE.format("- [ ] Next: Book the Jaipur hotel (20 min) ^s7001\n- [ ] Then: Reply to Priya (5 min) ^s7002"),
                              who="Priya", via="whatsapp", body="x", today=T, label="Nov 11")
_b33, _r33b = MS33.reply_sent(NOTE.format("- [ ] Next: Reply to Priya (5 min) ^s7001\n- [ ] Then: Book the Jaipur hotel (20 min) ^s7002"),
                              who="Priya", via="whatsapp", body="x", today=T, label="Nov 11")
_c33, _r33c = MS33.reply_sent(NOTE.format("- [ ] Next: Reply to Priya (5 min) ^s7001"), who="Priya", via="whatsapp", body="x", today=T, label="Nov 11")
decide("D-33", _r33a["state"] == "recorded" and "- [ ] Next: Book the Jaipur hotel (20 min) ^s7001" in _st33(_a33)
       and _r33b.get("ticked") == "Reply to Priya" and "status: open" in _st33(_b33) and "status: done" in _st33(_c33),
       (_r33a, _r33b, _r33c))

# D-34 auto-close only when every condition holds; a task he reopened, by any path, never auto-closes again.
from jobs.checkin_app.tasks import autoclose as ACL  # noqa: E402
_N34 = "---\ntype: action\nstatus: open\ntask_id: {}\nscheduled: 2026-11-10\n---\n\n## Next action\n- [ ] Next: Have dinner with Anna (2 h) ^sa001\n\n## Log\n"
_row34 = lambda lk: {"lk": lk, "g": "done", "s": "He said dinner was great", "ctx": [{"q": ["dinner was great"]}]}  # noqa: E731
_t34 = _N34.format("t-d34d34")
_one34 = [ACL.judge(_t34, {**_row34("Dinner with Anna"), **x}, T) == "" for x in ({"ctx": []}, {"lc": 1}, {"g": "today"})] \
    + [ACL.judge(_t34.replace("2026-11-10", T.isoformat()), _row34("Dinner with Anna"), T) == "",
       ACL.judge(_t34.replace("^sa001", "^sa001\n- [ ] Then: Send thanks (5 min) ^sa002"), _row34("Dinner with Anna"), T) == ""]
_pa34 = config.INBOX / "Dinner with Anna.md"
_pa34.write_text(_t34)
_ra34 = ACL.run([["T", [_row34("Dinner with Anna")]]], T, "pre-run", NA32.task_path)
ACL.undo(_pa34, T)
_rb34 = ACL.run([["T", [_row34("Dinner with Anna")]]], T, "pre-run", NA32.task_path)
_pb34 = config.INBOX / "Lunch with Ana.md"
_pb34.write_text(_N34.format("t-d34d35"))
ACL.run([["T", [_row34("Lunch with Ana")]]], T, "pre-run", NA32.task_path)
_pb34.write_text(_pb34.read_text().replace("status: done", "status: open"))   # his reopen in Obsidian
_rc34 = ACL.run([["T", [_row34("Lunch with Ana")]]], T, "pre-run", NA32.task_path)
_rd34 = ACL.run([["T", [_row34("Lunch with Ana")]]], T, "pre-run", NA32.task_path)
decide("D-34", all(_one34) and len(_ra34) == 1 and _rb34 == [] and _rc34 == [] and _rd34 == [] and "status: open" in _pb34.read_text(),
       (_one34, _ra34, _rb34, _rc34))

# S-4 a push that fails becomes an Inbox note; the heartbeat never swallows a failed push.
from runner import notify as NT4, job as RJ4  # noqa: E402
_sp4 = NT4.send_push
NT4.send_push = lambda *a, **k: (_ for _ in ()).throw(NT4.PushUnavailable("Pushover HTTP 500: test"))
_inbox4 = set(config.INBOX.glob("*.md"))
try:
    with RJ4.Job("s4probe", description="S4 probe") as _j4:
        _j4.check("thing works", ok=False, tier=RJ4.CRITICAL, owner=RJ4.AUTOMATIC, covers="s4", detail="broken")
except SystemExit:
    pass
finally:
    NT4.send_push = _sp4
_new4 = [q for q in set(config.INBOX.glob("*.md")) - _inbox4 if "S4 probe" in q.read_text()]
_hb4 = (ROOT / "jobs" / "heartbeat.py").read_text(encoding="utf-8")
decide("S-4", len(_new4) == 1 and "## Next action\n- [ ] Next:" in _new4[0].read_text() and "thing works" in _new4[0].read_text()
       and not re.search(r"send_push\([^\n]*\n(?:[^\n]*\n){0,2}\s*except Exception:[^\n]*\n\s*pass\b", _hb4),
       [q.name for q in set(config.INBOX.glob("*.md")) - _inbox4])
for _q4 in _new4:
    _q4.unlink()

# S-5 master moves only through bin/land: a hand fast-forward is refused, LAND=1 (bin/land) moves it.
_g5 = TMP / "s5"
(_g5 / "hooks").mkdir(parents=True)
(_g5 / "hooks" / "reference-transaction").write_text(f'#!/bin/sh\nexec "{ROOT}/bin/git-hook" reference-transaction "$@"\n')
(_g5 / "hooks" / "reference-transaction").chmod(0o755)
_G5 = lambda *a, **e: subprocess.run(["git", "-C", str(_g5 / "r"), "-c", "user.email=t@t", "-c", "user.name=t", *a],  # noqa: E731
                                     capture_output=True, text=True, env={**os.environ, "XDG_STATE_HOME": str(TMP / "xdg5"), **e})
(_g5 / "r").mkdir()
_G5("init", "-q", "-b", "master"); (_g5 / "r" / "f").write_text("1"); _G5("add", "f"); _G5("commit", "-qm", "base")
_G5("config", "core.hooksPath", str(_g5 / "hooks"))
_G5("checkout", "-q", "-b", "work"); (_g5 / "r" / "g").write_text("1"); _G5("add", "g"); _G5("commit", "-qm", "work")
_G5("checkout", "-q", "master")
_hand5 = _G5("merge", "-q", "--ff-only", "work")
_reset5 = _G5("update-ref", "refs/heads/master", "work")
_land5 = _G5("merge", "-q", "--ff-only", "work", LAND="1")
decide("S-5", _hand5.returncode != 0 and _reset5.returncode != 0 and "bin/land" in _hand5.stderr and _land5.returncode == 0
       and _G5("log", "-1", "--format=%s", "master").stdout.strip() == "work", (_hand5.stderr, _reset5.stderr, _land5.stderr))

# D-35 the backward pass: a date travels back through a wait; own-work chains keep their dates.
from tools import backsched as B  # noqa: E402
_T35 = dt.date(2026, 11, 12)
_shc = ["- [ ] Waiting: Lakeside nurse, on the lab order (form submitted Nov 11) ^s3501",
        "- [ ] Then: Book the lab draw (after: ^s3501) (35 min) ^s3502",
        "- [ ] Waiting: Lakeside lab, on the lab result (after: ^s3502) (up to 7 d) ^s3503",
        "- [ ] Then: Send Lakeside the clearance file (after: ^s3503) (45 min) 📅 2026-11-17 ^s3504",
        "- [ ] Next: Take the HIPAA course (90 min) 📅 2026-11-16 ^s3505"]
_st35 = S.steps_of("## Next action\n" + "\n".join(_shc), _T35)
_r35 = B.compute(B.from_steps(_st35, _T35), _T35)
# The rejected alternative: the old one-hop view, without the stated 7-day result in the chain.
_no35 = [l for l in _shc if "s3503" not in l]
_no35 = [l.replace("(after: ^s3503)", "(after: ^s3502)") for l in _no35]
_r35b = B.compute(B.from_steps(S.steps_of("## Next action\n" + "\n".join(_no35), _T35), _T35), _T35)
_m1 = ["- [ ] Next: the e-bike part 2 (2 h) 📅 2026-11-13 ^s3511", "- [ ] Then: the e-bike part 3 (after: ^s3511) (1 h) 📅 2026-11-13 ^s3512",
       "- [ ] Then: Take the DMV test (70 min) ^s3513"]
os.environ["CARMEN_FLAGS"] = "estimator,step_order"
_o35a = E.rank(dicts(_m1, _T35), _T35)["order"]
os.environ["CARMEN_FLAGS"] = "estimator,step_order,backsched"
_o35b = E.rank(dicts(_m1, _T35), _T35)["order"]
os.environ["CARMEN_FLAGS"] = "estimator"
_pend = S.steps_of("## Next action\n- [ ] Next: Book the draw (35 min) ^s3521\n- [ ] Waiting: Lakeside lab, on the lab result (after: ^s3521) (up to 7 d) ^s3522", _T35)
decide("D-35", _r35["state"] == "start" and _r35["nodes"]["s3501"]["state"] == "start" and "7 days" in _r35["why"]
       and _r35b["state"] == "" and _o35a == _o35b and not S.task_waiting(_pend)
       and S.format_line(_pend[1]) == "- [ ] Waiting: Lakeside lab, on the lab result (after: ^s3521) (up to 7 d) ^s3522"
       and B.compute(B.from_steps(_st35, _T35), _T35) == _r35,   # read only: the same answer twice
       (_r35["state"], _r35b["state"], _o35a, _o35b))

# D-36 durations: stated first, else the kind of party, judged by who it is before what it is about.
_w36 = {"kind": "Waiting", "text": "Holiday Craft, to resubmit the e-Visa with all given names"}
decide("D-36", B.duration({"kind": "Waiting", "text": "Lakeside lab, on the lab result", "span": 7})[:2] == (3.5, 7.0)
       and B.kind_of(B.who_of(_w36), _w36["text"]) == "vendor" and B.kind_of("", "the e-Visa office, on the visa") == "government"
       and B.duration({"kind": "Then", "text": "Book the draw"})[:2] == (0.0, 0.0),
       (B.duration(_w36), B.kind_of(B.who_of(_w36), _w36["text"])))

# D-37 the model judges last, never silently against the count: a downgrade needs its reason.
import jobs.plan_today_prerun as PR  # noqa: E402
_ev37 = [{"name": "HR", "backsched": {"state": "start", "start_by": "2026-11-08", "why": "start today: the lab result takes up to 7 days", "steps": [], "chases": []}}]
_d37 = {"rows": [["Scheduled today", [{"n": "HR", "g": "soon", "s": "HIPAA first."}]]]}
_d37b = {"rows": [["Scheduled today", [{"n": "HR", "g": "soon", "s": "x", "bo": "Lakeside ordered it at 07:10 (mail)"}]]]}
_n37, _n37b = PR._backsched_check(_d37, _ev37, _T35), PR._backsched_check(_d37b, _ev37, _T35)
_row37, _row37b = _d37["rows"][0][1][0], _d37b["rows"][0][1][0]
decide("D-37", _n37 == 1 and _row37["g"] == "today" and _row37["s"].startswith("Start today") and _row37["wd"].startswith("Start today")
       and _n37b == 0 and _row37b["g"] == "soon", (_row37, _row37b))

# D-38 the chase: an ordinary step when a dated chain needs it, the Nudge after 3 days otherwise.
_t38 = "---\ntype: action\n---\n\n## Next action\n" + "\n".join(_shc) + "\n\n## Log\n"
_c38, _sid38 = S.op_chase(_t38, "s3501", _T35)
_again38, _sid38b = S.op_chase(_c38, "s3501", _T35)
_over38, _gone38 = S.op_chase_over(_c38.replace("- [ ] Waiting: Lakeside nurse", "- [x] Waiting: Lakeside nurse"), _T35)
_un38 = {"kind": "Waiting", "text": "Priya, on the hotel options", "since": "2026-11-09"}
from jobs.checkin_app.planning import do as D38  # noqa: E402
decide("D-38", f"- [ ] Then: Chase Lakeside nurse about the lab order (15 min) 📅 2026-11-12 ^{_sid38}" in _c38 and _sid38b == ""
       and _gone38 == [_sid38] and "(wait over, Nov 12)" in _over38
       and B.chase_due(_un38, _T35, None)["due"] and not B.chase_due({**_un38, "since": "2026-11-10"}, _T35, None)["due"]
       and not B.chase_due(_un38, _T35, None, quiet=True)["due"] and D38.NUDGE_AFTER is B.NUDGE_AFTER,
       (_c38[-400:], _gone38))

# D-39 a wait's life is logged: opened and ended, so response times can be learned.
_p39 = TMP / "vault" / "Inbox" / "Decisions D39.md"
_a39 = NOTE.format("- [ ] Next: Send the form (10 min) ^s3901")
_p39.write_text(_a39)
S.record(_p39.stem, None, _a39, "test", _T35)
_b39 = NOTE.format("- [x] Next: Send the form (10 min) ✅ 2026-11-12 ^s3901\n- [ ] Waiting: Lakeside nurse, on the order (up to 3 d) ^s3902")
_e39a = S.record(_p39.stem, _a39, _b39, "test", _T35)
_c39 = _b39.replace("- [ ] Waiting: Lakeside nurse", "- [x] Waiting: Lakeside nurse")
_e39b = S.record(_p39.stem, _b39, _c39, "test", _T35)
decide("D-39", any(e.get("event") == "wait_opened" and e.get("span") == 3 for e in _e39a)
       and any(e.get("event") == "wait_ended" and e.get("how") == "done" for e in _e39b)
       and not any(e.get("event", "").startswith("wait_") for e in S.record(_p39.stem, _c39, _c39, "test", _T35)),
       (_e39a, _e39b))

# Every decision in the tables has its check here and every check its row; ids are unique; every
# mark is bare and has its row. A table that cannot be read fails (fail closed, Nov 12).
# A row pending on a branch (Code cell `pending: <branch>, <Mon D>`) may lack its check for 48 h.
_ids = set(re.findall(r'decide\("([DS]-\d+)"', pathlib.Path(__file__).read_text(encoding="utf-8")))
_fresh = {d for d, v in DG.pending(_T if not _DOC_ERR else {}).items() if v[2]}
decide("D-table", not _DOC_ERR and set(ROWS) - _fresh <= _ids <= set(ROWS) and not _DUPS and not _PROSE and set(_MARKS) <= set(ROWS),
       _DOC_ERR or {"no check": sorted(set(ROWS) - _ids - _fresh), "no row": sorted(_ids - set(ROWS)), "duplicate": _DUPS,
                    "marks with words": _PROSE[:5], "marks without a row": sorted(set(_MARKS) - set(ROWS))})
import shutil  # noqa: E402
shutil.rmtree(TMP, ignore_errors=True)
print("\nall passed" if not FAILS else f"\n{len(FAILS)} FAILED: {', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
