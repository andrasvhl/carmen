"""Tasks, what other modules read: a task note parsed (status, dates, steps with their ids and states,
the DoD, a step's workspace links, what was done today), estimates in minutes, what he finished
today and the Done fold. Tasks is the only writer of task notes inside Carmen (actions.py); outside
edits (Obsidian, chats, skills) are read as they are.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from runner import config

from . import cardnotes, shortsteps   # re-exported: Planning reads card notes and step summaries through Tasks
from ..adapters import clock
from ..adapters import state
from ..direction import read as direction
from .estimates import _minutes   # re-exported: estimates are Tasks' (Planning counts them)
from tools.todos import effective_due, is_later  # noqa: F401 - re-exported: one due and one "later" rule (audit A9)


def _stepmod():
    from . import steps
    return steps


_NOTE_CACHE: dict = {}   # path -> ((mtime_ns, size, day), parsed note)

_WORK_DONE = re.compile(r"\b(?:booked|sent|submitted|paid|completed|confirmed|done|ticked|replied|uploaded|filled|"
                        r"messaged|texted|ordered|bought|returned|cancelled|signed)\b", re.I)

# Edits are never work (Nov 4: "DoD set: ... booked ... confirmed" counted as done work, and
# with the Next step reworded by a chat the Nepal task left Do as finished for the day).
_NOT_WORK = re.compile(r"prepared by Claude|DoD proposed|DoD drafted|DoD set|DoD confirmed|Next set to|"
                       r"steps? (?:edited|discarded|rewritten|reworded)|rewritten by|"
                       r"your meditation|scheduled for|not waiting|"
                       r"(?:^|·\s)waiting on|not (?:yet )?(?:sent|booked|paid|submitted|done)", re.I)



def _step_due(body: str) -> tuple[str, str]:
    """The earliest deadline on an open step (📅, or an old "(due Mon D)"), as (YYYY-MM-DD, the
    step's display text). Waiting lines are not his steps."""
    from tools import steps as S
    best = ("", "")
    for st in S.steps_of(body):
        if st.state == "open" and st.kind != "Waiting" and st.due and (not best[0] or st.due < best[0]):
            best = (st.due, st.display())
    return best


# D-35
def _backsched(steps, since_of: dict, tdue: str) -> dict:
    """The backward pass (tools/backsched) over a note's steps, with the flag on: {"state",
    "start_by", "why", "chase", "nodes"}, else {}. Read only; never raises."""
    from tools import backsched as B
    if not B.enabled():
        return {}
    try:
        today = clock.carmen_day()
        return B.compute(B.from_steps(steps, today, since_of), today, hard=[tdue] if tdue else [])
    except Exception:   # noqa: BLE001 - the plan goes on without it
        return {}


def _done_today(body: str) -> int:
    pref = f"- {clock._label(clock.carmen_day())} · "
    return sum(1 for l in body.splitlines()
               if l.startswith(pref) and _WORK_DONE.search(l) and not _NOT_WORK.search(l))


OPEN_STATUSES = ("inbox", "open", "in-progress")


def all_open(q: str = "", limit: int = 300) -> list[dict]:
    """Every open task in Inbox/ (QA Nov 8 item 13: "Sell or cancel Iceland trek package", due Nov 10,
    was on no screen because it was scheduled after its due date). Search matches the title, the
    Next step and the later steps. Sorted by due date, then scheduled, then title; nothing re-ranked."""
    words = [w for w in (q or "").lower().split() if w]
    out = []
    for p in sorted(config.INBOX.glob("*.md")):
        try:   # a broken note (bad bytes, no frontmatter) is skipped, never a 500
            head = p.read_text(encoding="utf-8").partition("\n---\n")[0]
            if not re.search(r"^type:\s*action\s*$", head, re.M):
                continue
            st = read_note(p)
        except (OSError, ValueError):
            continue
        if st.get("status") not in OPEN_STATUSES:
            continue
        hay = " ".join([p.stem, st.get("next", ""), *st.get("steps", [])]).lower()
        if words and not all(w in hay for w in words):
            continue
        out.append({"id": p.stem, "title": p.stem, "next": st.get("next", ""), "due": st.get("due", ""),
                    "scheduled": st.get("scheduled", ""), "priority": st.get("priority", ""),
                    "waiting": bool(st.get("task_waiting")), "status": st.get("status", ""),
                    # For Plan's All tasks (Nov 10): who it waits on, and the whole step list.
                    "wait_who": (st.get("waits") or [{}])[0].get("who", "") if st.get("waits") else "",
                    "wait_since": st.get("wait_since", ""), "detail": _detail(st)})
    out.sort(key=lambda r: (r["due"] or "9999", r["scheduled"] or "9999", r["title"].lower()))
    return out[:limit]


def reply_already_sent(stem: str) -> bool:
    """The double-send guard for Replies (Nov 9): his current step already has a message out."""
    from ..adapters.notes import task_path
    from . import messages
    p = task_path(stem)
    if not p:
        return False
    try:
        return messages.sent_on_next(p.read_text(encoding="utf-8"))
    except OSError:
        return False


def read_note(p: Path) -> dict:
    """read_note, kept per file until the file changes (or the day turns, for wait_since);
    a deep copy each time, so no caller edits the cache (Nov 3, speed)."""
    import copy
    try:
        st = p.stat()
        # The flags are in the key: what a note shows depends on them (Tasks v2), and a flag
        # switched on must show without a restart or an edit to every note (Nov 6).
        from ..adapters import flags
        sig = (st.st_mtime_ns, st.st_size, clock.carmen_day(), tuple(flags.all_on().values()))
    except OSError:
        return _read_note(p)
    hit = _NOTE_CACHE.get(str(p))
    if hit and hit[0] == sig:
        return copy.deepcopy(hit[1])
    d = _read_note(p)
    _NOTE_CACHE[str(p)] = (sig, d)
    if state.persisting():   # a changed note's outside edits go into the step history: taps and the tick only
        try:
            from tools import steps as S
            S.observe(p)
        except Exception:   # the history is a layer on top
            pass
    return copy.deepcopy(d)


def _read_note(p: Path) -> dict:
    """The live state of a task note: what the app shows over the pre-run's copy,
    so a change made anywhere (a tap, Claude in chat, Obsidian) shows at once."""
    text = p.read_text(encoding="utf-8", errors="replace")
    head, _, body = text.partition("\n---\n")
    from tools import steps as S
    # Reading never writes (Nov 5: writing missing step ids on read changed notes in test mode and
    # on every open). Ids come from every writer instead: the task hook after any Claude edit,
    # task creation, every chat turn, Carmen's own writes and the 08:00 sweep.

    def fm(key):
        m = re.search(rf"^{key}:\s*(.*)$", head, re.M)
        return m.group(1).strip().strip('"') if m else ""

    _all = S.steps_of(body)
    found = _found(head, p.stem)
    _later = _steps_with_ids(body)
    nxt = next((x for x in _all if x.state == "open" and x.kind == "Next"), None)
    wait = next((x for x in _all if x.state == "open" and x.kind == "Waiting"), None)
    # A step's own deadline (Nov 3, "(due Nov 13)" on its line): the task counts as due by its
    # earliest open one when that comes before the task's own due date.
    sdue, stext = _step_due(body)
    tdue = fm("due")[:10]
    # Waits per step (Nov 10): which open steps wait on what, and the one rule for a waiting task.
    blk = S.blocked_ids(_all)
    waits = [{"id": x.id, "text": x.display(), "who": re.split(r"\s*(?: · for: |,|\(| to reply)", x.text)[0].strip(),
              "blocks": [y.id for y in _all if y.state == "open" and x.id and x.id in y.after],
              **({"pending": True} if x.id in blk else {})}   # after an open step of his: not started (D-35)
             for x in _all if x.state == "open" and x.kind == "Waiting"]
    for w in waits:
        w["since"] = _wait_since(body, w["who"], next(x for x in _all if x.state == "open" and x.kind == "Waiting" and (x.id == w["id"] or not w["id"])), _all)
    # A step others wait on counts as a wait only when it waits on someone (HR: "Check the portal for
    # Lakeside's reply" holds the draw back). A step after his own step is "after" it, never a wait row
    # (Nov 11, M1: part 2 after part 1 showed "Waiting on the step before"; tools/steps.waits_on_someone).
    held_by = {t for y in _all if y.state == "open" for t in y.after}
    for x in _all:
        who = S.waits_on_someone(x) if x.state == "open" and x.kind != "Waiting" else ""
        if who and x.id in held_by and x.id not in blk:
            waits.append({"id": x.id, "text": x.display(), "who": who,
                          "blocks": [y.id for y in _all if y.state == "open" and x.id in y.after], "step": True,
                          "since": _wait_since(body, "", x, _all)})
    since_of = {w["id"]: w.get("since") or "" for w in waits if w.get("id")}
    step_states = [{"id": x.id, "kind": x.kind, "state": x.state, "text": x.display(),
                    **({"done": x.done} if x.done else {}), **({"after": x.after} if x.after else {}),
                    **({"span": x.span} if x.span else {}),
                    **({"since": since_of[x.id]} if since_of.get(x.id) else {}),
                    **_v2_step(x, found)}
                   for x in _all if x.id]
    bs = _backsched(_all, since_of, tdue)
    by_id = {x.id: x for x in _all if x.id}
    for w in waits:
        c = (bs.get("chase") or {}).get(w.get("id"))
        if c is not None:
            # An open chase step is the chase: no Nudge beside it (D-38)
            open_chase = w.get("id") in by_id and S.chase_of(_all, by_id[w["id"]]) is not None
            w["chase_due"], w["chase_why"] = c["due"] and not open_chase, c["why"]
    # A chain that must start today (D-35 row) makes the task due today (never earlier: it is a start,
    # not a missed date); its own dates stay what the note says.
    start = max(bs["start_by"], clock.carmen_day().isoformat()) if bs.get("state") else ""
    return {"status": fm("status"), "scheduled": fm("scheduled")[:10],
            "due": min(d for d in (tdue, sdue, start) if d) if (tdue or sdue or start) else "",
            "task_due": tdue, "step_due": sdue, "step_due_text": stext,
            **({"backsched": {k: bs[k] for k in ("state", "start_by", "why")}} if bs.get("state") else {}),
            "waiting": fm("waiting").lower() == "true",
            "task_waiting": S.task_waiting(_all), "blocked": sorted(blk), "waits": waits,
            **({"after_label": al} if (al := {b: S.after_label(_all, b) for b in blk if S.after_label(_all, b)}) else {}),
            "next": nxt.display() if nxt else "",
            "next_id": nxt.id if nxt else "",
            "workspace": _workspace(body, p.stem),
            # Every step line with its stable id (^sxxxx, Nov 4): Do follows today's chunk by id,
            # so a reworded step stays the same step and only a ticked one counts as done.
            "step_states": step_states,
            **_step_order(step_states, None, str(fm("priority")).startswith("4") or fm("urgent").lower() == "true"),
            **({"urgent": True} if fm("urgent").lower() == "true" else {}),
            # The step contract (Tasks v2, Nov 6): {step id: reasons} for open steps that break it.
            **_unclear(body),
            "wait_line": wait.display() if wait else "",
            "task_id": fm("task_id"), "event_key": fm("event_key"),
            # Steps done today, for today's chunk (Nov 3): the app's own step lines, and a log line
            # from anyone else (a Claude chat) that says something was booked, sent, paid, ...
            "done_today": _done_today(body),
            "wait_since": next((w["since"] for w in waits if w.get("since")), ""),
            "priority": fm("priority"), "time_est": fm("timeEstimate"),
            "description": _description(body), "steps": [x["text"] for x in _later],
            "step_ids": [x["id"] for x in _later], "done_steps": _done_steps(body),
            "ticked": _ticked(body),
            **_dod(body)}


from tools.steps import MON as _MON, DATE_WORDS as _MDATE   # noqa: E402 - the one month list (Nov 11)


def _wait_since(body: str, who: str, step, steps: list) -> str:
    """The day an open wait began (r7 A: "since Oct 21" came from a ticked, closed wait): the last Log
    line that started a wait on that person, else the latest date the wait line itself names, else for
    a step others wait on (a check for a reply) the date of the step done just before it. ""."""
    today = clock.carmen_day()

    def iso(mon, d):
        try:
            x = dt.date(today.year, _MON[mon], int(d))
        except (KeyError, ValueError):
            return ""
        return (x.replace(year=today.year - 1) if x > today else x).isoformat()
    first = (who or "").split()[0] if who else ""
    if first and step.kind == "Waiting":
        hits = [iso(m.group(1), m.group(2)) for m in re.finditer(rf"^- ([A-Z][a-z]{{2}}) (\d{{1,2}}) · [^\n]*waiting on {re.escape(first)}", body, re.M)]
        if [h for h in hits if h]:
            return [h for h in hits if h][-1]
    named = [iso(m.group(2), m.group(3)) for m in _MDATE.finditer(step.text)]
    named = [d for d in named if d and d <= today.isoformat()]
    if step.kind == "Waiting" and named:
        return max(named)
    if step.kind != "Waiting":   # a check for a reply: since the step before it was done
        k = next((n for n, x in enumerate(steps) if x is step), 0)
        for x in reversed(steps[:k]):
            if x.state == "done":
                ds = [x.done, x.due] + [iso(m.group(2), m.group(3)) for m in _MDATE.finditer(x.text)]
                ds = [d for d in ds if d and d <= today.isoformat()]
                if ds:
                    return max(ds)
                break
    return ""


def _unclear(body: str) -> dict:
    """{"unclear": {step id: reasons}} with the contract flag on, else nothing (the view stays as
    before while Tasks v2 is switched off)."""
    from ..adapters import flags
    if not flags.on("contract"):
        return {}
    from . import contract
    x = contract.unclear_explained(body)
    return {"unclear": {sid: [e["title"] for e in xs] for sid, xs in x.items()}, "unclear_x": x}


def _v2_on() -> bool:
    from ..adapters import flags
    return any(v for k, v in flags.all_on().items() if k != "contract")


def _found(head: str, stem: str) -> dict:
    """{step id: the timeboxed step that found it} (tools/steps found_by), while a v2 flag is on."""
    if not _v2_on():
        return {}
    from tools import steps as S
    m = re.search(r"^task_id:\s*(t-[0-9a-f]{6})\s*$", head, re.M)
    return S.found_of(m.group(1) if m else stem)


def _v2_step(x, found: dict | None = None) -> dict:
    """A step's Tasks v2 fields (timebox, event, estimate in minutes, found_by), only while a v2
    flag is on."""
    if not _v2_on():
        return {}
    from ..adapters import flags
    so = flags.on("step_order")   # the computed order's inputs (Nov 11)
    return {**({"box": x.box} if x.box else {}), **({"event": x.event} if x.event else {}),
            **({"min": x.minutes} if x.minutes is not None else {}),
            **({"due": x.due} if so and x.due else {}), **({"optional": True} if so and x.optional else {}),
            **({"gate": x.gate} if so and x.gate else {}),
            **({"found_by": found[x.id]} if found and x.id in found else {})}


import contextvars

# The day's free minutes, set by Planning while it builds a view or a plan (Nov 11: the piece is
# sized by the time the day has). None: unknown, the piece is sized by the deadlines alone.
ROOM = contextvars.ContextVar("carmen_room", default=None)
ORDER_KEYS = ("plan", "piece", "optional", "held", "why", "parts", "budget", "need")


def _step_order(step_states: list[dict], room: int | None = None, high: bool = False) -> dict:
    """The computed step order (flag step_order, Nov 11): {"order": {"plan", "piece", "optional",
    "held", "why", "parts", "budget", "need"}}, or {} with the flag off. The note's lines never move."""
    from ..adapters import flags
    if not flags.on("step_order"):
        return {}
    from . import estimates
    try:
        r = estimates.piece_from(step_states, clock.carmen_day(), room, high)
    except Exception:   # noqa: BLE001 - the plan falls back to file order
        return {}
    return {"order": {k: r[k] for k in ORDER_KEYS}}


def order_now(live: dict) -> dict:
    """A note's order sized by the day's free time when Planning has set it (ROOM), else as read."""
    room = ROOM.get()
    if room is None or not live.get("order"):
        return live.get("order") or {}
    return _step_order(live.get("step_states") or [], room, is_high(live)).get("order") or live["order"]


def is_high(live: dict) -> bool:
    """A task he marked High or urgent: the piece's caps per step step aside (Nov 11)."""
    return str(live.get("priority") or "").startswith("4") or bool(live.get("urgent"))


def _step_key(t: str) -> str:
    t = re.sub(r"\s*\((?:the owner[^)]*|\d+\s*min|[\d.]+\s*h)\)\s*$", "", t.strip())
    return re.sub(r"\W+", " ", t.lower()).strip()[:80]


def _ticked(body: str) -> list[str]:
    """Every step the note shows as done: ticked lines in ## Next action ("- [x] Next: ...",
    struck text unwrapped) and the app's "step done:" / "step ticked:" Log lines. Do uses it to
    tell a step that was done from one that was only reworded (Nov 4)."""
    out = []
    for m in re.finditer(r"^\s*- \[x\] (?:(?:Next|Then): )?(.+)$", body, re.M):
        t = m.group(1).strip()
        s = re.match(r"~~(.+?)~~", t)
        out.append(s.group(1) if s else t)
    out += [m.group(1) for m in re.finditer(r"^- [A-Z][a-z]{2} \d{1,2} · step (?:done|ticked): (.+?)(?: · |$)", body, re.M)]
    return out


def _done_steps(body: str, limit: int = 3, today: dt.date | None = None) -> list[dict]:
    """The last steps he finished, newest first, for the Start panel under Next (owner decision, Nov 3:
    "history of past 3 steps that were accomplished would be nice to see below next steps").
    From the app's Log lines ("step done:", "step ticked:"), which carry the date, and the ticked
    lines in ## Next action that no Log line names, dated when the line says "(Nov 3". Undated
    ones come last. Each is {"t": the step, "d": "Nov 3" or ""}."""
    today = today or clock.carmen_day()

    def order(d: str) -> int:
        m = re.match(r"([A-Z][a-z]{2}) (\d{1,2})$", d or "")
        if not m or m.group(1) not in _MON:
            return 0
        try:
            x = dt.date(today.year, _MON[m.group(1)], int(m.group(2)))
        except ValueError:
            return 0
        return (x.replace(year=x.year - 1) if x > today else x).toordinal()

    out, seen = [], set()
    log = body.split("\n## Log", 1)[1] if "\n## Log" in body else ""
    for line in reversed(log.splitlines()):
        m = re.match(r"^- ([A-Z][a-z]{2} \d{1,2}) · step (?:done|ticked): (.+?)(?: · |$)", line)
        if m and _step_key(m.group(2)) not in seen:
            seen.add(_step_key(m.group(2)))
            out.append({"t": m.group(2).strip(), "d": m.group(1)})
    from tools import steps as S
    for st in S.steps_of(body):
        t = st.text
        if st.state != "done" or not t or _step_key(t) in seen:
            continue
        seen.add(_step_key(t))
        if st.done:   # the ✅ date (Nov 4), else a "(Nov 3" in the words
            d = clock._label(dt.date.fromisoformat(st.done))
        else:
            d = (lambda x: x.group(1) if x else "")(re.search(r"\(([A-Z][a-z]{2} \d{1,2})\b", t))
        out.append({"t": t, "d": d})
    out.sort(key=lambda x: -order(x["d"]))   # stable: within a day, the Log's newest first
    return out[:limit]


# D-16
def _dod(body: str) -> dict:
    """The Definition of Done (Nov 1, format from the DoD design session): the line
    "DoD: <text>" or "DoD (Claude's guess): <text>" inside the Next action block."""
    block = re.search(r"^## Next action\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    m = re.search(r"^DoD( \(Claude's guess\))?: (.+)$", block.group(1), re.M) if block else None
    from tools import steps as S
    return ({"dod": S.HIS_TAG.sub("", m.group(2)).strip(), "dod_guess": bool(m.group(1))}
            if m else {"dod": "", "dod_guess": False})


WS_MAX = 3   # links per step: a step's workspace stays very short (owner decision, Nov 4)


def _ws_link(item: str, note: str = ""):
    """One workspace line as markdown: a wikilink ([[Note#Section|Label]], or [[#Section|Label]] for a
    section of this note) becomes an obsidian:// link the app signs into its file view, a section
    opening just that section; anything else (a URL, a [label](url)) stays as written."""
    from urllib.parse import quote
    from tools import steps as S
    w = S.wikilink(item, note)
    if not w:
        return item.strip()
    name, sec, label = w
    rel = name if "/" in name else ("Inbox/" + name if (config.INBOX / f"{name}.md").is_file() else name)
    target = rel + (f"#{sec}" if sec else "")
    # obs: the link for Obsidian itself, with file before vault, so link_vault_files (which signs
    # "obsidian://open?vault=...&file=...") leaves it as it is; md: the signed read-only page.
    return {"label": label, "obs": f"obsidian://open?file={quote(target, safe='')}&vault=<VAULT_NAME>",
            "md": f"[{label}](obsidian://open?vault=<VAULT_NAME>&file={quote(target, safe='')})"}


def _workspace(body: str, note: str = "") -> dict[str, list[str]]:
    """{open step as shown: its links} (Nov 4, the owner: "the small workspace for the substep
    where relevant information is contained"): indented "- " lines right under an open step
    line in ## Next action, at most WS_MAX. They are not steps (no checkbox), so step tools
    leave them alone; a struck or done step's links are no longer shown."""
    from tools import steps as S
    lines = body.split("\n")
    r = S.block_range(lines)
    out, cur = {}, None
    for ln in lines[r[0] + 1:r[1]] if r else []:
        st = S.parse_line(ln)
        if st:
            cur = (st.id or st.display()) if st.state == "open" else None
            continue
        m = re.match(r"^\s+[-*] (.+)$", ln)
        if m and cur:
            if len(out.setdefault(cur, [])) < WS_MAX:
                out[cur].append(_ws_link(m.group(1), note))
        elif ln.strip():
            cur = None
    return out


def _steps_with_ids(body: str, limit: int = 8) -> list[dict]:
    """The open steps after Next (Nov 1, the Start panel: "a short overview of the steps
    afterwards"): the open step lines of `## Next action`, each {"text", "id"}, so the page can
    name a step by its id (refactor step 1, Nov 5). The block only (Nov 11: the HR note's
    `## Steps` checklist further down showed as Next steps, without ids or dates)."""
    from tools import steps as S
    out = [{"text": st.display(), "id": st.id or ""} for st in S.steps_of(body)
           if st.state == "open" and st.kind not in ("Next", "Waiting")]
    return out[:limit]


def _steps(body: str, limit: int = 8) -> list[str]:
    return [x["text"] for x in _steps_with_ids(body, limit)]


def _description(body: str, limit: int = 700) -> str:
    """The note's own text for the detail view (Oct 30): without the Next action
    checkboxes, the pre-run's Current context, the Log and its headings."""
    body = re.sub(r"## Current context.*?%% end current context %%", "", body, flags=re.S)
    body = body.split("\n## Log", 1)[0]
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith("## Next action") or re.match(r"^- \[[ x]\] (Next|Then|Waiting):", s):
            continue
        # Nov 1, the owner: the description "reads weird". Struck lines are replaced steps (the new
        # one shows as Next or a step), the DoD shows on its own; ticked steps read as done.
        if re.match(r"^- ~~", s) or re.match(r"^DoD( \(Claude's guess\))?: ", s):
            continue
        s = re.sub(r"^- \[x\] ", "✓ ", s)
        s = re.sub(r"^- \[ \] ", "○ ", s)
        s = re.sub(r"^[-*] ", "• ", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
        lines.append(re.sub(r"^#+\s*", "", s))
    text = "\n".join(lines).strip()
    # Never cut inside a link (Nov 4: a vault link cut at 700 characters lost its ")" and showed
    # as a raw URL across the desktop layout): a cut that lands in one moves to its end, since
    # the page shows it short anyway.
    for m in re.finditer(r"\[[^\]\n]*\]\([^)\s]*\)|https?://\S+", text):
        if m.start() < limit < m.end():
            limit = m.end()
    return text[:limit] + ("…" if len(text) > limit else "")


def _detail(live: dict, task_est: str = "", next_est: str = "") -> dict:
    """The fields the detail view shows, from the live note (owner decision, Oct 30: priority,
    estimate, due date, description, status, next step)."""
    if not live:
        return {}
    te = task_est or (f"{live['time_est']}m" if str(live.get("time_est", "")).isdigit() else "")
    return {"priority": live.get("priority", ""), "status": "waiting" if live.get("waiting") else live.get("status", ""),
            "status_raw": live.get("status", ""),
            "due": live.get("due", ""), "scheduled": live.get("scheduled", ""),
            "task_est": te, "next_est": next_est, "description": live.get("description", ""),
            "steps": live.get("steps", []), "done_steps": live.get("done_steps", []),
            "next_id": live.get("next_id", ""), "step_ids": live.get("step_ids", []),
            "blocked": live.get("blocked", []), "waits": live.get("waits", []),
            **({"after_label": live["after_label"]} if live.get("after_label") else {}),
            "task_waiting": bool(live.get("task_waiting")), "wait_since": live.get("wait_since", ""),
            **({"order": order_now(live)} if live.get("order") else {}),
            **({"unclear": live["unclear"]} if live.get("unclear") else {}),
            "dod": live.get("dod", ""), "dod_guess": live.get("dod_guess", False),
            "ws": live.get("workspace", {})}


# A ticked checkbox with its done date, at any depth: "- [x] Next: Send it (15 min) ✅ 2026-11-10 ^s3ef9".
_TICKED = re.compile(r"^[ \t]*- \[[xX]\][ \t]+(.*?)[ \t]*✅[ \t]*(\d{4}-\d{2}-\d{2})[^\n]*$", re.M)


def _tick_text(raw: str) -> str:
    """A ticked step as a Progress line: no Next/Then lead, minutes, proof, dates or block id."""
    t = re.sub(r"^(?:Next|Then|Waiting):\s*", "", raw.strip())
    t = re.sub(r"\s*\((?:proof|after|due)\b[^()]*(?:\([^()]*\)[^()]*)*\)", "", t)
    from tools import steps as S
    t = S.EST_IN.sub("", t)
    t = re.sub(r"\s*📅\s*\d{4}-\d{2}-\d{2}", "", t)
    t = re.sub(r"\s*\^[\w-]+\s*$", "", t)
    t = S.HIS_TAG.sub("", t.replace("~~", "")).strip()   # his note on the line, as clean() drops it (one rule, audit A3)
    return t[:1].upper() + t[1:]


_WINS_LINES: dict = {}   # (path, mtime) -> the note's Log-shaped lines; a note is reread only after a change


def today_wins(day: dt.date) -> dict:
    """What he finished today, small things included, for the Today fold on Do (owner decision, Nov 3:
    "more a 'Ah cool I already did all this'"). Read from the Log lines the app writes into each
    note ("step done:", "step ticked:", "done", "sent mail to"), notes touched today only.
    Claude's prep ("prepared by Claude: N steps done") is counted apart, its largest run per task."""
    pref = f"- {clock._label(day)} · "
    start = dt.datetime.combine(day, dt.time(0), clock.TZ).timestamp()
    wins, prep = [], {}
    words = lambda s: set(re.findall(r"[a-z]{4,}", s.lower()))
    paths = sorted(config.INBOX.glob("*.md"), key=lambda q: q.stat().st_mtime, reverse=True)
    for p in paths:
        try:
            mt = p.stat().st_mtime
            if mt < start:
                break
            key = (str(p), mt, "v3")
            if key not in _WINS_LINES:   # read a note again only when it changed
                text = p.read_text(encoding="utf-8", errors="replace")
                head = text.partition("\n---\n")[0]
                closed = re.search(r"^completedDate:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
                _WINS_LINES[key] = ([l for l in text.splitlines() if l.startswith("- ") and " · " in l],
                                    closed.group(1) if closed and re.search(r"^status:\s*done\s*$", head, re.M) else "",
                                    [(m.group(2), m.group(1)) for m in _TICKED.finditer(text)])
            lines, closed_on, ticked = _WINS_LINES[key]
        except OSError:
            continue
        n_before = len(wins)
        for line in reversed(lines):
            if not line.startswith(pref):
                continue
            rest = line[len(pref):]
            m = re.match(r"step (?:done|ticked): (.+?) · ", rest)
            if m:
                wins.append({"t": m.group(1), "task": p.stem})
            elif rest.startswith("done · "):
                wins.append({"t": "Finished the task", "task": p.stem, "big": True})
            elif rest.startswith("sent mail to "):
                wins.append({"t": "Sent mail to " + rest[len("sent mail to "):].split(" · ")[0], "task": p.stem})
            else:
                m = re.match(r"prepared by Claude: (\d+) steps? done", rest)
                if m:
                    prep[p.stem] = max(prep.get(p.stem, 0), int(m.group(1)))
                elif _WORK_DONE.search(rest) and not _NOT_WORK.search(rest):
                    # What he did and told Claude in a chat (Nov 3, "San mateo booked"): the
                    # chat logs it in the note, so it counts like a tap. The source is dropped.
                    said = re.sub(r"\s+·\s+[^·]*$", "", rest).strip()
                    said = said if len(said) <= 160 else said[:158].rsplit(" ", 1)[0] + " …"
                    if said:
                        wins.append({"t": said[:1].upper() + said[1:], "task": p.stem, "chat": True})
        # A step ticked today however it was ticked (Nov 10, the FNV guide: a chat ticked the box
        # and wrote one Log line that also said "drafted, not sent", so the not-work filter dropped
        # the whole line and Progress missed the step): the checkbox's done date is the record.
        # A tap's own "step done:" line already says it, so a box that line names is not counted twice.
        logged = [words(w["t"]) for w in wins[n_before:] if not w.get("chat") and not w.get("big")]
        for d_iso, raw in ticked:
            # A ticked wait is their answer arriving, not his work (the ECON split question).
            if d_iso != day.isoformat() or raw.lstrip().lower().startswith("waiting"):
                continue
            t = _tick_text(raw)
            if not t:
                continue
            tw = words(t)
            if any(len(tw & lw) >= min(3, len(tw) or 1) for lw in logged):
                continue
            wins.append({"t": t, "task": p.stem})
        # Closed today however it was closed (Nov 3: a chat closed the sports medicine task
        # after 16:00 PT and dated its log line by the box's UTC clock, Nov 4, so Progress
        # missed it): completedDate is the record.
        if closed_on == day.isoformat() and not any(w.get("big") and w["task"] == p.stem for w in wins[n_before:]):
            wins.append({"t": "Finished the task", "task": p.stem, "big": True})
    # A chat line is not counted again when a tapped or ticked step of the same note already says it.
    taps = [(w["task"], words(w["t"])) for w in wins if not w.get("chat") and not w.get("big")]
    wins = [w for w in wins if not w.get("chat")
            or not any(t == w["task"] and len(ws & words(w["t"])) >= 3 for t, ws in taps)]
    return {"items": wins, "claude_steps": sum(prep.values()), "claude_tasks": len(prep)}


def closed_recently(today: dt.date) -> list[dict]:
    """Tasks he closed today or yesterday, for the Done fold (owner decision, Oct 31: "I don't want
    them to disappear ... rather they should appear in the bottom folder in Done"). Read
    from the notes themselves, so a task closed anywhere shows: a tap, the chat, Obsidian."""
    days = {today.isoformat(): "today", (today - dt.timedelta(days=1)).isoformat(): "yesterday"}
    out = []
    for folder in (config.INBOX, config.VAULT / "Archive" / "Inbox"):
        for p in folder.glob("*.md") if folder.exists() else []:
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            m = re.match(r"---\s*\n(.*?)\n---", head, re.S)
            fm = m.group(1) if m else ""
            cd = re.search(r"^completedDate:\s*['\"]?(\d{4}-\d{2}-\d{2})", fm, re.M)
            if not (re.search(r"^type:\s*['\"]?action", fm, re.M) and re.search(r"^status:\s*['\"]?done", fm, re.M)
                    and cd and cd.group(1) in days):
                continue
            live = read_note(p) if folder == config.INBOX else {}
            try:   # closed by auto-close (Nov 4): the fold says so, with Undo
                from . import autoclose
                ac = autoclose.info(p.stem) if folder == config.INBOX else None
            except Exception:
                ac = None
            out.append({"auto_closed": ac["why"] if ac and ac.get("at") == cd.group(1) else "",
                        "id": p.stem, "task": folder == config.INBOX, "title": p.stem, "group": "Done",
                        "prio": direction.FM_PRIO.get(live.get("priority", ""), "Z"), "guess": "done",
                        "next": f"Done {days[cd.group(1)]}", "why": "", "meta": "", "waiting": "", "est": "",
                        "due": "", "anx": False, "urgent": False, "claude": [], "prep": [], "draft": None,
                        "later": False, "tab": "today", "closed": cd.group(1),
                        "detail": _detail(live, "", "") if live else {}})
    return sorted(out, key=lambda r: r["closed"], reverse=True)


DOD_RE = re.compile(r"^DoD( \(Claude's guess\))?: (.+)$", re.M)

STEP_OPEN = re.compile(r"^(\s*)- \[ \] (?:(Next|Then|Waiting): )?(.*)$")

from tools.steps import HIS_TAG  # noqa: E402,F401 - his tag, anywhere in the words: the one rule (audit A3)

PROP_IGNORED = config.STATE_DIR / "checkin_app" / "prop_ignored.json"


def _norm_step(t: str) -> str:
    from tools import steps as S
    return " ".join(HIS_TAG.sub("", S.clean(t or "")).lower().split())


def _ignored_props() -> dict:
    try:
        d = json.loads(PROP_IGNORED.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


# --- Tasks v2: estimates as read, his calibration on top; the actual checks (Estimates) -----------
def est_note(live: dict) -> str:
    """The Next step's estimate with his calibration, "~40 min (you usually take 1.4×)", or ""."""
    from . import estimates
    nid = live.get("next_id")
    x = next((x for x in live.get("step_states") or [] if x.get("id") == nid), None)
    if not x:
        return ""
    return estimates.shown(x.get("min") or x.get("box"), x.get("text", ""), x.get("box"))


def actual_asks(day: dt.date) -> list[dict]:
    from . import estimates
    return estimates.asks(day)


def step_review() -> list[dict]:
    """The steps to sharpen (Nov 6), for the Claude tab: unclear Next steps not marked fine."""
    from . import review
    return review.pending()


def cleanup_pending() -> list[dict]:
    """The backfill's step cleanups waiting for his approval (slice 7), for the Claude tab."""
    from . import backfill
    return [{k: v for k, v in x.items() if k != "after_raw"} for x in backfill.pending()]
