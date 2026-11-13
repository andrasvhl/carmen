#!/usr/bin/env python3
"""The flight watcher's judgement: what counts as a deal, and what never pushes.

    python3 -m tests.flight_watch

No network, no push, no mail. The flight search, Pushover, mail and the
quiet-hours window are stubbed, and the vault is a scratch directory.

Four properties, each written because getting it wrong has a specific cost:

  1. **A fare at or under target pushes, once.** the owner asked for deals only;
     the same $700 fare seen twice a day must not ring his phone twice a day.
  2. **A further 5 % drop pushes again**, because that is new information.
  3. **Without a target, nothing pushes until there is a history.** One check
     has no "usual price" to beat; a first-run push would be noise.
  4. **A broken watch block is reported, and the other watches still run.**
     He edits these blocks by hand in Obsidian.
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TMP = pathlib.Path(tempfile.mkdtemp())
from runner import config, notify, quiet_hours

config.STATE_DIR = TMP / "state"
config.LOCK_DIR = TMP / "state" / "locks"
config.LOCK_DIR.mkdir(parents=True, exist_ok=True)
config.ENV_FILE = TMP / "no-env"
config.VAULT = TMP / "vault"

from jobs import flight_watch as fw
from tools import flights

fw.TRIPS = config.VAULT / "Areas" / "Personal" / "Travel"
fw.STATUS_PAGE = config.VAULT / "_system" / "flight-watch-status.md"
fw.TRIPS.mkdir(parents=True)
fw.STATUS_PAGE.parent.mkdir(parents=True)

PUSHED: list[tuple[str, str]] = []
MAILED: list[str] = []
PRICE = {"usd": 900.0}
FAILURES: list[str] = []

notify.send_push = lambda title, message, **kw: PUSHED.append((title, message))
notify.send_mail = lambda subject, body: MAILED.append(subject)
quiet_hours.is_quiet = lambda *a, **k: False


def fake_grid(origins, dests, days, **kw):
    offer = flights.Offer(source="kiwi", origin=origins[0], destination=dests[0], date=days[0],
                          price_usd=PRICE["usd"], price=f"{PRICE['usd']} USD",
                          airlines="Testair", stops=1, duration_min=600, url="https://x.test")
    return [offer], []


flights.search_grid = fake_grid
fw.flights.search_grid = fake_grid


def page(name: str, blocks: list[str]) -> None:
    body = "\n\n".join(f"```flight-watch\n{b}\n```" for b in blocks)
    (fw.TRIPS / f"{name}.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")


def run() -> None:
    sys.argv = ["flight_watch"]
    fw.main()


def expect(cond: bool, what: str) -> None:
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        FAILURES.append(what)


good = json.dumps({"id": "nyc", "from": ["LHR"], "to": ["JFK"],
                   "dates": "2099-04-10..2099-04-11", "target_usd": 750, "bag": True})
nohist = json.dumps({"id": "lon", "from": ["BOS"], "to": ["LHR"], "dates": "2099-11-19"})
page("Winter", [good, nohist, "{not json"])

PRICE["usd"] = 900
run()
expect(not PUSHED, "over target and no history: no push")
expect("flight-watch-status" in fw.STATUS_PAGE.read_text(), "status page written")
state = json.loads((config.STATE_DIR / "flight_watch.json").read_text())
expect(any("parse" in json.dumps(c) for c in state.get("checks", state.get("failures", [])))
       or "parse" in json.dumps(state), "broken block reported")

PRICE["usd"] = 740
run()
expect(len(PUSHED) == 1, f"under target pushes; no-target watch with one check does not (got {len(PUSHED)})")
nyc = [t for t, _ in PUSHED if "LHR-JFK" in t]
expect(len(nyc) == 1 and "$740" in nyc[0], "nyc pushed at $740")

PUSHED.clear()
run()
expect(not PUSHED, "same price again: no second push")

PRICE["usd"] = 700
run()
expect(any("LHR-JFK" in t for t, _ in PUSHED), "a further 5% drop pushes again")

PUSHED.clear()
PRICE["usd"] = 900
run()
expect(not PUSHED, "price back up: nothing pushed")

print(f"\n{len(FAILURES)} failure(s)")
sys.exit(1 if FAILURES else 0)
