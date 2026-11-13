"""Backward scheduling (Nov 12): the operations rules, in one place, read only.

owner decision, Nov 12, on HR: the most time-critical step (chase the Lakeside nurse, so the lab draw and its
7-day result land before the file is due Nov 17) was not flagged, because no rule carried a date
backwards through a wait. Design: _system/claude/backward-scheduling-proposal-Nov-12.md (the
council's rules B-1 to B-11), decisions D-35 to D-39 in _system/checkin-app.md.

What this module owns, and nothing else:
  KINDS          how long a party usually takes (p50) and at worst (p90), per kind, in days
  kind_of        a party's kind from its name, rules only (a model refines later)
  duration       a wait's (p50, p90, source): stated "(up to N d)", else the kind default
  compute        the backward pass over a note's steps: per open step and wait its latest finish,
                 safe start and state (quiet, start, late), and per running wait its chase
  chase_due      when a wait's chase comes due (the Nudge, one owner since Nov 12)

Rules (D-35): a step's latest finish is the earliest of its own date and, for each step or wait
after it, that one's latest finish minus its duration. His own steps take 0 days (their minutes
are need_per_day's). The safe start is the latest start at p50 minus one buffer for the chain, the
square root of the summed (p90 - p50) squared of its uncertain nodes: no padding per step, and a
chain of his own work only has no buffer, so its dates are exactly its own. Nothing here writes a
note or state: every date is computed on each read (D-27) and never written into a line.

Steps are dicts as tasks/read builds them ({"id", "kind", "state", "text", "after", "due", "gate",
"event", "done", "span", "since"}) or tools/steps.Step objects (from_steps)."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re

from runner import config

# D-38
NUDGE_AFTER = 3          # an unchained wait (nothing dated after it): the Nudge after this many days
QUIET_HOURS = 24         # the party wrote within this: no chase (they are on it), the date still shows

# D-36
# Per kind, (p50, p90) in calendar days (business days and service windows: later). A stated
# bound "up to N" is the p90 and half of it the p50. Sized from 7 asked/answered pairs in his notes
# (Nov 12: most within 2 days, one open 37 days); learned from wait events once there are some.
KINDS = {
    "friend": (0.5, 2.0),
    "vendor": (1.0, 4.0),
    "clinic": (1.0, 4.0),
    "university": (2.0, 7.0),
    "government": (7.0, 21.0),
    "lab": (3.0, 7.0),
    "delivery": (2.0, 2.0),   # LEAD_DAYS, the Nov 11 M1 gear rule, as a wait
}
_KIND_RX = [
    ("lab", r"\b(?:lab|result|test result|titer|biopsy|pathology)\b"),
    ("delivery", r"\b(?:delivery|shipping|shipment|courier|parcel|package|ups|fedex|usps|dhl|amazon)\b"),
    ("government", r"\b(?:dmv|visa|e-visa|consulate|embassy|uscis|irs|passport|immigration|government|customs|ministry)\b"),
    ("clinic", r"\b(?:Lakeside|clinic|nurse|doctor|dr\.?|physician|pharmacy|walgreens|cvs|portal|hospital|dentist|clinician|np)\b"),
    ("university", r"\b(?:stanford|gsb|registrar|professor|prof\.?|tas?|faculty|dean|admissions|office|department|academic)\b"),
    ("vendor", r"\b(?:craft|travel|agency|airline|hotel|shop|store|support|bank|insurance|wise|ltd|inc|llc|gmbh|company|service|landlord)\b"),
]

FLAGS = config.STATE_DIR / "checkin_app" / "flags.json"


def enabled() -> bool:
    """Flag `backsched`, read as jobs/checkin_app/adapters/flags.on reads it (same file, same
    CARMEN_FLAGS override), without importing the app: tools/todos and the pre-run use this."""
    env = os.environ.get("CARMEN_FLAGS")
    if env is not None:
        got = {x.strip() for x in env.split(",") if x.strip()}
        return "all" in got or "backsched" in got
    try:
        d = json.loads(FLAGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(d, dict) and d.get("backsched") is True)


# D-36
def kind_of(who: str, text: str = "") -> str:
    """A party's kind (D-36), rules first: who it is (Holiday Craft: a vendor, even when the line
    is about an e-Visa), else the words of the line. A person's name alone is a friend."""
    for t in ((who or "").lower(), (text or "").lower()):   # who they are first, then what for
        for k, rx in _KIND_RX:
            if t and re.search(rx, t):
                return k
    return "friend" if re.match(r"^[A-Z][a-z]+(?:\s[A-Z][a-z]+)?$", (who or "").strip()) else "vendor"


def who_of(x: dict) -> str:
    return re.split(r"\s*(?: · for: |,|\(| to reply| on )", x.get("text") or "")[0].strip()


# D-36
def duration(x: dict) -> tuple[float, float, str]:
    """A wait's (p50, p90, source) in days (D-36): its stated "(up to N d)", else its kind's
    default. A step of his takes 0 days in the pass."""
    if x.get("kind") != "Waiting":
        return 0.0, 0.0, ""
    if x.get("span"):
        n = float(x["span"])
        return n / 2, n, f"stated: up to {_days(n)}"
    k = kind_of(who_of(x), x.get("text") or "")
    p50, p90 = KINDS[k]
    return p50, p90, f"guess ({k}): usually {_days(p50)}, up to {_days(p90)}"


def _days(n: float) -> str:
    n = round(n, 1)
    if n < 1:
        return f"{int(round(n * 24))} h"
    s = f"{n:g}"
    return f"{s} day" + ("" if s == "1" else "s")


def _ord(iso: str) -> float | None:
    try:
        return float(dt.date.fromisoformat(iso[:10]).toordinal())
    except (TypeError, ValueError):
        return None


def _iso(o: float) -> str:
    return dt.date.fromordinal(int(math.floor(o))).isoformat()


APPT = re.compile(r"^(?:join|attend|go to|take the [\w ]*test)\b", re.I)


# D-35
def is_appointment(x: dict) -> bool:
    """A step whose date is when it happens, not a deadline: an event step (ev- link) or one
    that joins or attends something on that day ("Join the Lakeside video visit")."""
    return bool(x.get("event")) or bool(APPT.match(_clean(x.get("text") or "")))


def _clean(t: str) -> str:
    return re.sub(r"^\s*(?:Next|Then|Waiting):\s*", "", t or "").strip()


# D-35
def compute(steps: list[dict], today: dt.date, hard: list[str] | None = None,
            quiet: dict | None = None) -> dict:
    """The backward pass over one note's steps (D-35). steps: every step of the note; hard: extra
    hard dates for the task (its frontmatter due, booked events), each also a cap on every open
    item; quiet: {wait id: True} when the party wrote within QUIET_HOURS.

    Returns {"nodes": {id: {"lf", "safe", "state", "p50", "p90", "src", "why", "chain"}},
             "chase": {wait id: {"due": bool, "at": iso, "why": str}},
             "start_by": the earliest safe start of an open item that is actionable, or "",
             "state": "late" | "start" | "", "why": the reason line for the task}."""
    t0 = float(today.toordinal())
    quiet = quiet or {}
    items = {x["id"]: x for x in steps if x.get("state") == "open" and x.get("id")}
    done = {x["id"]: x for x in steps if x.get("state") == "done" and x.get("id")}
    succ: dict[str, list[str]] = {i: [] for i in items}
    for i, x in items.items():
        for t in x.get("after") or []:
            if t in items:
                succ[t].append(i)
    hard_cap = min((o for o in (_ord(h) for h in hard or []) if o is not None), default=None)

    def pending(x):   # a wait after an open step of his: not started yet (B-1)
        return x.get("kind") == "Waiting" and any(t in items for t in x.get("after") or [])

    def since(x):
        if x.get("since"):
            return _ord(x["since"])
        ds = [_ord(done[t].get("done") or "") for t in x.get("after") or [] if t in done]
        ds = [d for d in ds if d is not None]
        return max(ds) if ds else None

    def dur(x):
        p50, p90, src = duration(x)
        if x.get("kind") == "Waiting" and not pending(x):
            s = since(x)
            if s is not None:   # past its median a wait is not "almost done": the full p50 again
                p90 = max(p50, p90 - max(0.0, t0 - s))
        return p50, p90, src

    def own(x, hard_only):
        d = _ord(x.get("due") or "")
        if hard_only:
            d = d if (d is not None and is_appointment(x)) else None
            if hard_cap is not None:
                d = hard_cap if d is None else min(d, hard_cap)
        return d

    memo: dict = {}

    def lf(i, hard_only=False, seen=()):
        """(latest finish at p50, variance behind it, the chain of ids to the anchor), or None."""
        k = (i, hard_only)
        if k in memo:
            return memo[k]
        if i in seen:   # a cycle in after: is the consistency check's; stop here
            return None
        x = items[i]
        cands = []
        o = own(x, hard_only)
        if o is not None:
            cands.append((o, 0.0, [i]))
        for s in succ[i]:
            r = lf(s, hard_only, seen + (i,))
            if r is None:
                continue
            p50, p90, _ = dur(items[s])
            cands.append((r[0] - p50, r[1] + (p90 - p50) ** 2, [i] + r[2]))
        best = min(cands, key=lambda c: (c[0] - math.sqrt(c[1]), c[0])) if cands else None
        memo[k] = best
        return best

    nodes, chase, starts = {}, {}, []
    late_any = start_any = False
    task_why = ""
    for i, x in items.items():
        r = lf(i)
        if r is None:
            continue
        p50, p90, src = dur(x)
        lf50, var, chain = r
        safe = lf50 - p50 - math.sqrt(var + (p90 - p50) ** 2)
        h = lf(i, True)
        free = (x.get("kind") != "Waiting" and not any(t in items for t in x.get("after") or [])
                and not (x.get("gate") and x["gate"] > today.isoformat()) and not x.get("optional"))
        running = x.get("kind") == "Waiting" and not pending(x)
        state = ""
        if free or running:
            if h is not None and t0 + (p50 if running else 0) > h[0]:
                state = "late"
            elif safe <= t0:
                state = "start"
        why = _why(items, chain, safe, lf50, today)
        nodes[i] = {"lf": _iso(lf50), "safe": _iso(safe), "state": state, "p50": p50, "p90": p90,
                    "src": src, "why": why, "chain": chain}
        if running:
            c = chase_due(x, today, lf50 - p50 - math.sqrt(var + (p90 - p50) ** 2), quiet.get(i, False))
            chase[i] = c
            if state and not c["due"]:
                state = nodes[i]["state"] = ""   # no action today (they are on it): the why only
        if state and (free or running):
            starts.append((safe, i))
            if state == "late":
                late_any = True
            start_any = True
    # Unchained running waits keep the Nudge after NUDGE_AFTER days.
    for i, x in items.items():
        if x.get("kind") == "Waiting" and not pending(x) and i not in chase:
            chase[i] = chase_due(x, today, None, quiet.get(i, False))
    starts.sort()
    if starts:
        task_why = nodes[starts[0][1]]["why"]
    return {"nodes": nodes, "chase": chase,
            "start_by": _iso(starts[0][0]) if starts else "",
            "state": "late" if late_any else ("start" if start_any else ""), "why": task_why}


# D-38
def chase_due(x: dict, today: dt.date, safe: float | None, quiet: bool = False) -> dict:
    """When a running wait's chase comes due (D-38): the earlier of its since plus the party's p75
    and its safe start, for a wait in a dated chain; since plus NUDGE_AFTER days for one with
    nothing dated after it. A party who wrote within QUIET_HOURS is not chased."""
    t0 = float(today.toordinal())
    s = _ord(x.get("since") or "")
    p50, p90, _ = duration(x)
    if safe is None:
        at = s + NUDGE_AFTER if s is not None else None
    else:
        p75 = p50 + (p90 - p50) / 2
        at = min(s + p75, safe) if s is not None else safe
    due = bool(at is not None and at <= t0 and not quiet)
    why = ""
    if due:
        why = "chase today" if safe is not None and safe <= t0 else f"no answer for {int(t0 - s)} days" if s is not None else "chase"
    return {"due": due, "at": _iso(at) if at is not None else "", "why": why, "quiet": bool(quiet)}


def _why(items: dict, chain: list[str], safe: float, lf50: float, today: dt.date) -> str:
    """The reason line (D-35): what the date protects, the waits on the way and their durations."""
    if not chain:
        return ""
    anchor = items[chain[-1]]
    ad = anchor.get("due") or ""
    parts = []
    for i in chain[1:]:
        x = items[i]
        if x.get("kind") == "Waiting":
            p50, p90, src = duration(x)
            what = re.sub(r"^.*?\bon (?:the )?", "", _clean(x.get("text") or ""), count=1)
            what = re.split(r"\s*\(", what)[0].strip() or who_of(x)
            parts.append(f"{what} takes up to {_days(p90)}" + ("" if src.startswith("stated") else " (a guess)"))
    if not parts:
        return ""
    d = dt.date.fromordinal(int(math.floor(safe)))
    head = "start today" if safe <= today.toordinal() else f"start by {d:%a} {d:%b} {d.day}"
    tail = ""
    if ad:
        a = dt.date.fromisoformat(ad)
        words = re.sub(r"\s*\(.*$", "", _clean(anchor.get("text") or "")).split()
        tail = f", and \"{' '.join(words[:7])}{'…' if len(words) > 7 else ''}\" is due {a:%b} {a.day}"
    return f"{head}: the " + ", then the ".join(parts) + tail


def named_since(text: str, today: dt.date) -> str:
    """The latest date on or before today that a Waiting line names ("form submitted Nov 11"), the
    fallback for when a wait began when no Log line says (tasks/read._wait_since has the Log)."""
    from tools import steps as S   # the one month list (D-10)
    out = ""
    for _wd, mo, d in S.DATE_WORDS.findall(text or ""):
        try:
            x = dt.date(today.year, S.MON[mo.title()[:3]], int(d))
        except (KeyError, ValueError):
            continue
        if x > today:
            x = x.replace(year=today.year - 1)
        out = max(out, x.isoformat())
    return out


def from_steps(steps, today: dt.date, since: dict | None = None) -> list[dict]:
    """tools/steps.Step objects as compute() reads them; a wait's since from `since`, else the
    latest date its line names."""
    since = since or {}
    out = []
    for s in steps:
        if not s.id:
            continue
        sn = since.get(s.id) or (named_since(s.text, today) if s.kind == "Waiting" else "")
        if s.kind == "Waiting" and s.state == "open":   # the clock restarts at his last chase (D-38)
            from tools import steps as S
            c = S.chase_of(steps, s, states=("done",))
            if c and c.done and c.done > sn:
                sn = c.done
        out.append({"id": s.id, "kind": s.kind, "state": s.state, "text": s.text, "after": list(s.after),
                    "due": s.due, "gate": s.gate, "event": s.event, "done": s.done,
                    "optional": s.optional, "span": getattr(s, "span", None), "since": sn})
    return out
