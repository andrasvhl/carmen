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


def _stepmod():
    from . import steps
    return steps


_NOTE_CACHE: dict = {}   # path -> ((mtime_ns, size, day), parsed note)

_WORK_DONE = re.compile(r"\b(?:booked|sent|submitted|paid|completed|confirmed|done|ticked|replied|uploaded|filled|"
                        r"messaged|texted|ordered|bought|returned|cancelled|signed)\b", re.I)

# Edits are never work (Nov 4: "DoD set: ... booked ... confirmed" counted as done work, and
# with the Next step reworded by a chat the India task left Do as finished for the day).
_NOT_WORK = re.compile(r"prepared by Claude|DoD proposed|DoD drafted|DoD set|DoD confirmed|Next set to|"
                       r"steps? (?:edited|discarded|rewritten|reworded)|rewritten by|"
                       r"your meditation|scheduled for|not waiting|"
                       r"(?:^|·\s)waiting on|not (?:yet )?(?:sent|booked|paid|submitted|done)", re.I)

_DUE_MARK = re.compile(r"\(due (?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*\s+)?([A-Z][a-z]{2})\w*\s+(\d{1,2})\)")

_MON = {m: n for n, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _step_due(body: str) -> tuple[str, str]:
    """The earliest deadline on an open step (📅, or an old "(due Mon D)"), as (YYYY-MM-DD, the
    step's display text). Waiting lines are not his steps."""
    from tools import steps as S
    best = ("", "")
    for st in S.steps_of(body):
        if st.state == "open" and st.kind != "Waiting" and st.due and (not best[0] or st.due < best[0]):
            best = (st.due, st.display())
    return best


def _done_today(body: str) -> int:
    pref = f"- {clock._label(dt.datetime.now(clock.TZ).date())} · "
    return sum(1 for l in body.splitlines()
               if l.startswith(pref) and _WORK_DONE.search(l) and not _NOT_WORK.search(l))


def read_note(p: Path) -> dict:
    """read_note, kept per file until the file changes (or the day turns, for wait_since);
    a deep copy each time, so no caller edits the cache (Nov 3, speed)."""
    import copy
    try:
        st = p.stat()
        # The flags are in the key: what a note shows depends on them (Tasks v2), and a flag
        # switched on must show without a restart or an edit to every note (Nov 6).
        from ..adapters import flags
        sig = (st.st_mtime_ns, st.st_size, dt.datetime.now(clock.TZ).date(), tuple(flags.all_on().values()))
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
    return {"status": fm("status"), "scheduled": fm("scheduled")[:10],
            "due": min(d for d in (tdue, sdue) if d) if (tdue or sdue) else "",
            "task_due": tdue, "step_due": sdue, "step_due_text": stext,
            "waiting": fm("waiting").lower() == "true",
            "next": nxt.display() if nxt else "",
            "next_id": nxt.id if nxt else "",
            "workspace": _workspace(body),
            # Every step line with its stable id (^sxxxx, Nov 4): Do follows today's chunk by id,
            # so a reworded step stays the same step and only a ticked one counts as done.
            "step_states": [{"id": x.id, "kind": x.kind, "state": x.state, "text": x.display(), **_v2_step(x, found)}
                            for x in _all if x.id],
            # The step contract (Tasks v2, Nov 6): {step id: reasons} for open steps that break it.
            **_unclear(body),
            "wait_line": wait.display() if wait else "",
            "task_id": fm("task_id"), "event_key": fm("event_key"),
            # Steps done today, for today's chunk (Nov 3): the app's own step lines, and a log line
            # from anyone else (a Claude chat) that says something was booked, sent, paid, ...
            "done_today": _done_today(body),
            "wait_since": (lambda d: d.isoformat() if d else "")(
                _stepmod().waiting_since(body, dt.datetime.now(clock.TZ).date())),
            "priority": fm("priority"), "time_est": fm("timeEstimate"),
            "description": _description(body), "steps": [x["text"] for x in _later],
            "step_ids": [x["id"] for x in _later], "done_steps": _done_steps(body),
            "ticked": _ticked(body),
            **_dod(body)}


_MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


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
    return {**({"box": x.box} if x.box else {}), **({"event": x.event} if x.event else {}),
            **({"min": x.minutes} if x.minutes is not None else {}),
            **({"found_by": found[x.id]} if found and x.id in found else {})}


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
    today = today or dt.datetime.now(clock.TZ).date()

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


def _dod(body: str) -> dict:
    """The Definition of Done (Nov 1, format from the DoD design session): the line
    "DoD: <text>" or "DoD (Claude's guess): <text>" inside the Next action block."""
    block = re.search(r"^## Next action\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    m = re.search(r"^DoD( \(Claude's guess\))?: (.+)$", block.group(1), re.M) if block else None
    return ({"dod": re.sub(r"\s*\(the owner[^)]*\)\s*$", "", m.group(2)).strip(), "dod_guess": bool(m.group(1))}
            if m else {"dod": "", "dod_guess": False})


WS_MAX = 3   # links per step: a step's workspace stays very short (owner decision, Nov 4)


def _ws_link(item: str):
    """One workspace line as markdown: a wikilink ([[Note#Section|Label]]) becomes an obsidian://
    link the app signs into its file view, a section opening just that section; anything else
    (a URL, a [label](url)) stays as written."""
    from urllib.parse import quote
    m = re.fullmatch(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]", item.strip())
    if not m:
        return item.strip()
    name, sec, label = m.group(1).strip(), (m.group(2) or "").strip(), (m.group(3) or "").strip()
    rel = name if "/" in name else ("Inbox/" + name if (config.INBOX / f"{name}.md").is_file() else name)
    target = rel + (f"#{sec}" if sec else "")
    label = label or sec or name
    # obs: the link for Obsidian itself, with file before vault, so link_vault_files (which signs
    # "obsidian://open?vault=...&file=...") leaves it as it is; md: the signed read-only page.
    return {"label": label, "obs": f"obsidian://open?file={quote(target, safe='')}&vault=<VAULT_NAME>",
            "md": f"[{label}](obsidian://open?vault=<VAULT_NAME>&file={quote(target, safe='')})"}


def _workspace(body: str) -> dict[str, list[str]]:
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
                out[cur].append(_ws_link(m.group(1)))
        elif ln.strip():
            cur = None
    return out


def _steps_with_ids(body: str, limit: int = 8) -> list[dict]:
    """The open steps after Next (Nov 1, the Start panel: "a short overview of the steps
    afterwards"): the note's unchecked boxes outside Current context and the Log, each
    {"text", "id"}, so the page can name a step by its id (refactor step 1, Nov 5)."""
    from tools import steps as S
    body = re.sub(r"## Current context.*?%% end current context %%", "", body, flags=re.S)
    body = body.split("\n## Log", 1)[0]
    out = []
    for ln in body.splitlines():
        st = S.parse_line(ln)
        if st and st.state == "open" and st.kind not in ("Next", "Waiting"):
            out.append({"text": st.display(), "id": st.id or ""})
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
            **({"unclear": live["unclear"]} if live.get("unclear") else {}),
            "dod": live.get("dod", ""), "dod_guess": live.get("dod_guess", False),
            "ws": live.get("workspace", {})}


_WINS_LINES: dict = {}   # (path, mtime) -> the note's Log-shaped lines; a note is reread only after a change


def today_wins(day: dt.date) -> dict:
    """What he finished today, small things included, for the Today fold on Do (owner decision, Nov 3:
    "more a 'Ah cool I already did all this'"). Read from the Log lines the app writes into each
    note ("step done:", "step ticked:", "done", "sent mail to"), notes touched today only.
    Claude's prep ("prepared by Claude: N steps done") is counted apart, its largest run per task."""
    pref = f"- {clock._label(day)} · "
    start = dt.datetime.combine(day, dt.time(0), clock.TZ).timestamp()
    wins, prep = [], {}
    paths = sorted(config.INBOX.glob("*.md"), key=lambda q: q.stat().st_mtime, reverse=True)
    for p in paths:
        try:
            mt = p.stat().st_mtime
            if mt < start:
                break
            key = (str(p), mt, "v2")
            if key not in _WINS_LINES:   # read a note again only when it changed
                text = p.read_text(encoding="utf-8", errors="replace")
                head = text.partition("\n---\n")[0]
                closed = re.search(r"^completedDate:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
                _WINS_LINES[key] = ([l for l in text.splitlines() if l.startswith("- ") and " · " in l],
                                    closed.group(1) if closed and re.search(r"^status:\s*done\s*$", head, re.M) else "")
            lines, closed_on = _WINS_LINES[key]
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
        # Closed today however it was closed (Nov 3: a chat closed the sports medicine task
        # after 16:00 PT and dated its log line by the box's UTC clock, Nov 4, so Progress
        # missed it): completedDate is the record.
        if closed_on == day.isoformat() and not any(w.get("big") and w["task"] == p.stem for w in wins[n_before:]):
            wins.append({"t": "Finished the task", "task": p.stem, "big": True})
    # A chat line is not counted again when a tapped step of the same note already says it.
    words = lambda s: set(re.findall(r"[a-z]{4,}", s.lower()))
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

HIS_TAG = re.compile(r"\s*\(the owner[^)]*\)\s*$")

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
