"""Planning, what the tabs and other modules read: the day's view (the one thing and TOP_N decisions
ranked, his selection, tomorrow ranked the same way, Replies and the Claude tab, Do) built from the
pre-run, the task notes and his overrides, and the plan prompt with its time budget. Plan and Do are
two views of the same day: Do never re-ranks, its order changes only through Planning's actions. The
rank keys are frozen for the day (frozen_keys), saved only by taps and the minute tick.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from tools import steps as S   # the one place for step-line facts (Nov 11)
from pathlib import Path

from runner import config

from . import capacity
from .. import runs
from ..adapters import calendar
from ..adapters import clock
from ..adapters import notes
from ..adapters import state
from ..adapters import taplog
from ..direction import read as direction
from ..outbox import read as outbox
from ..signals import read as signals
from ..tasks import read as tasks


TOP_N = 5                  # decisions under the one thing; a hard cap, not a filter (Oct 30)

# What he selected to plan is never cut to TOP_N (Nov 5: the 7th task, FNV prep, pulled up from
# More, was silently left out of the plan). PLAN_MAX is only a safety cap; anything over it, or
# any ID the view cannot find, is named in the plan prompt and logged, never dropped silently.
PLAN_MAX = 30


def plan_ids(body: dict) -> tuple[list[str], list[str]]:
    """The selected IDs to plan, and those over PLAN_MAX."""
    raw = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else [])]
    return raw[:PLAN_MAX], raw[PLAN_MAX:]


def plan_gaps_note(over: list[str], missing: list[str], where: str) -> str:
    """The line the plan prompt gets when not every selected task made it into the list."""
    if not over and not missing:
        return ""
    taplog.log_event("error", where=where, detail=f"plan list short: {len(over)} over cap, missing {missing[:10]}")
    parts = []
    if over:
        parts.append(f"{len(over)} more selected beyond the list's cap: {', '.join(over[:20])}")
    if missing:
        parts.append(f"selected but not found in the view: {', '.join(missing[:20])}")
    return ("\n\nNot every selected task is listed above (" + "; ".join(parts) + "). Find them in the vault"
            " by ID or name and plan them too, or tell him which ones you could not place.")


PRIO = {"H": 0, "N": 1, "Z": 2}


def home_tab(r: dict) -> str:
    """Each task has one home tab (owner decision, Oct 30). A drafted message and a next
    step of 5 min or less: Replies. Claude can act without a message: Claude.
    Everything else: Today."""
    mins = tasks._minutes(r["est"])
    if r["draft"] and mins is not None and mins <= 5:
        return "replies"
    if r["claude"] and not r["draft"]:
        return "claude"
    return "today"


def chunk_feedback_rows() -> list[dict]:
    """His chunk feedback (planning/chunkreview), for the estimator's examples (Tasks)."""
    from . import chunkreview
    return chunkreview.rows()


# The pre-run's group for tasks past a date (Nov 10, Tier 2.8): it holds soft scheduled dates as well
# as due dates, so "Overdue" said too much. Older pre-runs and the morning panel still write "Overdue".
PAST = "Past its date"


def room(day: dt.date) -> int | None:
    """The day's minutes for tasks from now: free time plus the slots (Big rocks, Admin backup),
    the number the budget card shows. None when the calendar cannot be read."""
    try:
        b = capacity.budget(day, [])
    except Exception:   # noqa: BLE001
        return None
    if b["free_min"] is None:
        return None
    st = b.get("start", b["day_start"])
    return b["free_min"] + sum(max(0, min(x["f"], b["day_end"]) - max(x["s"], st)) for x in b["slots"])


def _with_room(day: dt.date):
    """Sizes every task's suggested piece by the day's free time while a view or a plan is built
    (flag step_order, Nov 11). Returns the token to reset, or None."""
    from ..adapters import flags
    if not flags.on("step_order"):
        return None
    return tasks.ROOM.set(room(day))


def build_view(data: dict, today: dt.date | None = None) -> dict:
    tok = _with_room(today or clock.carmen_day())
    try:
        return _build_view(data, today)
    finally:
        if tok is not None:
            tasks.ROOM.reset(tok)


def _build_view(data: dict, today: dt.date | None = None) -> dict:
    """Today (the one thing, at most TOP_N decisions, folds), Replies and Claude.

    The pre-run supplies the ranking, the why and the drafts; the task note
    supplies everything that can change during the day. Closed tasks and tasks
    moved past today drop out of today's lists."""
    today = today or clock.carmen_day()
    data = signals.meditation.with_meditation(data, today)
    try:   # the notes Carmen made for cards (cardnotes, Nov 4), so a row without lk finds its note
        from ..tasks.read import cardnotes
        made = cardnotes.load()
    except Exception as exc:   # a layer on top; the view must never fail on it
        taplog.log_event("error", where="made.json", detail=repr(exc)[:200])
        cardnotes, made = None, {}
    rows = []
    ignored = tasks._ignored_props()
    for group, items in data.get("rows", []):
        for r in items:
            try:
                p = notes.task_path(r.get("lk") or "")
                if r.get("lk") and not p:
                    continue
                # A row without its link that names a task note exactly is that task (Nov 3: the
                # CS 351Z row came back from an update without "lk", so the Start panel showed the
                # update's guess as Next and none of the note's steps or its DoD).
                if not p and not r.get("lk") and notes.note_name(r.get("n") or ""):
                    s = notes.note_name(r["n"])
                    p, r = notes.task_path(s), {**r, "lk": s}
                # A card whose note Carmen made on a tap, while the row it came from has no lk yet.
                if not p and not r.get("lk") and cardnotes and made:
                    s = cardnotes.stem_for(r.get("n") or "", made)
                    if s:
                        p, r = notes.task_path(s), {**r, "lk": s}
                drop_note = ""
                live = tasks.read_note(p) if p else {}
                if live.get("status") in ("done", "cancelled"):
                    continue
                ch = [a for a in (r.get("ch") or []) if isinstance(a, dict)]
                draft = next((a["msg"] for a in ch if a.get("msg")), None)
                row = {
                    "id": r.get("lk") or r.get("n"),
                    # False only for a live meditation row before its first tap (cardnotes).
                    "task": bool(p),
                    "title": r.get("n", ""),
                    "group": PAST if group == "Overdue" else group,
                    "prio": r.get("p", "Z"),
                    "guess": r.get("g", ""),
                    "unsure": bool(r.get("lc")),
                    # The pre-run's and update.py's "Mark as done" (and "Nothing left") on a done
                    # guess is a verdict, never a step: it became Do's card title on Nov 4.
                    "next": live.get("next") or signals._real_next(r.get("nx", "")),
                    "why": r.get("s", ""),
                    # The reason for the day's Plan card (Nov 10, Tier 2.3): written by the pre-run
                    # opening with the signal, absolute dates only; the page falls back to `why`.
                    "why_day": r.get("wd", ""),
                    # An update's note (update.py "upd"; older overlays wrote it into m) is shown
                    # in Details and the summary on top, never in the card's tag line (Oct 31).
                    "meta": drop_note or ("" if str(r.get("m", "")).startswith("Update") else r.get("m", "")),
                    "update": r.get("upd") or (r.get("m", "") if str(r.get("m", "")).startswith("Update") else ""),
                    "waiting": live.get("wait_line") or r.get("wf", ""),
                    # The wait looks over (Nov 3): the pre-run saw the answer (gw 0) on a task
                    # still marked waiting; Do brings it back from the Waiting fold.
                    "wait_over": r.get("gw") == 0 and bool(live.get("waiting")),
                    "med_pending": bool(r.get("mp")),
                    # A "(N min)" he set on the Next line wins over the pre-run's guess.
                    "est": (lambda n: f"{n}m" if n else "")(S.est_min(live.get("next", "")))
                           or (lambda x: f"{x}m" if isinstance(x, (int, float)) else x)(r.get("ne") or r.get("e")) or "",
                    "due": live.get("due") or "",
                    "anx": bool(r.get("anx")),
                    "urgent": bool(r.get("u")),
                    "claude": [a.get("t", "") for a in ch if not a.get("msg")],
                    # cp (Oct 30): steps Claude can take itself to move the task, even if he finishes it.
                    "prep": [a.get("t", "") for a in (r.get("cp") or []) if isinstance(a, dict) and a.get("t")][:3],
                # Rewrites the pre-run only proposed for his own step lines (Nov 1): one tap each.
                "prop": [{"old": x["old"], "new": x.get("new") or "", "why": str(x.get("why") or "")[:200],
                          **({"field": x["field"], "due": str(x.get("due") or "")} if x.get("field") in ("optional", "due", "reference", "split", "after", "move_in", "name_link") else {}),
                          **({"after": str(x.get("after") or "")} if x.get("field") == "after" else {}),
                          # Its own tap labels (Nov 12, the consistency check: Strike / Keep).
                          **({"yes": str(x["yes"])[:24]} if isinstance(x.get("yes"), str) and x["yes"] else {}),
                          **({"no": str(x["no"])[:24]} if isinstance(x.get("no"), str) and x["no"] else {}),
                          **({"parts": [str(y)[:200] for y in x["parts"]][:6]} if x.get("field") == "split" and isinstance(x.get("parts"), list) else {})}
                         for x in (r.get("prop") if isinstance(r.get("prop"), list) else [])
                         if isinstance(x, dict) and isinstance(x.get("old"), str) and isinstance(x.get("new", ""), str)
                         and x["old"] not in ignored.get(r.get("lk") or r.get("n") or "", [])][:3],
                    # acct, needs_slot and the draft's time feed Send in Replies (Oct 31).
                    "draft": {**{k: draft.get(k, "") for k in ("via", "to", "subj", "body", "acct")},
                              "needs_slot": bool(draft.get("needs_slot") or outbox.SLOT.search(draft.get("body") or "")),
                              "at": data.get("updated") or data.get("generated", "")} if draft else None,
                    "later": tasks.is_later(live, today),
                    "detail": tasks._detail(live, r.get("e", ""), r.get("ne", "")),
                }
                row["tab"] = home_tab(row)
                rows.append(row)
            except Exception as exc:   # one bad row hides itself; the rest of the day still shows (Nov 1)
                taplog.log_event("error", where="row", row=str(r.get("n") or r.get("lk") or "")[:120], detail=repr(exc)[:200])

    def rank(r):
        return (0 if r["anx"] else 1, 0 if r["urgent"] else 1,
                PRIO.get(r["prio"], 3), 0 if r["group"] == PAST else 1)

    tomorrow = today + dt.timedelta(days=1)
    ov = load_overrides()
    # What his meditations said about each task in the last 30 days (owner decision, Oct 31: a worry
    # named again and again is eating mental space, so it ranks higher each time; priority
    # itself is only offered, after 3 mentions, never changed for him).
    try:
        from ..signals.read import mentions as _mn
        mload, mcount = _mn.load(today), _mn.count(today)
    except Exception:   # the ledger is a layer on top; the view must never fail on it
        _mn, mload, mcount = None, {}, {}
    for r in rows:
        r["mind"], r["load"] = mcount.get(r["id"], 0), round(mload.get(r["id"], 0.0), 1)
        r["raise"] = bool(_mn and r["task"] and r["mind"] >= _mn.OFFER_AT and r["prio"] != "H"
                          and not direction.raise_dismissed(r["id"], today))
    by_id = {r["id"]: r for r in rows}

    live_rows = [r for r in rows if not r["later"]]
    # A reply the pre-run or a Refresh judged answered, or now waiting on the other side,
    # leaves Replies (Nov 2: Refresh set it to done and it stayed); Plan's folds still have it.
    # Skip in Replies hides the reply for the day and nothing else (Nov 3): the task keeps its
    # date and its place on Do.
    skipped = set(ov.get("reply_skip", {}).get(today.isoformat(), []))
    replies = sorted([r for r in live_rows if r["tab"] == "replies" and r["guess"] != "done"
                      and r["group"] != "Waiting" and r["id"] not in skipped], key=rank)
    # Never offer a message he already sent on that step (QA Nov 8, P1: a Sent invite came back
    # after midnight, ready to go to the group twice). A new step, or a new message in, brings it back.
    replies = [r for r in replies if not (r.get("task") and _safe(tasks.reply_already_sent, r["id"],
                                                                     where="reply guard", default=False))]
    # ToDo is his day, picked by what matters rather than by which tab a task lives in
    # (owner decision, Oct 31): every task competes for it, Claude tasks included.
    # Replies and ToDo are not exclusive (owner decision, Oct 31): a task with a draft is listed in
    # Replies for sending and competes for ToDo like any other, by the same ranking.
    mine = list(live_rows)
    done = [r for r in mine if r["guess"] == "done"]
    # Waiting by the one rule (Nov 10, waits per step): a Waiting line open and no free step of his,
    # the same function as Do's fold. A row without a task note keeps the pre-run's group.
    # D-25
    def is_waiting(r):
        d = r.get("detail") or {}
        return bool(d.get("task_waiting")) if r.get("task") and d else r.get("group") == "Waiting"
    waiting = [r for r in mine if is_waiting(r) and r not in done]

    def pick(r):
        """Due dates are hard, then today's meditation (the first steps he chose against
        his anxiety), then the pre-run's today call (where North Star is weighed), then
        anxiety, urgency and priority."""
        # The due: field, not the pre-run's Overdue group: that one also holds every
        # task whose soft scheduled date has passed.
        due = bool(r["due"] and r["due"] <= today.isoformat())
        # Mental load from the meditations (mentions.load): from about three recent anxious
        # mentions up it outranks today's meditation and the pre-run's call; below that it
        # breaks ties, more mentions first.
        heavy = r.get("load", 0) >= 3
        return (0 if due else 1, 0 if heavy else 1, 0 if r["group"] == signals.meditation.MED else 1,
                0 if r["guess"] == "today" else 1, -r.get("load", 0)) + rank(r)

    # A task he pulled up from below stays out of Claude's ranking and keeps his order after
    # Claude's picks (Oct 31: promoting one made it jump into the middle of the list).
    pulled_today = set(ov["pulled"].get(today.isoformat(), []))
    pool = [r for r in mine if r not in waiting + done and r["id"] not in pulled_today]
    fz = frozen_keys(pick, pool, "today", today)
    # Left open is high signal (owner decision, Nov 5: "Pre-selected should populate the selection from
    # Left over from yesterday as well though, and consider these as high signal"): a task an
    # earlier day left open, and not dropped, ranks level with today's meditation rows.
    try:
        from . import do as _dm
        _e = _dm.ensure(today, {r["id"] for r in rows},
                        lambda i: tasks.read_note(notes.task_path(i)) if notes.task_path(i) else None, write=False)
        left_since = {k: v for k, v in _e["carried"].items() if k not in _e["dropped"]}
    except Exception as exc:   # a layer on top; the ranking must never fail on it
        taplog.log_event("error", where="left_open rank", detail=str(exc)[:200])
        left_since = {}
    for r in rows:
        if r["id"] in left_since:
            try:
                r["left_from"] = clock._label(dt.date.fromisoformat(left_since[r["id"]]))
            except ValueError:
                r["left_from"] = "earlier"

    # Push-down (Nov 10): what he took out again and again sinks, right after the due key, live
    # (only his own taps change it); a task due by the day is never pushed.
    try:
        from . import pushdown as _pd
        _pdata = _pd.load()
        push = lambda r, d: 0 if r.get("due") and r["due"] <= d.isoformat() else _pd.level(r["id"], d, _pdata)  # noqa: E731
    except Exception as exc:
        taplog.log_event("error", where="pushdown", detail=repr(exc)[:200])
        push = lambda r, d: 0  # noqa: E731

    def _lifted(r):
        k = fz[r["id"]]
        k = k[:2] + (0,) + k[3:] if r["id"] in left_since else k
        return k[:1] + (push(r, today),) + k[1:]
    pool.sort(key=_lifted)
    now, rest = pool[:TOP_N + 1], pool[TOP_N + 1:] + [r for r in mine if r["id"] in pulled_today]
    # Tasks dated today that the morning pre-run never saw (added since, e.g. via + Task).
    rest += [r for r in _safe(day_list, today, {}, where="day_list today", default=[]) if r["id"] not in by_id]
    now = _swap_one(now, rest, ov["one"].get(today.isoformat()), ov["notone"].get(today.isoformat()))
    rest = [r for r in rest if not now or not now[0] or r["id"] != now[0]["id"]]
    # The Claude tab lists every task Claude can push forward, also those in ToDo;
    # the page groups today's selection on top (owner decision, Oct 31).
    claude = sorted([r for r in mine if r["claude"] or r["prep"]], key=pick)
    for r in claude:
        r["claude"] = r["claude"] + [s for s in r["prep"] if s not in r["claude"]]
    later = [r for r in rows if r["later"]]
    sel_today = ov["selected"].get(today.isoformat(), [])
    sel_tom = ov["selected"].get(tomorrow.isoformat(), [])
    for r in rows:
        r["selected"] = r["id"] in sel_today
    # Tomorrow is ranked like today (owner decision, Oct 31: "meditation should of course also inform
    # tasks for tomorrow", with the same folds below and promotable): what is scheduled or due
    # tomorrow first, then today's open tasks by the same pick, so the meditation's load and
    # today's meditation rows count. Add or One thing on one of them schedules it for tomorrow.
    def for_tom(r):
        return {**r, "selected": r["id"] in sel_tom}
    dated = [r for r in _safe(day_list, tomorrow, by_id, where="day_list tomorrow", default=[])]
    dated_ids = {r["id"] for r in dated}
    carry = [r for r in mine if r not in waiting + done and r["id"] not in dated_ids]

    def pick_tom(r):
        """Due tomorrow is hard; then heavy mental load; then what he dated for tomorrow; then
        today's meditation and the pre-run's today or soon call, as pick does for today."""
        heavy = r.get("load", 0) >= 3
        return (0 if r.get("due") and r["due"] <= tomorrow.isoformat() else 1, 0 if heavy else 1,
                0 if r["id"] in dated_ids else 1, 0 if r.get("group") == signals.meditation.MED else 1,
                0 if r.get("guess") in ("today", "soon") else 1, -r.get("load", 0)) + rank(r)
    pulled_tom = set(ov["pulled"].get(tomorrow.isoformat(), []))
    tfz = frozen_keys(pick_tom, dated + carry, "tomorrow", tomorrow)
    # A waiting task is never Suggested, Tomorrow either (Nov 10): a dated one waits under Waiting.
    dated_wait = [r for r in dated if is_waiting(r)]
    dated = [r for r in dated if not is_waiting(r)]
    tpool = [for_tom(r) for r in sorted(dated + carry, key=lambda r: tfz[r["id"]][:1] + (push(r, tomorrow),) + tfz[r["id"]][1:])
             if r["id"] not in pulled_tom]
    tpool_pulled = [for_tom(r) for r in dated + carry if r["id"] in pulled_tom]
    nxt, nrest = tpool[:TOP_N + 1], tpool[TOP_N + 1:] + tpool_pulled
    nxt = _swap_one(nxt, nrest, ov["one"].get(tomorrow.isoformat()), ov["notone"].get(tomorrow.isoformat()))
    nrest = [r for r in nrest if not nxt or not nxt[0] or r["id"] != nxt[0]["id"]]
    tom_more = nxt[TOP_N + 1:] + nrest + [for_tom(r) for r in later if r["id"] not in dated_ids]
    tom_done, tom_wait = [for_tom(r) for r in done], [for_tom(r) for r in dated_wait] + [for_tom(r) for r in waiting if r["id"] not in dated_ids]
    suggest = []

    def chosen(rows_in, ids):
        """His selection for the day, in his order of adding (Add, Oct 30)."""
        pool = {r["id"]: r for r in rows_in}
        return [pool[i] for i in ids if i in pool]

    today_pool = ([now[0]] if now and now[0] else []) + now[1:] + rest + later + done + waiting
    sel_rows_today, sel_rows_tom = chosen(today_pool, sel_today), chosen([r for r in nxt if r] + tom_more + tom_done + tom_wait, sel_tom)
    view = {
        "generated": data.get("generated", ""),
        "updated": data.get("updated", ""),
        "update_summary": [{"id": r["id"], "title": r["title"] or r["id"], "note": r["update"]}
                           for r in rows if r.get("update") and (r["title"] or r["id"])][:30] if data.get("updated") else [],
        "today": {"one": now[0] if now else None, "needs": now[1:TOP_N + 1],
                  "more": now[TOP_N + 1:] + rest + later, "done": done, "waiting": waiting,
                  "closed": _safe(tasks.closed_recently, today, where="closed_recently", default=[]),
                  "selection": sel_rows_today, "budget": capacity.budget(today, sel_rows_today)},
        "tomorrow": {"date": tomorrow.isoformat(), "one": nxt[0] if nxt else None,
                     "needs": nxt[1:TOP_N + 1], "more": tom_more, "done": tom_done, "waiting": tom_wait, "closed": [],
                     "suggest": suggest[:8],
                     "selection": sel_rows_tom, "budget": capacity.budget(tomorrow, sel_rows_tom)},
        "prerun": runs.prerun_state(),
        "replies": replies,
        "claude": claude,
        "counts": {"today": min(len(now), TOP_N + 1), "replies": len(replies), "claude": len(claude)},
        "notes": data.get("notes", [])[:3],
    }
    # Do (Nov 1): the day he executes from, which this ranking never reorders.
    view["do"] = _safe(do_view, view, ov, where="do", default=None)
    from ..adapters import flags   # Tasks v2 (Nov 6): the page draws what is switched on
    if any(flags.all_on().values()):
        view["flags"] = flags.all_on()
        # Today's booked minutes per task (Nov 11): Plan's bar counts a step booked as a part by its block.
        from . import do as _dm
        _bday = clock.do_day(dt.datetime.now(clock.TZ))
        _bm = _safe(_dm.booked_minutes, _bday,
                    lambda i: tasks.read_note(notes.task_path(i)) if notes.task_path(i) else None,
                    calendar.day_events(_bday) if _dm._day(_dm._load(), _bday)["block_steps"] else None,
                    where="booked_min", default={})
        if _bm:
            view["booked_min"] = _bm
    if flags.on("contract"):   # the steps to sharpen, in the Claude tab, never on the Do card (Nov 6)
        view["review"] = _safe(tasks.step_review, where="review", default=[])
        from . import chunkreview   # today's chunks, their outcome and estimate, steps grouped under them
        view["chunk_review"] = _safe(chunkreview.pending, view.get("do"), clock.do_day(dt.datetime.now(clock.TZ)),
                                     where="chunk_review", default=[])
    if flags.on("backfill"):   # slice 7: the step cleanups for his approval, in the Claude tab
        view["cleanup"] = _safe(tasks.cleanup_pending, where="cleanup", default=[])
    if view["do"]:
        view["counts"]["do"] = len(view["do"]["rows"])
    return view


def do_view(view: dict, ov: dict) -> dict:
    from . import do as dmod
    now = dt.datetime.now(clock.TZ)
    day = clock.do_day(now)
    by_id = {}
    for r in all_rows(view, with_do=False):
        by_id.setdefault(r["id"], r)

    def note(i):
        p = notes.task_path(i)
        return tasks.read_note(p) if p else None

    def make_row(i):
        p = notes.task_path(i)
        if not p:
            return None
        live = tasks.read_note(p)
        return {"id": i, "task": True, "title": i, "group": "Do", "prio": direction.FM_PRIO.get(live.get("priority", ""), "Z"),
                "guess": "", "unsure": False, "next": live.get("next", ""), "why": "", "meta": "", "update": "",
                "waiting": live.get("wait_line", ""), "est": "", "due": live.get("due", ""), "anx": False,
                "urgent": False, "claude": [], "prep": [], "draft": None, "later": False, "tab": "today",
                "detail": tasks._detail(live, "", "")}

    same = day == now.date()   # between midnight and 4:00 the ranking already shows the new date
    t = view["today"]
    top = (([t["one"]] if t["one"] else []) + t["needs"]) if same else []
    closed = t["closed"] if same else _safe(tasks.closed_recently, day, where="closed_recently", default=[])
    d = dmod.view(day, by_id, closed, [], top, calendar.day_events(day), note, make_row, clock._label,
                  ov["selected"].get(day.isoformat(), []),
                  {i for k, v in ov["selected"].items() if k > day.isoformat() for i in v if isinstance(i, str)})
    # A short and a long next step (Nov 3): the card's title is the short one.
    from ..tasks.read import shortsteps
    store = shortsteps._load()
    longs = []
    for r in d["rows"] + d.get("left_open", []):
        step = S.strip_marks(r.get("next") or "", due=False, ids=False)
        if step:
            r["next_short"], final = shortsteps.short(step, store)
            if not final:
                longs.append(step)
    from ..adapters import flags
    if flags.on("do_plan"):
        _do_headlines(d, store, longs)
    if state.persisting():   # the Haiku batch starts from the tick, never from a page load
        shortsteps.ensure(longs, taplog.log_event)
    # Left open in Plan (owner decision, Nov 4: "in plan this should be considered"): what an earlier day
    # left open and today's plan does not hold yet, for Add or Drop above the pre-selection.
    if same:
        e = dmod._day(dmod._load(), day)
        sel_now = set(ov["selected"].get(day.isoformat(), []))
        left = []
        for i, since in e["carried"].items():
            if i in e["ids"] or i in e["dropped"] or i in sel_now:
                continue
            st = note(i)
            if st is None or st.get("status") in ("done", "cancelled") or not dmod._open(i, day, set(by_id), note):
                continue
            if st.get("task_waiting"):
                continue   # the Waiting fold has it (one rule, steps.task_waiting; audit A1)
            r = dict(by_id.get(i) or make_row(i) or {})
            if not r:
                continue
            r["carried_from"] = clock._label(dt.date.fromisoformat(since))
            r["next_short"] = shortsteps.short(S.strip_marks(r.get("next") or "", due=False, ids=False), store)[0] \
                if r.get("next") else ""
            left.append(r)
        view["today"]["left_open"] = left
    if flags.on("actuals"):   # Tasks v2: his calibration on the Next's estimate, and the actual checks
        for r in d["rows"]:
            st = note(r["id"])
            n = _safe(tasks.est_note, st, where="est_note", default="") if st else ""
            if n:
                r["est_note"] = n
        d["actual_asks"] = _safe(tasks.actual_asks, day, where="actual_asks", default=[])
    return d




def _do_headlines(d: dict, store: dict, longs: list[str]) -> None:
    """The Do card's headline and bullets (Nov 6, the owner: "the result: what has to be achieved by
    today", then today's steps as bullets). Each plan step gets its short line; a chunk whose
    outcome he did not write himself takes the model's result phrase (shortsteps.outcome), else
    the short step, never the old "<step> done". Chunks without a phrase are queued from the tick."""
    from ..tasks.read import shortsteps
    from . import do as dmod
    want = []
    for r in d.get("rows", []) + d.get("part_done", []):
        c = r.get("chunk") or {}
        plan = c.get("plan") or r.get("plan") or []
        for x in plan:
            body = S.strip_marks(x.get("text") or "", ids=False)
            sh, final = shortsteps.short(body, store)
            x["short"] = sh
            if not final:
                longs.append(body)
        steps = [x.get("text") or "" for x in plan]
        if not steps:
            continue
        # A part-done row carries no outcome_his: its outcome is his when it is not the suggestion.
        his = c.get("outcome_his") if c else r.get("outcome") != dmod.suggest_outcome(steps)
        if his:
            continue
        got = shortsteps.outcome(r.get("title") or "", steps, store)
        if not got:
            want.append((r.get("title") or "", steps))
            sug = dmod.suggest_outcome(steps)
            got = sug if sug.startswith("Know ") else (plan[-1].get("short") or sug)
        if c and c.get("outcome") is not None:
            c["outcome"] = got
        if "outcome" in r:
            r["outcome"] = got
    if state.persisting():
        shortsteps.ensure_outcomes(want, taplog.log_event)


def _swap_one(now: list[dict], rest: list[dict], one_id: str | None,
              not_one: list[str] | None = None) -> list[dict]:
    """He made another task the one thing: put it first (rows from rest are pulled in).
    Tasks he said are not the one thing stay in the list, just not first."""
    if one_id:
        pick = next((r for r in now + rest if r["id"] == one_id), None)
        if pick:
            return [pick] + [r for r in now if r["id"] != one_id]
    if not_one and now:
        first = next((i for i, r in enumerate(now) if r["id"] not in not_one), None)
        if first is None:
            return [None] + now
        if first:
            return [now[first]] + now[:first] + now[first + 1:]
    return now


OVERRIDES = config.STATE_DIR / "checkin_app" / "overrides.json"

PICK_FROZEN = config.STATE_DIR / "checkin_app" / "pick_frozen.json"


# D-26
def frozen_keys(key_fn, rows, tag: str, day: dt.date) -> dict:
    """Each row's rank key as first seen that day, so the list holds still (owner decision, Nov 1:
    a task jumped up the list just because he marked it as added; Add schedules
    the task, which changed its key). A task new to the day slots in by its own key."""
    d = state.read(PICK_FROZEN, {})
    name = f"{tag}:{day.isoformat()}"
    m = d.get(name) if isinstance(d.get(name), dict) else {}
    out, changed = {}, False
    for r in rows:
        k = m.get(r["id"])
        if not isinstance(k, list):
            k, changed = list(key_fn(r)), True
            m[r["id"]] = k
        out[r["id"]] = tuple(k)
    if changed and state.persisting():   # frozen by taps and the tick, never by a page load
        old = (day - dt.timedelta(days=3)).isoformat()

        def f(cur):
            cur = {n: v for n, v in cur.items() if n.rsplit(":", 1)[-1] >= old}
            have = cur.get(name) if isinstance(cur.get(name), dict) else {}
            for k, v in m.items():   # first seen wins, also against a writer in between
                have.setdefault(k, v)
            cur[name] = have
            return cur
        try:
            state.update(PICK_FROZEN, f, {}, indent=None)
        except OSError:
            pass
    return out


def load_overrides() -> dict:
    try:
        ov = json.loads(OVERRIDES.read_text())
    except (OSError, ValueError):
        ov = {}
    ov = ov if isinstance(ov, dict) else {}
    # one: a task name per day; the rest: a list of names per day. Anything else is dropped,
    # so a hand-edited or half-written file cannot take the view down (Nov 1).
    out = {}
    for kind in ("one", "hidden", "selected", "notone", "pulled", "reply_skip", "note", "note_sent"):
        days = ov.get(kind) if isinstance(ov.get(kind), dict) else {}
        if kind in ("one", "note", "note_sent"):
            out[kind] = {d: v for d, v in days.items() if isinstance(v, str)}
        else:
            out[kind] = {d: [i for i in v if isinstance(i, str)] for d, v in days.items() if isinstance(v, list)}
    return out


def plan_note(day: dt.date, ov: dict | None = None) -> dict:
    """His note for Claude for the day (r8 F2): kept here with the day's plan, so phone and laptop
    share it, and pruned with the day. `sent` is the note as the day's plan chat last got it."""
    ov = ov if ov is not None else load_overrides()
    return {"text": ov["note"].get(day.isoformat(), ""), "sent": ov["note_sent"].get(day.isoformat(), "")}


def _norm_overrides(ov) -> dict:
    ov = ov if isinstance(ov, dict) else {}
    out = {}
    for kind in ("one", "hidden", "selected", "notone", "pulled", "reply_skip", "note", "note_sent"):
        days = ov.get(kind) if isinstance(ov.get(kind), dict) else {}
        if kind in ("one", "note", "note_sent"):
            out[kind] = {d: v for d, v in days.items() if isinstance(v, str)}
        else:
            out[kind] = {d: [i for i in v if isinstance(i, str)] for d, v in days.items() if isinstance(v, list)}
    return out


def day_list(day: dt.date, pre_rows: dict) -> list[dict]:
    """The Tomorrow side of the ToDo switch (owner decision, Oct 30): every open task
    scheduled or due that day, read live from Inbox/, so it works before the
    next pre-run. The pre-run's row (why, drafts, estimate) is reused where it has one."""
    from tools.todos import load_tasks
    iso, out = day.isoformat(), []
    for t in load_tasks():
        # Due on or before the day counts too (Nov 10, Tier 1.4: "Sell or cancel Iceland trek package",
        # due that day and scheduled later, was on no list), whatever its scheduled date.
        due = tasks.effective_due(t)   # its own due or an open step's 📅 (audit A9)
        if t["status"] in ("done", "cancelled") or not (iso == t["scheduled"][:10] or (due and due <= iso)):
            continue
        live = tasks.read_note(Path(t["path"]))
        base = dict(pre_rows.get(t["name"]) or {
            "id": t["name"], "task": True, "title": t["name"], "group": "Scheduled",
            "guess": "", "unsure": False, "why": "", "meta": "", "anx": False,
            "claude": [], "draft": None})
        base.update({
            "prio": direction.FM_PRIO.get(t["priority"], base.get("prio", "Z")),
            "urgent": t["urgent"] or base.get("urgent", False),
            "next": live["next"] or base.get("next", ""),
            "waiting": live["wait_line"] or base.get("waiting", ""),
            "est": base.get("est") or (f"{t['timeEstimate']}m" if t["timeEstimate"] else ""),
            "due": base.get("due") or due,
            "meta": base.get("meta") or ("Due " + clock._label(day) if due == iso else
                                         "Overdue since " + clock._label(dt.date.fromisoformat(due)) if due and due < iso
                                         else "Scheduled " + clock._label(day)),
            "later": False, "tab": "today",
            "detail": tasks._detail(live, (base.get("detail") or {}).get("task_est", ""), base.get("est", ""))})
        out.append(base)
    return sorted(out, key=lambda r: (0 if r["urgent"] else 1, PRIO.get(r["prio"], 3), r["title"].lower()))


def _safe(fn, *args, where: str, default):
    """A side list of the view (Done fold, tasks dated that day) that fails gives way to
    its default; the rest of the view still loads (Nov 1)."""
    try:
        return fn(*args)
    except Exception as exc:
        taplog.log_event("error", where=where, detail=repr(exc)[:200])
        return default


def attach_shorts(v: dict) -> None:
    """v["shorts"]: {step as the page shows it: its one-line summary} for every step of the
    Plan cards (Nov 3, the owner: "a quick overview in Today with one line / one sentence summary
    of each step"). Same store and background model as Do's titles (shortsteps.py)."""
    from ..tasks.read import shortsteps
    store, out, longs = shortsteps._load(), {}, []
    for r in all_rows(v, with_do=False):
        for s in [r.get("next") or ""] + list((r.get("detail") or {}).get("steps") or []):
            k = S.HIS_TAG.sub("", s or "").strip()   # the key page.html's clean() looks up (audit A3)
            if not k or k in out:
                continue
            body = S.strip_marks(k, ids=False)
            short, final = shortsteps.short(body, store)
            if short != body:
                out[k] = short
            if not final:
                longs.append(body)
    if state.persisting():
        shortsteps.ensure(longs, taplog.log_event)
    v["shorts"] = out


def all_rows(view: dict, with_do: bool = True) -> list[dict]:
    rows = []
    for side in ("today", "tomorrow"):
        t = view[side]
        rows += ([t["one"]] if t["one"] else []) + t["needs"] + t["more"] + t["done"] + \
            t["waiting"] + t.get("suggest", []) + t.get("closed", [])
    return rows + view["replies"] + view["claude"] + (((view.get("do") or {}).get("rows") or []) if with_do else [])




def _key_txt(t: str) -> str:
    """A step's words for matching a chunk's stored text: no estimate, date marker or id."""
    t = S.strip_marks(t or "", optional=True)
    return re.sub(r"\W+", " ", t.lower()).strip()


def _with_chunk_est(day: dt.date, rows: list[dict]) -> list[dict]:
    """Each row's estimate as the page's budget bar counts it (Nov 6, refactor Phase 4): the
    steps of today's committed chunk, each its own (N min), the first falling back to the card's
    estimate; a row without a chunk keeps its estimate. Claude's plan and the bar then quote the
    same planned minutes."""
    try:
        from . import do as dmod
        d = dmod._load()
        chunks = {**dmod._day(d, clock.do_day(dt.datetime.now(clock.TZ)))["chunks"], **dmod._day(d, day)["chunks"]}
    except Exception:
        return rows
    out = []
    for r in rows:
        steps = chunks.get(r.get("id"))
        if not steps:
            out.append(r)
            continue
        tot = 0
        from ..adapters import flags
        so = flags.on("step_order")
        pmin = {}   # a long step's part (Nov 11, M1): its words -> the part's minutes
        if so:
            pth = notes.task_path(r.get("id") or "")
            st = tasks.read_note(pth) if pth else {}
            parts = tasks.order_now(st).get("parts") or {}
            pmin = {_key_txt(x["text"]): parts[x["id"]]
                    for x in st.get("step_states") or [] if x.get("id") in parts}
        for k, s in enumerate(steps):
            if so and re.search(r"\s\(optional\)", str(s)):
                continue   # optional steps are filler (Nov 11): never planned minutes
            if so and _key_txt(str(s)) in pmin:
                tot += pmin[_key_txt(str(s))]
                continue
            m = S.est_min(str(s))
            v = m if m is not None else (tasks._minutes(r.get("est", "")) if k == 0 else None)
            if v is None:
                tot = None
                break
            tot += v
        out.append({**r, "est": f"{tot}m" if tot is not None else ""})
    return out


def _fit_lines(day: dt.date, rows: list[dict]) -> list[str]:
    """The day's time budget for the plan, and when the selection is over it, the decisions it
    needs (owner decision, Nov 2: "if i go over budget (which is ok) then the plan step needs to take
    decisions / see how things fit ideally or would need to change to make it work")."""
    try:
        b = capacity.budget(day, _with_chunk_est(day, rows))
    except Exception:
        return []
    if b["free_min"] is None:
        return []
    by = {}
    for x in b["slots"]:
        m = max(0, min(x["f"], b["day_end"]) - max(x["s"], b.get("start", b["day_start"])))
        if m:
            by[x["title"]] = by.get(x["title"], 0) + m
    room = b["free_min"] + sum(by.values())
    # The app's duration format (UI r3): "3h 40", "2h", "45 min".
    h = lambda m: (f"{m // 60}h" + (f" {m % 60:02d}" if m % 60 else "")) if m >= 60 else f"{m} min"
    out = [f"Time ({b['window']}): {h(b['planned_min'])} planned"
           + (f", {b['unestimated']} without an estimate" if b["unestimated"] else "")
           + f"; {h(room)} available" + (", of it " + ", ".join(f"{h(m)} {t}" for t, m in by.items()) if by else "")
          
           + ". Use this available time as the room, the same number his budget bar shows (Nov 10): do not"
           " take off buffers of your own; a buffer you add comes out of it and you say how much."
           " Fixed time (accepted events, the Week Structure routines on Focus) is already taken out;"
           " slots are where tasks of that kind go: big-rock and Best Year work into Big rocks and focus"
           " blocks, admin into Admin backup."]
    if b["planned_min"] > room:
        out.append(f"That is {h(b['planned_min'] - room)} over, which is fine, but the plan has to decide how it "
                   "works: first fit what is most important (due dates, the one thing, big rocks) into the "
                   "best slots, then say plainly what has to give, as options for me to pick, your "
                   "recommendation first: move a task to another day (name the day with room), shorten it to "
                   "a first step, put it in a flexible Week Structure block (run, commute, admin backup), or "
                   "skip an event I have not accepted. Never move a fixed commitment without asking.")
    return out


_FENCE = re.compile(r"```blocks\s*\n([\s\S]*?)(```|$)")

# A fence line (Nov 10, Tier 1.1): `YYYY-MM-DD HH:MM-HH:MM Title`. The date was added because
# Monday's chat proposed a Wednesday block that Plan drew, and Do offered to book, as Monday's.
_FLINE = re.compile(r"^\s*(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\s*(.*)")
# The steps a block covers (Nov 11, the M1 case: Book left Do on a chunk the chat no longer
# planned): `... M1 gear order + NorCal waiver [sce28 s5f2e]` at the end of the line.
_FSIDS = re.compile(r"\s*\[((?:\^?s[0-9a-f]{4,8}[\s,]*)+)\]\s*$")


def fence_blocks(text: str, day: dt.date) -> list[dict]:
    """The blocks in a message's ```blocks fence, each with its date: {"date", "s", "f", "title",
    "dated"}. An undated line (chats from before Nov 10) is drawn on the chat's own day but never
    booked (r3b: Monday's undated "(due Wed)" block was offered as Monday's Book)."""
    fence = _FENCE.search(str(text or ""))
    out = []
    for line in (fence.group(1).split("\n") if fence else []):
        m = _FLINE.match(line.replace("**", "").replace("`", ""))
        if not m:
            continue
        b_s, b_f = int(m[2]) * 60 + int(m[3]), int(m[4]) * 60 + int(m[5])
        if b_f <= b_s:
            continue
        try:
            d = dt.date.fromisoformat(m[1]) if m[1] else day
        except ValueError:
            d = day
        title, sids = m[6].strip(), []
        si = _FSIDS.search(title)
        if si:
            sids = list(dict.fromkeys(x.lstrip("^") for x in re.split(r"[\s,]+", si.group(1)) if x))
            title = title[:si.start()].strip()
        out.append({"date": d.isoformat(), "s": b_s, "f": b_f, "title": title, "dated": bool(m[1]),
                    **({"sids": sids} if sids else {})})
    return out


def last_proposal(conv: dict, day: dt.date) -> list[dict]:
    """The blocks of the chat's latest proposal: stored when a reply carries a fence (copy r3), else
    the latest message with one. A reply without one (a question, a booking report) keeps it."""
    pl = conv.get("plan")
    if isinstance(pl, dict) and isinstance(pl.get("blocks"), list):
        last = next((m for m in reversed(conv.get("messages") or []) if m.get("role") == "claude"
                     and _FENCE.search(str(m.get("text", "")))), None)
        if not last or str(last.get("at", "")) <= str(pl.get("at", "")):
            return [b for b in pl["blocks"] if isinstance(b, dict) and {"date", "s", "f"} <= set(b)]
    for m in reversed(conv.get("messages") or []):
        if m.get("role") == "claude" and _FENCE.search(str(m.get("text", ""))):
            return fence_blocks(m.get("text", ""), day)
    return []


def event_for(b: dict, evs: list[dict] | None) -> dict | None:
    """The calendar event a booked block became (the in_calendar rule), or None."""
    return next((e for e in evs or [] if not e.get("allday") and e.get("s") == b["s"] and e.get("f") == b["f"]
                 and (e.get("task_ids") or e.get("tasks"))), None)


def in_calendar(b: dict, evs: list[dict]) -> bool:
    """A proposed block is booked only when an event with its exact times names a task with a
    `Task:` line (Nov 10, r3b: a block titled "Admin backup: Expedia check" matched the Admin backup
    slot on a shared word and read as booked). Never on words; the page's lane uses the same rule."""
    return any(e.get("s") == b["s"] and e.get("f") == b["f"] and (e.get("task_ids") or e.get("tasks"))
               for e in evs if not e.get("allday"))


def day_over(day: dt.date, now: dt.datetime | None = None) -> bool:
    """The day's planning window has passed (the budget's start is its end): nothing to book."""
    now = now or dt.datetime.now(clock.TZ)
    return day < now.date() or (day == now.date() and now.hour * 60 + now.minute >= capacity.DAY_END)


def day_blocks(day: dt.date, conv: dict | None = None, fresh: bool = False) -> dict:
    """The day's own proposal, split: {"open": not in the calendar yet, "booked": in it,
    "other": blocks dated for another day (shown with their day, never booked from here),
    "ok": whether the calendar could be read}."""
    from ..chat import load as _load_chat
    conv = conv if conv is not None else _load_chat("plan:" + day.isoformat())
    blocks = last_proposal(conv, day)
    own = [b for b in blocks if b["date"] == day.isoformat() and b.get("dated", True)]
    evs = calendar.day_events(day, fresh=fresh) if own else []
    out = {"open": [], "booked": [], "other": [b for b in blocks if b["date"] != day.isoformat()],
           "undated": [b for b in blocks if not b.get("dated", True)], "ok": evs is not None}
    # Booked only when the last Book says so and the calendar agrees (PM r3: a tick stayed under
    # "Not booked"). A chat with no Book on record (before Nov 10) goes by the calendar alone.
    bk = (conv.get("book") or {}).get("status")
    trust = bk is None or bk in ("booked", "changed", "partial")
    gone = _moved_away(day, conv)
    for b in own:
        if trust and evs and in_calendar(b, evs):
            out["booked"].append(b)
        elif gone and gone(b):
            out.setdefault("moved", []).append(b)   # its task now belongs to another day (r4 C)
        else:
            out["open"].append(b)
    return out


_STOP = {"with", "from", "into", "this", "that", "your", "part", "batch", "block", "prep", "plan", "work", "review",
         "admin", "check"}


def _title_words(t) -> set:
    return {w for w in re.sub(r"[^a-z0-9à-ÿ ]+", " ", str(t).lower()).split() if len(w) >= 4 and w not in _STOP}


def _moved_away(day: dt.date, conv: dict):
    """A test for blocks whose tasks all left the day (r4 C: a task picked for Tomorrow kept its
    block in today's count): the tasks the plan was made for, read from its request, matched to a
    block by a shared title word, now dated another day and not committed to it. None when no
    task of the plan has left."""
    names = []
    for m in conv.get("messages") or []:
        if m.get("role") == "you" and str(m.get("text", "")).startswith("🗓 Plan "):
            names += [re.sub(r"\s\[t-[0-9a-f]+\].*$|\s\(.*$|:.*$", "", ln.split(". ", 1)[1]).strip()
                      for ln in m["text"].split("\n") if re.match(r"^\d+\.\s", ln)]
    if not names:
        return None
    try:
        from . import do as dmod
        ids = set(dmod._day(dmod._load(), day)["ids"])
    except Exception:
        ids = set()
    left = set()
    for n in dict.fromkeys(names):
        p = notes.task_path(n)
        sched = tasks.read_note(p).get("scheduled", "") if p else ""
        if n not in ids and sched and sched[:10] != day.isoformat():
            left.add(n)
    if not left:
        return None
    words = {n: _title_words(n) for n in dict.fromkeys(names)}

    def gone(b):
        bw = _title_words(b.get("title", ""))
        hit = [n for n, w in words.items() if w & bw]
        return bool(hit) and all(n in left for n in hit)
    return gone


def plan_pending(day: dt.date, now: dt.datetime | None = None) -> dict | None:
    """Blocks the day's Plan chat proposed for that day that are not in the calendar yet (Nov 4,
    the owner: "i didn't see that the plan is unconfirmed yet"): {"n", "min", "busy", "book"}, or None.
    Only the day's own blocks count (Nov 10): another day's never shows as this day's Book."""
    from ..chat import load as _load_chat
    if day_over(day, now):   # r3b: a day that is over books nothing, so Do offers nothing
        return None
    conv = _load_chat("plan:" + day.isoformat())
    left = day_blocks(day, conv)["open"]
    return {"n": len(left), "min": sum(b["f"] - b["s"] for b in left), "busy": bool(conv.get("busy")),
            "book": conv.get("book")} if left else None


def book_text(day: dt.date, blocks: list[dict]) -> str:
    """What Book sends Claude (Nov 10, Tier 1.3): the app's [Book] marker, then exactly the blocks."""
    lines = [f"[Book] the owner tapped Book for {day.strftime('%A')}, {clock._label(day)} ({day.isoformat()}). "
             "Create exactly these calendar blocks on that date, each with its `Task:` line(s), and nothing else:"]
    lines += [f"- {day.isoformat()} {b['s'] // 60:02d}:{b['s'] % 60:02d}-{b['f'] // 60:02d}:{b['f'] % 60:02d} {b['title']}" for b in blocks]
    lines.append("Do not move, reschedule, close or edit any task, and do not change any other event. If something "
                 "else looks needed, say so after booking as a proposal for him to decide. Then say in one line "
                 "what you created.")
    return "\n".join(lines)


_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def book_result(day: dt.date, blocks: list[dict], tools: list[tuple[str, dict]]) -> dict:
    """What a Book turn did, read from the calendar, not from Claude's words (Nov 10, Tier 1.2):
    {"status": booked | partial | failed | changed | unknown, "n", "booked", "changes", "at"}.
    changed: every block is in, and the turn also changed something else (listed)."""
    evs = calendar.day_events(day, fresh=True)
    got = [b for b in blocks if evs and in_calendar(b, evs)]
    # Booking is the final word on today's chunks (Nov 11, M1): the booked blocks' steps become
    # each task's part on Do (planning/actions.apply_booked, called by the chat after this).
    changes = []
    for name, data in tools:
        op = name.rsplit("__", 1)[-1]
        if name in _WRITE_TOOLS:
            changes.append("Edited " + Path(str((data or {}).get("file_path") or (data or {}).get("notebook_path") or "a file")).stem)
        elif name.startswith("mcp__") and op in ("update_event", "delete_event", "respond_to_event"):
            changes.append({"update_event": "Changed an event", "delete_event": "Deleted an event",
                            "respond_to_event": "Answered an invite"}[op])
    status = ("unknown" if evs is None else "failed" if not got else "partial" if len(got) < len(blocks)
              else "changed" if changes else "booked")
    return {"status": status, "n": len(blocks), "booked": len(got), "changes": list(dict.fromkeys(changes))[:6],
            "got": [i for i, b in enumerate(blocks) if b in got],
            # Each booked block's event key (Nov 11): Do follows the event if he moves it.
            "keys": {str(i): (event_for(b, evs) or {}).get("key", "") for i, b in enumerate(blocks) if b in got},
            "day": day.isoformat(), "at": dt.datetime.now(clock.TZ).isoformat(timespec="seconds")}


def plan_text(day: dt.date, rows: list[dict], ending: bool = True, note: str = "") -> str:
    tok = _with_room(day)   # the day's own free time sizes each suggested piece (Nov 11)
    try:
        return _plan_text(day, rows, ending, note)
    finally:
        if tok is not None:
            tasks.ROOM.reset(tok)


def _plan_text(day: dt.date, rows: list[dict], ending: bool = True, note: str = "") -> str:
    lines = [f"🗓 Plan {day.strftime('%A')}, {clock._label(day)}"]
    # His note for the day (Nov 10): first, so the first proposal already takes it in.
    if (note or "").strip():
        lines.append("the owner adds: " + " ".join(str(note).split())[:1000])
    lines += [
             "Put these into the calendar with the schedule-work rules (Week Structure, "
             "working conditions, buffers). Propose the blocks first; the owner books them with the app's Book "
             "button (Nov 10): his chat messages are changes to the proposal, never an OK to book:"]
    def tid(r):   # the task's ID, so the block it gets names it (Do orders by blocks, by key only)
        p = notes.task_path(r.get("id") or "")
        t = tasks.read_note(p).get("task_id", "") if p else ""
        return f" [{t}]" if t else ""
    # Each task with the part he committed and its minutes (Nov 10, Tier 1.5: the card estimate
    # said 20m for a 1h part), the same number as the budget line below.
    parts = _parts(day)
    est = {r["id"]: r.get("est", "") for r in _with_chunk_est(day, rows)}
    def dur(e):   # the app's duration format, as the page shows it ("1h 05", "45 min")
        n = tasks._minutes(e or "")
        return e if n is None else (f"{n // 60}h" + (f" {n % 60:02d}" if n % 60 else "")) if n >= 60 else f"{n} min"

    def line(i, r):
        m = dur(est.get(r["id"]) or r.get("est"))
        part = [re.sub(r"\s*\^s[0-9a-z]+\s*$", "", str(x)).strip() for x in parts.get(r.get("id")) or []]
        return (f"{i}. {r['title']}{tid(r)}" + (f" ({m})" if m else "")
                + (f": part for {day.strftime('%A')}, " + "; ".join(part) if part else f": next, {r['next']}" if r.get("next") else ""))
    def blocked(r):   # the steps a wait holds back (Nov 10): named, so Claude never gives them a block
        p = notes.task_path(r.get("id") or "")
        st = tasks.read_note(p) if p else {}
        by_id = {x["id"]: x["text"] for x in st.get("step_states") or [] if x.get("id")}
        out = []
        for w in st.get("waits") or []:
            held = [S.strip_marks(by_id.get(b, ""), ids=False) for b in w.get("blocks") or []]
            held = [h for h in held if h]
            if held:
                out.append(f"   Blocked until {w.get('who') or 'the reply'} replies: " + "; ".join(held))
        return out
    from ..adapters import flags
    so = flags.on("step_order")

    def order(r):   # the computed order and why (flag step_order, Nov 11), so Claude plans the same piece
        p = notes.task_path(r.get("id") or "")
        st = tasks.read_note(p) if p else {}
        o = tasks.order_now(st)
        if not o.get("plan"):
            return []
        by_id = {x["id"]: x["text"] for x in st.get("step_states") or [] if x.get("id")}
        def w(i):
            t = S.strip_marks(by_id.get(i, ""), ids=False, optional=True)
            y = (o.get("why") or {}).get(i, "")
            pm = (o.get("parts") or {}).get(i)
            return t + (f", {pm} min of it today (too long for one sitting)" if pm else "") + (f" · {y}" if y else "")
        main = [i for i in o["plan"] if i not in (o.get("optional") or [])]
        out = ["   Order (computed from dates, who waits and length; the note's order does not count): "
               + "; ".join(f"{k}. {w(i)}" for k, i in enumerate(main[:6], 1))]
        if o.get("piece"):   # today's piece, each step with why it is in (Nov 11: the budget is a ceiling)
            out.append("   Today's piece (only steps with a reason; nothing fills the hour, Keep going does more): "
                       + "; ".join(w(i) for i in o["piece"]))
        nd = o.get("need") or {}
        if nd.get("min"):   # the deadline pressure, so the plan can give a freer day more of it (Nov 11)
            dd = dt.date.fromisoformat(nd["by"])
            out.append(f"   Needs about {dur(str(nd['min']) + 'm')} a day until {dd:%a} {dd:%b} {dd.day}; "
                       f"today's suggested part is {dur(str(o.get('budget') or 60) + 'm')}, sized by the free time. "
                       "If there is more room, propose more parts of it; each long step stays a 60 min part unless he says otherwise.")
        if o.get("optional"):
            out.append("   Optional, only if time is left (not in the minutes above): " + "; ".join(w(i) for i in o["optional"]))
        for i, g in (o.get("held") or {}).items():
            out.append(f"   Not before its date ({g}): {w(i)}")
        return out
    def step_ids(r):   # the steps a block may cover, by id (Nov 11): the fence names them, Book commits them
        p = notes.task_path(r.get("id") or "")
        st = tasks.read_note(p) if p else {}
        blk = set(st.get("blocked") or [])
        free = [x for x in st.get("step_states") or [] if x.get("id") and x["state"] == "open"
                and x.get("kind") != "Waiting" and x["id"] not in blk]
        if not free:
            return []
        return ["   Steps by id: " + "; ".join(f"[{x['id']}] " + S.DUE_OLD.sub("", x["text"]) for x in free[:8])]
    for i, r in enumerate(rows, 1):
        lines.append(line(i, r))
        lines += blocked(r)
        lines += step_ids(r)
        if so:
            lines += order(r)
    lines += _fit_lines(day, rows)
    if ending:   # Telegram only: in the app's chat the tap itself is the go (Oct 31)
        lines.append("Reply \"go\" to have me plan it.")
    else:   # the app draws the proposal on his calendar above the chat (Oct 31)
        lines.append(f"This chat plans {day.strftime('%A')} {day.isoformat()} only. Each time you propose or change "
                     "blocks, end the message with a ```blocks fence, one line per new block you would create (not "
                     f"what is already in the calendar): YYYY-MM-DD HH:MM-HH:MM Title, dated {day.isoformat()}, the "
                     "title a short name he recognises at a glance (\"Batch: Delta code, CS 351Z quiz, haircut\"), "
                     "then in brackets the ids of the steps the block covers, from \"Steps by id\" above "
                     "(\"13:00-13:45 M1 gear order + NorCal waiver [sce28 s5f2e]\"). Booking makes exactly "
                     "those steps his part for the day on Do, so a block with no ids changes nothing there. "
                     "Something that belongs on another day goes in the text with its day, never in the fence. "
                     "The app draws the fence on his day and hides it.")
        lines.append("When you create the blocks, give each one a `Task: <obsidian link> (t-xxxxxx)` line per "
                     "task it holds, with the ID in brackets above (a batch has several). Do orders its cards "
                     "by these lines only; a block without them belongs to no task.")
    # Nov 5: FNV prep, scheduled today, was missing from the list and nobody noticed.
    lines.append("Before proposing, check this list against what is scheduled or due that day in the vault and "
                 "in the pre-run notes above. If such a task is missing here, name it in one line and ask whether to add it.")
    return "\n".join(lines)


def _parts(day: dt.date) -> dict:
    """The committed part per task for the day (do.json chunks), {} when it cannot be read."""
    try:
        from . import do as dmod
        d = dmod._load()
        return {**dmod._day(d, clock.do_day(dt.datetime.now(clock.TZ)))["chunks"], **dmod._day(d, day)["chunks"]}
    except Exception:
        return {}
