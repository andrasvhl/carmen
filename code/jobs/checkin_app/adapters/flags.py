"""Feature flags: one read path for behaviour that ships switched off (Tasks v2, Nov 6).

Each Tasks v2 slice that changes what the owner sees or what lands in a note is behind one flag, so
the code deploys first and the behaviour is switched on after his OK, and off again without a
deploy. A flag is on when the state file `checkin_app/flags.json` says so ({"contract": true}),
or when the environment's CARMEN_FLAGS names it ("contract,estimator", or "all"); the
environment wins, so every test suite sets exactly what it tests. Everything is off by default.

  contract      the step contract: "unclear step · sharpen?" on the card
  estimator     the one estimator on every write path: no open step without an estimate
  timebox       timeboxed steps on the card, the known / not known switch, found_by
  frozen_chunk  today's chunk frozen at commit, with an outcome line
  do_plan       the Do card keeps today's committed steps, found steps apart
  actuals       actuals, the one-tap check and calibration
  day_turn      left-over tasks re-estimated, with a fresh proposed piece, at the day turn
  backfill      the step cleanup proposals in Carmen
"""
from __future__ import annotations

import json
import os

from runner import config

FILE = config.STATE_DIR / "checkin_app" / "flags.json"
NAMES = ("contract", "estimator", "timebox", "frozen_chunk", "do_plan", "actuals", "day_turn", "backfill")


def on(name: str) -> bool:
    env = os.environ.get("CARMEN_FLAGS")
    if env is not None:
        got = {x.strip() for x in env.split(",") if x.strip()}
        return "all" in got or name in got
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(d, dict) and d.get(name) is True)


def all_on() -> dict:
    return {n: on(n) for n in NAMES}
