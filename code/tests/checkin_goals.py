"""Offline checks for Carmen's Goals tab (jobs/checkin_app/goals.py): the note round-trips, every
change is logged, hand edits are logged on the next read, and counting rules hold.

    .venv/bin/python -m tests.checkin_goals
"""
import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A scratch HOME and vault before the app is imported, as every suite in the checkin-restart gate
# (since 6f5b5c7): state paths are fixed at import, and this suite must never touch the live note.
_SCRATCH = Path(tempfile.mkdtemp(prefix="carmen-goals-"))
(_SCRATCH / "home").mkdir()
(_SCRATCH / "vault" / "Areas" / "Goals").mkdir(parents=True)
os.environ["HOME"], os.environ["VAULT_PATH"] = str(_SCRATCH / "home"), str(_SCRATCH / "vault")

import jobs.checkin_app as app
import jobs.checkin_app.runs as _runs
from jobs.checkin_app.direction import goals as G

assert str(app.config.STATE_DIR).startswith(str(_SCRATCH)) and str(app.config.VAULT).startswith(str(_SCRATCH)) \
    and str(G.NOTE).startswith(str(_SCRATCH)) and str(G.STATE).startswith(str(_SCRATCH)), \
    "goals suite is not isolated: refusing to touch live state"
_runs.maybe_start_prep = lambda generated: None   # never a real prep run from a test

NOTE = """---
type: note
---

# Current goals

Intro line.

Direction: Social
Close friends: Ben, Chris

## Social
Status: active
Why: Friends.

| Target | Goal | Counted by |
|---|---|---|
| Lunches | 4 | lunches |
| Late nights | max 3 | late_nights |

A free line he wrote.

## Calm
Status: paused
Why: Mind.

| Target | Goal | Counted by |
|---|---|---|
| Gym | 1 | calendar: gym, workout |

## Change log
- Nov 5 · Created · the owner
"""
TODAY = dt.date(2026, 11, 5)
fails = 0


def check(name, cond):
    global fails
    print(("ok   " if cond else "FAIL ") + name)
    fails += not cond


with tempfile.TemporaryDirectory() as d:
    G.NOTE, G.STATE = Path(d) / "Current goals.md", Path(d) / "goals.json"
    G.NOTE.write_text(NOTE)
    m = G.parse(NOTE)
    check("parse direction and close", m["direction"] == "Social" and m["close"] == ["Ben", "Chris"])
    check("parse goals, status, targets", [g["name"] for g in m["goals"]] == ["Social", "Calm"]
          and m["goals"][1]["status"] == "paused" and m["goals"][0]["targets"][1]["goal"] == "max 3")
    check("free lines kept", "A free line he wrote." in G.serialize(m))
    check("round trip is stable", G.serialize(G.parse(G.serialize(m))) == G.serialize(m))
    check("round trip keeps the intro and log", "Intro line." in G.serialize(m) and "- Nov 5 · Created · the owner" in G.serialize(m))

    G.load(TODAY)   # first read: snapshot only, nothing logged
    check("first read logs nothing", G.NOTE.read_text().count("\n- ") == 1)

    import unittest.mock as um
    with um.patch.object(G, "view", lambda *a, **k: {}):
        G.edit({"op": "target", "goal": "Social", "index": 0, "name": "Lunches"}, today=TODAY)
        check("no-op edit logs nothing", G.NOTE.read_text().count("\n- ") == 1)
        G.edit({"op": "target", "goal": "Social", "index": 1, "value": "max 2"}, today=TODAY)
        text = G.NOTE.read_text()
        check("target number changed and logged", "| Late nights | max 2 | late_nights |" in text
              and "Social: Late nights max 3 → max 2 · Carmen" in text and "## Social" in text)
        m2 = G.parse(G.NOTE.read_text())
        G.apply_op(m2, {"op": "rename", "goal": "Calm", "name": "Mind"})
        check("rename moves the goal", [g["name"] for g in m2["goals"]] == ["Social", "Mind"])
        G.edit({"op": "add_target", "goal": "Social", "name": "Dinners", "by": "dinners"}, today=TODAY)
        text = G.NOTE.read_text()
        check("add target writes row and log", "| Dinners | 1 | dinners |" in text and "added target Dinners" in text and "· Carmen" in text)
        G.edit({"op": "direction", "goal": "Calm"}, today=TODAY)
        check("direction change logged", "Direction: Calm" in G.NOTE.read_text() and "Direction: Social → Calm" in G.NOTE.read_text())
        G.edit({"op": "pause", "goal": "Calm"}, today=TODAY)
        check("resume logged", "Status: active" in G.NOTE.read_text().split("## Calm")[1] and "Resumed Calm" in G.NOTE.read_text())
        G.edit({"op": "add_goal", "name": "Run", "why": "A race"}, today=TODAY, test=True)
        check("test mode writes nothing", "## Run" not in G.NOTE.read_text())
        try:
            G.edit({"op": "add_goal", "name": "Change log"}, today=TODAY)
            check("reserved name refused", False)
        except ValueError:
            check("reserved name refused", True)

    # a hand edit in Obsidian is logged on the next read
    G.NOTE.write_text(G.NOTE.read_text().replace("| Lunches | 4 |", "| Lunches | 5 |"))
    G.load(TODAY)
    text = G.NOTE.read_text()
    check("hand edit logged as in the note", "Social: Lunches 4 → 5 · in the note" in text)
    G.load(TODAY)
    check("hand edit logged once", G.NOTE.read_text().count("Lunches 4 → 5") == 1)

# counting rules
t4, tmax = {"goal": "4", "by": "lunches"}, {"goal": "max 3", "by": "late_nights"}
check("status good/warn/bad", (G.status(4, t4), G.status(3, t4), G.status(1, t4)) == ("good", "warn", "bad"))
check("status max", (G.status(3, tmax), G.status(4, tmax), G.status(5, tmax)) == ("good", "warn", "bad"))
check("status pro-rated", G.status(2, {"goal": "6", "by": "journal_days"}, 3 / 7) == "warn")
check("manual is not counted", G.status(None, {"goal": "2", "by": "manual"}) == "idle")
ev = lambda title, **k: {"title": title, "kind": "main", "n": 0, "allday": False, "who": "", "tentative": False,
                         "day": TODAY, "last": TODAY, "s": 720, "f": 780} | k
check("class is not people", not G.with_people(ev("ECON 342.1", n=1)))
check("lunch with attendee is people", G.with_people(ev("Lunch", n=1)))
check("focus block is not people", not G.with_people(ev("Lunch", kind="focus")))
check("close friend matched", G._close_hit(ev("Coffee x Ben", n=1), ["Ben", "Chris"]) == "Ben")
check("unknown metric is manual", G._by("foo: bar") == ("manual", "bar"))

print("checks passed" if not fails else f"{fails} failed")
raise SystemExit(1 if fails else 0)
