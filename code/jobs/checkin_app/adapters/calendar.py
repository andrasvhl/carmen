"""Calendar adapter: one read path and one cache for his calendars. day_events reads a day from every
relevant calendar (blocking ones must answer), cached 3 min, the last good read kept an hour.
Signals reads evidence from it, Planning capacity and the day view. Carmen itself books nothing: the
Plan chat creates blocks through its own calendar tools, so the write path is not in this server
yet.
"""
from __future__ import annotations

import datetime as dt
import re
import time

from . import clock
from . import taplog


_BUSY_CACHE: dict[str, tuple[float, list]] = {}

# The day view shows his calendars merged, the ones ticked in his Calendar app (owner decision, Oct 31,
# with a screenshot of it: "whatever i am seeing here"): all but the class-wide GSB events and the
# club scheduling overlay. Only the blocking calendars count as busy, so the budget is unchanged.
DAY_VIEW_SKIP = ("c_d42df2aba5f245b77ce1e699b4ccf9b1d3799c668e9d799414a810c536",   # Club Events Scheduling Tool
                 "c_188beuv4stefoghmhh23tmd1ibidm@resource")                       # GSB MBA 2027 Events

SLOT_RE = re.compile(r"big rocks?|admin backup|^\W*🔒", re.I)

CAL_KIND = {"Focus": "focus", "Social": "social", "Luma": "social", "Partiful": "social",
            "<YOUR_PERSONAL_EMAIL>": "personal"}


def task_links(desc: str) -> list[str]:
    """The task notes a calendar block links (its Task: lines), as note names."""
    from urllib.parse import unquote
    return [unquote(m).rsplit("/", 1)[-1] for m in
            re.findall(r"Task:\s*obsidian://open\?[^\s<\"]*?file=([^&\s<\"]+)", desc or "")]


def _event_key(uid: str) -> str:
    from tools.taskids import event_key
    return event_key(uid)


def task_ids(desc: str) -> list[str]:
    """The task IDs a block names: `Task: <link> (t-xxxxxx)`, one per task (Nov 3)."""
    return re.findall(r"Task:[^\n]*?\((t-[0-9a-f]{6})\)", desc or "")


def day_events(day: dt.date, fresh: bool = False) -> list[dict] | None:
    """The day's events on his relevant calendars, cached 3 min: s and f in minutes of the day,
    busy only for a blocking calendar's opaque events, tentative for an invite he has not
    accepted. Declined ones and duplicates (one invite on two calendars) are left out. None if
    unreadable. Feeds the budget and the Plan chat's day view (Oct 31)."""
    key = day.isoformat()
    if not fresh and key in _BUSY_CACHE and time.time() - _BUSY_CACHE[key][0] < 180:
        return _BUSY_CACHE[key][1]
    try:
        import urllib.parse
        from concurrent.futures import ThreadPoolExecutor
        from jobs.plan_today_prerun import BLOCKING_CALENDARS, _cal_token, _cget
        tok = _cal_token()
        lo = dt.datetime.combine(day, dt.time(0), clock.TZ).isoformat()
        hi = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), clock.TZ).isoformat()
        cals = [(c["id"], c.get("summary", "")) for c in _cget(tok, "users/me/calendarList").get("items", [])
                if not c["id"].startswith(DAY_VIEW_SKIP)]
        for cal in BLOCKING_CALENDARS:   # never lose a blocking one, even if the list call misses it
            if cal not in {c for c, _ in cals}:
                cals.append((cal, cal))
        q = urllib.parse.urlencode({"timeMin": lo, "timeMax": hi, "singleEvents": "true",
                                    "timeZone": "America/Los_Angeles", "maxResults": 250})

        def items(cal):
            try:
                return _cget(tok, f"calendars/{urllib.parse.quote(cal)}/events?{q}").get("items", [])
            except Exception:
                return None
        # All calendars at once (Nov 1: one after the other, 20 of them took 6 s, and the page waited).
        with ThreadPoolExecutor(max_workers=8) as pool:
            got = list(pool.map(items, [c for c, _ in cals]))
        # A blocking calendar that fails makes the budget wrong, so that counts as unreadable;
        # an overlay calendar that fails (one removed or unshared) is just left out.
        if any(g is None and c in BLOCKING_CALENDARS for (c, _), g in zip(cals, got)):
            raise RuntimeError("a blocking calendar could not be read")
        events, seen = [], set()
        for (cal, name), evs in zip(cals, got):
            for e in evs or []:
                me = next((a.get("responseStatus") for a in e.get("attendees", []) if a.get("self")), "")
                st, en = e.get("start", {}), e.get("end", {})
                uid = (e.get("iCalUID") or e.get("id"), st.get("dateTime") or st.get("date"))
                if me == "declined" or uid in seen:
                    continue
                seen.add(uid)
                title = (e.get("summary") or "Busy").strip()
                kind = "canvas" if "Canvas" in name else CAL_KIND.get(name, "main" if cal in BLOCKING_CALENDARS else "other")
                # What a tap on the block shows (Oct 31): the calendar, where, and the description.
                desc = re.sub(r"\s*\n\s*\n\s*", "\n", re.sub(r"<br\s*/?>|</p>|</li>", "\n", e.get("description") or ""))
                info = {"cal": name if "@" not in name else ("Stanford" if "stanford" in name else "Personal"),
                        "loc": (e.get("location") or "")[:160],
                        "desc": re.sub(r"<[^>]+>", "", desc).strip()[:600],
                        # Nov 3: a batch lists its tasks' links after the 600 shown characters,
                        # so the names are read from the whole description.
                        "tasks": task_links(desc), "task_ids": task_ids(desc),
                        "key": _event_key(e.get("iCalUID") or e.get("id", ""))}
                raw = e.get("description") or ""
                slot = bool(SLOT_RE.search(title)) or raw.lstrip().startswith("SCHEDULABLE BLOCKER")
                if not st.get("dateTime"):
                    events.append({"s": 0, "f": 24 * 60, "title": title, "allday": True, "busy": False, "kind": kind, **info})
                    continue
                a = dt.datetime.fromisoformat(st["dateTime"]).astimezone(clock.TZ)
                b = dt.datetime.fromisoformat(en.get("dateTime", st["dateTime"])).astimezone(clock.TZ)
                events.append({"s": a.hour * 60 + a.minute if a.date() == day else 0,
                               "f": b.hour * 60 + b.minute if b.date() == day else 24 * 60,
                               "title": title, "allday": False, "kind": kind, **info,
                               "tentative": me in ("needsAction", "tentative"), "slot": slot,
                               "busy": not slot and (
                                   (cal in BLOCKING_CALENDARS and e.get("transparency") != "transparent"
                                    and me not in ("needsAction", "tentative"))
                                   or (kind == "focus" and "Task:" not in raw))})
        events.sort(key=lambda x: (x["s"], x["f"]))
        _BUSY_CACHE[key] = (time.time(), events)
        return events
    except Exception as exc:   # calendar down or token gone: the last good read, else the budget says so
        taplog.log_event("error", where="calendar", detail=repr(exc)[:200])
        if key in _BUSY_CACHE and time.time() - _BUSY_CACHE[key][0] < 3600:
            return _BUSY_CACHE[key][1]
        return None
