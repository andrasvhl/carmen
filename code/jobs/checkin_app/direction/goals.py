"""Carmen's Goals tab (owner decision, Nov 5): the coming week checked against his goals.

The goals live in one vault note, `Areas/Goals/Current goals.md`, which he edits in the tab or
by hand. Each target names what counts it (`Counted by`); this module counts it for a week from
the calendars, the meditation journal, the race training log and task notes, gives each goal
a count of targets on track, a 5-week trend, and up to three moves for the goal he picked as his
direction. Every change to the goals, from the tab or made by hand in the note, gets one dated
line in the note's `## Change log`. How to change goals, the note format and the metrics:
`_system/checkin-app.md` § Goals tab, which this module must match.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

from runner import config

TZ = ZoneInfo("America/Los_Angeles")
NOTE = config.VAULT / "Areas" / "Goals" / "Current goals.md"
JOURNAL_DIR = config.VAULT / "Areas" / "Personal" / "Meditation Journal"
TRAINING_LOG = config.VAULT / "Projects" / "Ultra 100k" / "training-log.md"
INBOX = config.VAULT / "Inbox"
STATE = config.STATE_DIR / "checkin_app" / "goals.json"
VT = Path.home() / "automation" / "bin" / "vt"
JS = Path(__file__).with_name("goals.js")

# The calendars that say who he is with: main (both accounts), RSVPs (Luma, Partiful), Social.
# Not Focus (his own blocks), not a calendar of events someone else hosts,
# not a class-wide events feed.
SOCIAL_CAL = "<SOCIAL_CALENDAR_ID>"
# Focus holds his own standing blocks (runs among them): read for `runs` only, never as people
# or as busy time.
FOCUS_CAL = "<FOCUS_CALENDAR_ID>"
# Counted only looking back: for a week that has not started they show "counts as the week runs".
HINDSIGHT = ("journal_box", "journal_days")
RSVP_CALS = ("<LUMA_CALENDAR_ID>",    # Luma
             "<PARTIFUL_CALENDAR_ID>")    # Partiful

METRICS = {   # key: (source label, what it counts). The doc's table is written from this.
    "friend_days": ("Calendar", "days with at least one event with people, solo days left out"),
    "lunches": ("Calendar", "days with an event with people starting 11:00-14:00"),
    "dinners": ("Calendar", "days with a dinner with people starting 17:00-21:30"),
    "close_1on1": ("Calendar", "events of up to 5 people naming someone on the Close friends line"),
    "late_nights": ("Calendar", "days with an event with people starting 20:00 or later or ending 22:30 or later"),
    "journal_box": ("Journal", "days whose box (the text after the colon) is ticked under Yesterday in the next day's entry"),
    "journal_days": ("Journal", "days with a meditation journal entry"),
    "calendar": ("Calendar", "events whose title contains one of the comma-separated words"),
    "runs": ("Strava", "runs in the training log; booked runs (🏃 or 'run' in the title) for days still ahead"),
    "long_run": ("Strava", "weekend runs of at least the given km; a booked long run or hike for days still ahead"),
    "task": ("Task", "1 once the named Inbox task is done"),
    "manual": ("Manual", "not counted: listed under Not measured yet"),
}

EXCLUDE = re.compile(r"seminar|workshop|Lecture|Office hours|\bclass\b|Blocker|Commute|Travel to|"   # titles that are never time with people
                     r"Haircut|Focus|Solo day|^Free\b|Hold:", re.I)
PEOPLE = re.compile(r"dinner|lunch|drinks|coffee|hangout|\bx\b|FOAM|BPL|TALK|party|birthday|bday|hike|squash|tennis|"
                    r"chat|aperitivo|brunch|walk|night|gathering|yard|game|trek", re.I)
DINNER = re.compile(r"dinner|supper|aperitivo|birthday|bday|potluck|bbq|restaurant|yard|hangout|cook", re.I)
NOT_DINNER = re.compile(r"FOAM|TALK|BPL|band|happy hour|trek|tennis|squash|night\b", re.I)
RUN = re.compile(r"🏃|\brun\b|long run", re.I)
LONG = re.compile(r"long run|hike|trail", re.I)
VISIT = re.compile(r"\bin (SF|town|Palo Alto|the Bay)\b|visit", re.I)

_LOCK = threading.Lock()
_CACHE: dict = {}


# --- the note -------------------------------------------------------------------------------

def _goal_num(raw: str) -> tuple[int | None, bool]:
    m = re.fullmatch(r"\s*(max\s+)?(\d+)\s*", raw or "", re.I)
    return (int(m.group(2)), bool(m.group(1))) if m else (None, False)


def _by(raw: str) -> tuple[str, str]:
    key, _, arg = (raw or "").partition(":")
    key = key.strip().lower()
    return (key if key in METRICS else "manual", arg.strip())


def parse(text: str) -> dict:
    """The note as data. Lines a goal section has besides Status, Why and the table are kept."""
    lines = text.splitlines()
    first = next((i for i, l in enumerate(lines) if l.startswith("## ")), len(lines))
    head = lines[:first]
    m = {"head": head, "direction": "", "close": [], "goals": [], "log": []}
    for l in head:
        if l.startswith("Direction:"):
            m["direction"] = l.split(":", 1)[1].strip()
        if l.startswith("Close friends:"):
            m["close"] = [n.strip() for n in l.split(":", 1)[1].split(",") if n.strip()]
    cur = None
    for l in lines[first:]:
        if l.startswith("## "):
            title = l[3:].strip()
            if title.lower() == "change log":
                cur = "log"
                continue
            cur = {"name": title, "status": "active", "why": "", "targets": [], "extra": []}
            m["goals"].append(cur)
            continue
        if cur == "log":
            if l.strip().startswith("- "):
                m["log"].append(l.strip()[2:])
            continue
        if cur is None:
            continue
        if l.startswith("Status:"):
            cur["status"] = "paused" if "pause" in l.lower() else "active"
        elif l.startswith("Why:"):
            cur["why"] = l.split(":", 1)[1].strip()
        elif l.lstrip().startswith("|"):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if len(cells) < 3 or cells[0].lower() == "target" or set(cells[0]) <= set("-: "):
                continue
            cur["targets"].append({"name": cells[0], "goal": cells[1], "by": cells[2]})
        elif l.strip():
            cur["extra"].append(l)
    return m


def serialize(m: dict) -> str:
    head = list(m["head"])
    for key, val in (("Direction:", m["direction"]), ("Close friends:", ", ".join(m["close"]))):
        idx = next((i for i, l in enumerate(head) if l.startswith(key)), None)
        if idx is None:
            while head and not head[-1].strip():
                head.pop()
            head += ["", f"{key} {val}"]
        else:
            head[idx] = f"{key} {val}"
    while head and not head[-1].strip():
        head.pop()
    out = head + [""]
    for g in m["goals"]:
        out += [f"## {g['name']}", f"Status: {g['status']}", f"Why: {g['why']}", "",
                "| Target | Goal | Counted by |", "|---|---|---|"]
        out += [f"| {t['name']} | {t['goal']} | {t['by']} |" for t in g["targets"]]
        if g.get("extra"):
            out += [""] + g["extra"]
        out.append("")
    out.append("## Change log")
    out += [f"- {l}" for l in m["log"]]
    return "\n".join(out) + "\n"


def _core(m: dict) -> dict:
    """What the change log tracks: everything but the log itself and free lines."""
    return {"direction": m["direction"], "close": m["close"],
            "goals": [{k: g[k] for k in ("name", "status", "why")} | {"targets": g["targets"]} for g in m["goals"]]}


def diff(old: dict, new: dict) -> list[str]:
    """One plain line per change, in his terms."""
    out = []
    if old["direction"] != new["direction"]:
        out.append(f"Direction: {old['direction'] or 'none'} → {new['direction'] or 'none'}")
    if old["close"] != new["close"]:
        out.append(f"Close friends: {', '.join(old['close']) or 'none'} → {', '.join(new['close']) or 'none'}")
    og = {g["name"]: g for g in old["goals"]}
    ng = {g["name"]: g for g in new["goals"]}
    for name in ng:
        if name not in og:
            out.append(f"Added goal {name}")
    for name in og:
        if name not in ng:
            out.append(f"Removed goal {name}")
    for name, g in ng.items():
        o = og.get(name)
        if not o:
            continue
        if o["status"] != g["status"]:
            out.append(f"{'Paused' if g['status'] == 'paused' else 'Resumed'} {name}")
        if o["why"] != g["why"]:
            out.append(f"{name}: why → \"{g['why']}\"")
        ot = {t["name"]: t for t in o["targets"]}
        nt = {t["name"]: t for t in g["targets"]}
        for tn, t in nt.items():
            if tn not in ot:
                out.append(f"{name}: added target {tn} ({t['goal']}, {t['by']})")
            else:
                if ot[tn]["goal"] != t["goal"]:
                    out.append(f"{name}: {tn} {ot[tn]['goal']} → {t['goal']}")
                if ot[tn]["by"] != t["by"]:
                    out.append(f"{name}: {tn} counted by {ot[tn]['by']} → {t['by']}")
        for tn in ot:
            if tn not in nt:
                out.append(f"{name}: removed target {tn}")
    return out


def _label(d: dt.date) -> str:
    return f"{d:%b} {d.day}"


def _atomic(p: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".goals-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, p)


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    _atomic(STATE, json.dumps(s, ensure_ascii=False, indent=1))


def load(today: dt.date | None = None) -> dict:
    """The note, with any hand edit since Carmen last saw it written into the change log."""
    today = today or dt.datetime.now(TZ).date()
    with _LOCK:
        m = parse(NOTE.read_text(encoding="utf-8"))
        s = _state()
        snap = s.get("snapshot")
        if snap is not None and snap != _core(m):
            lines = diff(snap, _core(m))
            if lines:
                m["log"] += [f"{_label(today)} · {l} · in the note" for l in lines]
                _atomic(NOTE, serialize(m))
        s["snapshot"] = _core(m)
        _save_state(s)
        return m


# --- counting -------------------------------------------------------------------------------

def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def fetch_events(lo: dt.date, hi: dt.date) -> list[dict]:
    """Events from lo to hi (exclusive) on the people calendars, cached 10 min."""
    key = (lo, hi)
    if key in _CACHE and time.time() - _CACHE[key][0] < 600:
        return _CACHE[key][1]
    from jobs.plan_today_prerun import BLOCKING_CALENDARS, _cal_token, _cget
    tok = _cal_token()
    cals = list(dict.fromkeys([*BLOCKING_CALENDARS, SOCIAL_CAL, FOCUS_CAL]))
    out, seen = [], set()
    for cal in cals:
        kind = "social" if cal == SOCIAL_CAL else "focus" if cal == FOCUS_CAL else "rsvp" if cal in RSVP_CALS else "main"
        page = None
        while True:
            q = {"timeMin": dt.datetime.combine(lo, dt.time(0), TZ).isoformat(),
                 "timeMax": dt.datetime.combine(hi, dt.time(0), TZ).isoformat(),
                 "singleEvents": "true", "timeZone": "America/Los_Angeles", "maxResults": 2500}
            if page:
                q["pageToken"] = page
            try:
                got = _cget(tok, f"calendars/{urllib.parse.quote(cal)}/events?{urllib.parse.urlencode(q)}")
            except Exception:
                break   # one calendar unreadable: counted without it
            for e in got.get("items", []):
                if e.get("status") == "cancelled":
                    continue
                att = e.get("attendees", [])
                me = next((a.get("responseStatus") for a in att if a.get("self")), "")
                if me == "declined":
                    continue
                st, en = e.get("start", {}), e.get("end", {})
                uid = (e.get("iCalUID") or e.get("id"), st.get("dateTime") or st.get("date"))
                if uid in seen:
                    continue
                seen.add(uid)
                others = [a for a in att if not a.get("self") and not a.get("resource")]
                ev = {"title": (e.get("summary") or "").strip(), "kind": kind, "n": len(others),
                      "who": " ".join((a.get("displayName") or "") + " " + (a.get("email") or "") for a in others).lower(),
                      "tentative": me in ("needsAction", "tentative")}
                if st.get("date"):
                    d0, d1 = dt.date.fromisoformat(st["date"]), dt.date.fromisoformat(en.get("date", st["date"]))
                    ev |= {"allday": True, "day": d0, "last": d1 - dt.timedelta(days=1), "s": 0, "f": 1440}
                else:
                    a = dt.datetime.fromisoformat(st["dateTime"]).astimezone(TZ)
                    b = dt.datetime.fromisoformat(en.get("dateTime", st["dateTime"])).astimezone(TZ)
                    ev |= {"allday": False, "day": a.date(), "last": a.date(), "s": a.hour * 60 + a.minute,
                           "f": (b.hour * 60 + b.minute) if b.date() == a.date() else 1440 + b.hour * 60 + b.minute}
                out.append(ev)
            page = got.get("nextPageToken")
            if not page:
                break
    out.sort(key=lambda x: (x["day"], x["s"]))
    _CACHE[key] = (time.time(), out)
    return out


def with_people(e: dict) -> bool:
    if e["allday"] or e["kind"] == "focus" or not e["title"] or EXCLUDE.search(e["title"]):
        return False
    return e["n"] >= 1 or e["kind"] in ("rsvp", "social") or bool(PEOPLE.search(e["title"]))


def solo_days(events: list[dict]) -> set:
    return {e["day"] + dt.timedelta(days=i) for e in events if e["allday"] and "solo day" in e["title"].lower()
            for i in range((e["last"] - e["day"]).days + 1)}


def _close_hit(e: dict, close: list[str]) -> str:
    if not with_people(e) or e["n"] > 5:
        return ""
    hay = (e["title"] + " " + e["who"]).lower()
    for name in close:
        first = name.split()[0].lower()
        if re.search(rf"\b{re.escape(name.lower())}\b", hay) or re.search(rf"\b{re.escape(first)}\b", hay):
            return name
    return ""


def _journal(day: dt.date) -> str | None:
    p = JOURNAL_DIR / f"{day.isoformat()}.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _runs() -> list[tuple[dt.date, float]]:
    """(day, km) for each run the training log records: a `### <Day> <d> <Mon> <yyyy>` entry
    with a Strava line."""
    try:
        text = TRAINING_LOG.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for block in re.split(r"(?m)^### ", text)[1:]:
        hm = re.match(r"\w{3} (\d{1,2}) (\w{3}) (\d{4})", block)
        sm = re.search(r"\*\*Strava:\*\* ([\d.]+) km", block)
        if not hm or "**Strava:**" not in block:
            continue
        try:
            day = dt.datetime.strptime(" ".join(hm.groups()), "%d %b %Y").date()
        except ValueError:
            continue
        out.append((day, float(sm.group(1)) if sm else 0.0))
    return out


def _task_state(title: str) -> tuple[bool, str]:
    p = INBOX / f"{title}.md"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False, "task not found in Inbox"
    done = bool(re.search(r"(?m)^status:\s*(done|cancelled)\s*$", text))
    nx = re.search(r"(?m)^- \[ \] (?:Next|Waiting): (.+?)(?:\s+\^\w+)?$", text)
    return done, ("done" if done else (nx.group(1) if nx else "open"))


def count(t: dict, week: dt.date, events: list[dict], close: list[str], today: dt.date) -> tuple[int | None, list[str]]:
    """How many a target has in the week (Mon to Sun), and what was counted, as short lines."""
    key, arg = _by(t["by"])
    if key in HINDSIGHT and week > today:
        return None, ["later"]
    days = [week + dt.timedelta(days=i) for i in range(7)]
    wk = [e for e in events if week <= e["day"] < week + dt.timedelta(days=7)]
    ppl = [e for e in wk if with_people(e)]
    solo = solo_days(wk)
    fmt = lambda e: f"{e['day']:%a} {e['title']}" + (" (not accepted)" if e["tentative"] else "")
    if key == "friend_days":
        hit = {}
        for e in ppl:
            if e["day"] not in solo:
                hit.setdefault(e["day"], e)
        empty = [f"{d:%a} {_label(d)} empty" for d in days if d not in hit and d not in solo and d >= today]
        return len(hit), [fmt(e) for e in hit.values()] + empty
    if key == "lunches":
        hit = {}
        for e in ppl:
            if 660 <= e["s"] <= 810 and not NOT_DINNER.search(e["title"]):
                hit.setdefault(e["day"], e)
        return len(hit), [fmt(e) for e in hit.values()]
    if key == "dinners":
        hit = {}
        for e in ppl:
            if 1020 <= e["s"] <= 1290 and not NOT_DINNER.search(e["title"]) and (DINNER.search(e["title"]) or (e["kind"] == "rsvp" and e["s"] <= 1170) or 2 <= e["n"] <= 20):
                hit.setdefault(e["day"], e)
        return len(hit), [fmt(e) for e in hit.values()]
    if key == "close_1on1":
        hits = [(e, _close_hit(e, close)) for e in wk]
        hits = [(e, n) for e, n in hits if n]
        return len(hits), [f"{fmt(e)} · {n}" for e, n in hits] or ([] if close else ["no Close friends line in the note"])
    if key == "late_nights":
        hit = {}
        for e in ppl:
            if e["s"] >= 1200 or e["f"] >= 1350:
                hit.setdefault(e["day"], e)
        return len(hit), [fmt(e) for e in hit.values()]
    if key == "journal_box":
        want = arg.lower() or "deep interaction"
        yes, lines = 0, []
        for d in days:
            if d >= today:
                continue
            text = _journal(d + dt.timedelta(days=1))
            box = re.search(rf"(?mi)^\s*- \[([ xX])\] .*{re.escape(want)}", text or "")
            if box and box.group(1).lower() == "x":
                yes += 1
                lines.append(f"{d:%a} yes")
            elif d not in solo:
                lines.append(f"{d:%a} {'no' if box else 'no entry'}")
        return yes, lines
    if key == "journal_days":
        got = [d for d in days if _journal(d) is not None]
        return len(got), [", ".join(f"{d:%a}" for d in got) or "none yet"]
    if key == "calendar":
        words = [w.strip().lower() for w in arg.split(",") if w.strip()]
        hits = [e for e in wk if not e["allday"] and any(w in e["title"].lower() for w in words)]
        return len(hits), [fmt(e) for e in hits]
    if key in ("runs", "long_run"):
        km = float(arg) if key == "long_run" and re.fullmatch(r"[\d.]+", arg or "") else 0.0
        past = [(d, k) for d, k in _runs() if week <= d < min(today, week + dt.timedelta(days=7))]
        if key == "long_run":
            past = [(d, k) for d, k in past if d.weekday() >= 5 and k >= km]
        ahead = [e for e in wk if e["day"] >= today and not e["allday"]
                 and (LONG.search(e["title"]) and e["day"].weekday() >= 5 if key == "long_run" else RUN.search(e["title"]))]
        lines = [f"{d:%a} {k:.1f} km" if k else f"{d:%a} run, no distance" for d, k in past]
        return len(past) + len(ahead), lines + [f"{fmt(e)} (booked)" for e in ahead]
    if key == "task":
        done, state = _task_state(arg)
        return int(done), [state]
    return None, []


def status(n: int | None, t: dict, share: float = 1.0) -> str:
    """good, warn (close) or bad. share < 1 for a hindsight target in the running week: judged
    against the part of the week that has passed."""
    num, is_max = _goal_num(t["goal"])
    if n is None or num is None:
        return "idle"
    if not is_max and share < 1:
        num = math.ceil(num * share)
    if is_max:
        return "good" if n <= num else "warn" if n == num + 1 else "bad"
    return "good" if n >= num else "warn" if n >= math.ceil(num * 2 / 3) and n > 0 else "bad"


def _count_text(n: int | None, t: dict) -> str:
    num, is_max = _goal_num(t["goal"])
    if n is None:
        return "not counted"
    if num is None:
        return str(n)
    return f"{n}, max {num}" if is_max else f"{n} of {num}"


# --- moves ----------------------------------------------------------------------------------

def _free(day: dt.date, s: int, f: int, events: list[dict]) -> bool:
    return not any(e["day"] == day and not e["allday"] and e["kind"] != "focus" and e["s"] < f and e["f"] > s
                   and not re.match(r"Free\b", e["title"]) for e in events)


def _friend_order(close: list[str], events: list[dict]) -> list[str]:
    last = {n: dt.date.min for n in close}
    for e in events:
        n = _close_hit(e, close)
        if n:
            last[n] = max(last[n], e["day"])
    return sorted(close, key=lambda n: last[n])


def moves(g: dict, week: dt.date, events: list[dict], close: list[str], today: dt.date, rows: list[dict],
          dismissed: set) -> list[dict]:
    """At most three, each for a target that is short, each with a real slot or a real list."""
    wk = [e for e in events if week <= e["day"] < week + dt.timedelta(days=7)]
    solo = solo_days(wk)
    ahead = [week + dt.timedelta(days=i) for i in range(7) if week + dt.timedelta(days=i) > today]
    ahead = [d for d in ahead if d not in solo]
    friends = _friend_order(close, events) or ["a friend"]
    fi = iter(friends * 3)
    used_days, out = set(), []
    has_people = {e["day"] for e in wk if with_people(e)}

    def add(row, gap, what, day=None, task=True):
        sig = f"{week}|{g['name']}|{row['name']}|{row['n']}"
        if sig in dismissed or len(out) >= 3:
            return
        out.append({"sig": sig, "target": row["name"], "gap": gap, "what": what,
                    "date": day.isoformat() if day else "", "task": task})
        if day:
            used_days.add(day)

    for row in rows:
        if row["status"] in ("good", "idle", "later") or len(out) >= 3:
            continue
        key, arg = _by(row["by"])
        if key == "friend_days":
            empty = [d for d in ahead if d not in has_people and d not in used_days]
            if empty:
                d = empty[0]
                visit = next((e for e in wk if e["allday"] and e["kind"] == "social" and VISIT.search(e["title"])
                              and e["day"] <= d <= e["last"]), None)
                who = visit["title"].split(" in ")[0] if visit else next(fi)
                gap = f"{d:%a} {_label(d)} has nothing with people." + (f" {visit['title']}." if visit else "")
                add(row, gap, f"Plan {d:%a} {_label(d)} with {who}", d)
        elif key == "lunches":
            d = next((d for d in ahead if d not in used_days and _free(d, 720, 780, wk)
                      and d.weekday() < 5), None) or next((d for d in ahead if d not in used_days and _free(d, 720, 780, wk)), None)
            if d:
                add(row, f"{row['count']} lunch{'' if row['count'] == 1 else 'es'} booked. {d:%a} {_label(d)} 12:00 is free.",
                    f"Lunch {d:%a} {_label(d)} 12:00 with {next(fi)}", d)
        elif key in ("dinners", "close_1on1"):
            d = next((d for d in ahead if d not in used_days and _free(d, 1110, 1230, wk)), None)
            if d:
                gap = (f"{row['count']} dinner{'' if row['count'] == 1 else 's'} booked." if key == "dinners" else "No 1-1 with a close friend booked.")
                add(row, f"{gap} {d:%a} {_label(d)} evening is free.",
                    f"{'Dinner' if key == 'dinners' else '1-1'} {d:%a} {_label(d)} with {next(fi)}", d)
        elif key == "late_nights":
            add(row, f"{row['count']} late night{'' if row['count'] == 1 else 's'} booked.", "Pick one evening to leave by 22:30: " + "; ".join(row["detail"][:5]), task=False)
        elif key in ("runs", "long_run"):
            if key == "long_run":
                d = next((d for d in ahead if d.weekday() == 5), None) or next((d for d in ahead if d.weekday() == 6), None)
                if d:
                    add(row, "No long run at the weekend yet.", f"Long run {d:%a} {_label(d)} morning", d)
            else:
                pref = sorted([d for d in ahead if d.weekday() < 5 and d not in used_days], key=lambda d: (d.weekday() not in (0, 2), d))
                if pref:
                    d = pref[0]
                    add(row, f"{row['count']} runs so far.", f"Run {d:%a} {_label(d)}", d)
        elif key == "calendar":
            d = next((d for d in ahead if d not in used_days), None)
            if d:
                add(row, f"{row['name']}: {row['count']} booked.", f"Book {row['name'].lower()} on {d:%a} {_label(d)}", d)
        elif key == "task":
            add(row, f"Open: {row['detail'][0] if row['detail'] else arg}", arg, task=False)
    return out


# --- the view -------------------------------------------------------------------------------

def _rows(g, week, events, close, today):
    rows = []
    for t in g["targets"]:
        n, detail = count(t, week, events, close, today)
        key, _ = _by(t["by"])
        later = n is None and detail == ["later"]
        share = 1.0
        if key in HINDSIGHT and week <= today < week + dt.timedelta(days=7):
            share = ((today - week).days + (key == "journal_days")) / 7
        rows.append({"name": t["name"], "goal": t["goal"], "by": t["by"], "n": n, "count": "" if n is None else n,
                     "text": "counts as the week runs" if later else _count_text(n, t),
                     "status": "later" if later else status(n, t, share), "src": METRICS[key][0],
                     "detail": [] if later else detail})
    return rows


def view(which: str = "next", today: dt.date | None = None, m: dict | None = None) -> dict:
    today = today or dt.datetime.now(TZ).date()
    m = m or load(today)
    this = monday(today)
    week = this + dt.timedelta(days=7) if which == "next" else this
    first = this - dt.timedelta(days=28)
    calendar_ok = True
    try:
        events = fetch_events(first, this + dt.timedelta(days=14))
    except Exception:
        events, calendar_ok = [], False
    dismissed = set(_state().get("dismissed", {}))
    goals = []
    for g in m["goals"]:
        rows = _rows(g, week, events, m["close"], today)
        measured = [r for r in rows if r["status"] not in ("idle", "later")]
        trend = []
        for k in range(4, -1, -1):
            w = this - dt.timedelta(days=7 * k)
            rs = [r for r in _rows(g, w, events, m["close"], today) if r["status"] not in ("idle", "later")]
            trend.append({"wk": _label(w), "pct": round(100 * sum(r["status"] == "good" for r in rs) / len(rs)) if rs else None})
        goals.append({"name": g["name"], "active": g["status"] == "active", "why": g["why"],
                      "on": sum(r["status"] == "good" for r in measured), "of": len(measured),
                      "rows": [r for r in rows if r["status"] != "idle"],
                      "unmeasured": [r["name"] for r in rows if r["status"] == "idle"],
                      "targets": g["targets"], "trend": trend,
                      "moves": moves(g, week, events, m["close"], today, rows, dismissed)
                      if g["name"] == m["direction"] else []})
    end = week + dt.timedelta(days=6)
    return {"week": which, "label": f"{_label(week)} to {_label(end)}", "today": today.isoformat(),
            "direction": m["direction"], "close": m["close"], "goals": goals, "log": list(reversed(m["log"]))[:15],
            "metrics": {k: v[1] for k, v in METRICS.items()}, "calendar_ok": calendar_ok,
            "note": "obsidian://open?vault=<VAULT_NAME>&file=" + urllib.parse.quote("Areas/Goals/Current goals")}


# --- edits ----------------------------------------------------------------------------------

def _goal(m, name):
    g = next((g for g in m["goals"] if g["name"] == name), None)
    if not g:
        raise ValueError(f"no goal called {name}")
    return g


def _clean(s, limit=160) -> str:
    return re.sub(r"[|\n\r]+", " ", str(s or "")).strip()[:limit]


def apply_op(m: dict, op: dict) -> None:
    kind = op.get("op")
    if kind == "direction":
        m["direction"] = _goal(m, op["goal"])["name"]
    elif kind == "rename":
        g, new = _goal(m, op["goal"]), _clean(op.get("name"), 80)
        if not new or new.lower() == "change log" or any(x["name"] == new for x in m["goals"]):
            raise ValueError("that name is empty or taken")
        if m["direction"] == g["name"]:
            m["direction"] = new
        g["name"] = new
    elif kind == "why":
        _goal(m, op["goal"])["why"] = _clean(op.get("why"), 300)
    elif kind == "pause":
        g = _goal(m, op["goal"])
        g["status"] = "active" if g["status"] == "paused" else "paused"
    elif kind == "target":
        g = _goal(m, op["goal"])
        i = int(op["index"])
        t = g["targets"][i]
        for f, src in (("name", "name"), ("goal", "value"), ("by", "by")):   # "goal" in op is the goal's name
            if src in op and _clean(op[src]):
                t[f] = _clean(op[src], 120)
    elif kind == "add_target":
        g = _goal(m, op["goal"])
        name = _clean(op.get("name"), 80) or "New target"
        if any(t["name"] == name for t in g["targets"]):
            raise ValueError("that target already exists")
        g["targets"].append({"name": name, "goal": _clean(op.get("value")) or "1", "by": _clean(op.get("by")) or "manual"})
    elif kind == "remove_target":
        g = _goal(m, op["goal"])
        g["targets"].pop(int(op["index"]))
    elif kind == "add_goal":
        name = _clean(op.get("name"), 80)
        if not name or name.lower() == "change log" or any(x["name"] == name for x in m["goals"]):
            raise ValueError("that name is empty or taken")
        m["goals"].append({"name": name, "status": "active", "why": _clean(op.get("why"), 300), "targets": [], "extra": []})
    elif kind == "close":
        m["close"] = [_clean(n, 60) for n in str(op.get("names", "")).split(",") if _clean(n)]
    else:
        raise ValueError(f"unknown change {kind}")


def edit(op: dict, test: bool = False, today: dt.date | None = None) -> dict:
    """One change from the tab: written to the note with its change-log line (test: nothing written)."""
    today = today or dt.datetime.now(TZ).date()
    m = load(today)
    with _LOCK:
        if op.get("op") == "dismiss":
            if not test:
                s = _state()
                s.setdefault("dismissed", {})[op["sig"]] = today.isoformat()
                s["dismissed"] = {k: v for k, v in s["dismissed"].items() if v >= (today - dt.timedelta(days=21)).isoformat()}
                _save_state(s)
            return view(op.get("week", "next"), today, m)
        before = json.loads(json.dumps(_core(m)))
        apply_op(m, op)
        lines = diff(before, _core(m))
        m["log"] += [f"{_label(today)} · {l} · Carmen" for l in lines]
        if lines and not test:
            _atomic(NOTE, serialize(m))
            s = _state()
            s["snapshot"] = _core(m)
            _save_state(s)
    return view(op.get("week", "next"), today, m)


def add_task(title: str, day: str, test: bool = False) -> str:
    title = _clean(title, 140)
    if test:
        return f"would add {title}"
    cmd = [str(VT), "todos", "--add", title]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        cmd += ["--scheduled", day]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or "vt todos failed")
    return title


# --- routes ---------------------------------------------------------------------------------

def routes(base: str, authed) -> list:
    import asyncio
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    async def get(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            return JSONResponse(await asyncio.to_thread(view, req.query_params.get("week", "next")))
        except OSError as exc:
            return JSONResponse({"error": f"the goals note could not be read ({exc})"}, status_code=503)

    async def post(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        body = await req.json()
        test = bool(body.get("test"))
        try:
            if body.get("op") == "task":
                done = await asyncio.to_thread(add_task, body.get("title", ""), body.get("date", ""), test)
                return JSONResponse({"ok": True, "added": done})
            return JSONResponse(await asyncio.to_thread(edit, body, test))
        except (ValueError, KeyError, IndexError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def js(req: Request):
        return Response(JS.read_bytes(), media_type="application/javascript", headers={"Cache-Control": "no-store"})

    return [Route(f"{base}/api/goals", get), Route(f"{base}/api/goals", post, methods=["POST"]),
            Route(f"{base}/goals.js", js)]
