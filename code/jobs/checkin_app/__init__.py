"""Check-in Mini App: the plan-today rows as a Telegram Mini App (MVP, 2026-10-30).

owner decision, Oct 30: the check-in must not be one chained morning run, must be
reachable from phone and laptop whenever, and must live in a single interface
on the phone. So the rows the 08:00 pre-run already writes
(`_system/claude/plan-today-prerun.md`) are served as a Mini App that opens
inside the vault bot's chat. Taps write the task note at once; Talk posts the
row into the chat, where replying continues it with the tg_bridge agent.

Auth, two ways to the same page (owner decision, Oct 30: one system for phone and
desktop, "so can use from chrome? and from claude browser?"):
  - Inside Telegram: the Mini App's initData, signed with the bot token; only
    TELEGRAM_OWNER_ID passes.
  - In a browser: CHECKIN_PASSWORD once, then a 30-day signed cookie
    (CHECKIN_SECRET). Five wrong passwords lock the login for 10 minutes.
The page itself holds no data. Binds 127.0.0.1 only; the Tailscale Funnel path
/checkin proxies it.

Design and open questions: Inbox/Review plan-today after its first week.md.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
import signal as signal_mod
import threading
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from runner import config, notify

# The box runs UTC: without this an evening tap logged tomorrow's date and
# "Tomorrow" skipped a day (Oct 30, his first test).
TZ = ZoneInfo("America/Los_Angeles")

PAGE = config.VAULT / "_system" / "claude" / "plan-today-prerun.md"
HTML = Path(__file__).with_name("page.html")
PORT = int(os.environ.get("CHECKIN_PORT", "8790"))
MAX_AUTH_AGE = 86400       # Telegram reissues initData every time the app opens
TOP_N = 5                  # decisions under the one thing; a hard cap, not a filter (Oct 30)

PRIO = {"H": 0, "N": 1, "Z": 2}
VOICE_MAX_BYTES = 20 * 1024 * 1024   # Groq's upload cap is 25 MB; the page stops recording at 5 min


# --- auth -----------------------------------------------------------------------

def verify_init_data(init_data: str, token: str, owner: str, now: float | None = None) -> bool:
    """Telegram's Mini App check: HMAC of the sorted fields, keyed by the bot token."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", "")
    if not got or not token or not owner:
        return False
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return False
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > MAX_AUTH_AGE:
            return False
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return False
    return str(user.get("id")) == str(owner)


COOKIE = "checkin"
COOKIE_AGE = 30 * 86400
LOCK_AFTER, LOCK_FOR = 5, 600


def make_cookie(secret: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + COOKIE_AGE))
    return f"{exp}.{hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()}"


def verify_cookie(value: str, secret: str, now: float | None = None) -> bool:
    exp, _, sig = (value or "").partition(".")
    if not secret or not exp.isdigit() or not sig:
        return False
    want = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig) and int(exp) > (now or time.time())


class Lockout:
    """In-memory: a restart clears it, which is fine for one owner."""
    def __init__(self):
        self.fails: list[float] = []

    def locked(self, now: float | None = None) -> bool:
        now = now or time.time()
        self.fails = [t for t in self.fails if now - t < LOCK_FOR]
        return len(self.fails) >= LOCK_AFTER

    def fail(self, now: float | None = None) -> None:
        self.fails.append(now or time.time())


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Carmen</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Carmen">
<style>body{font:15px -apple-system,system-ui,sans-serif;background:#f2f2f7;display:flex;
justify-content:center;padding:60px 16px}form{background:#fff;border-radius:12px;padding:20px;
width:100%;max-width:320px}input,button{width:100%;font:inherit;padding:10px;border-radius:8px;
border:1px solid #ccc;margin-top:10px;box-sizing:border-box}button{background:#2481cc;color:#fff;
border:0}p{color:#c33;font-size:13px}@media (prefers-color-scheme:dark){body{background:#000}
form{background:#1c1c1e;color:#fff}input{background:#2c2c2e;color:#fff;border-color:#3a3a3c}}</style>
</head><body><form method="post"><b>Carmen</b>{msg}
<input type="password" name="password" autocomplete="current-password" placeholder="Password" autofocus>
<button>Sign in</button></form></body></html>"""


# --- reading the pre-run --------------------------------------------------------

# The rows are model output, from the pre-run and from the quick update. On Oct 31 one wrote
# "lk": 1 and another an estimate as a bare number, and each took the whole view down (16 times
# that evening). So every row is checked here, once, on the way in: a field of the wrong type is
# fixed or dropped, a row with no name is dropped, and nothing downstream has to guard (Nov 1).
_STR_KEYS = ("n", "g", "s", "nx", "m", "upd", "wf", "p", "bk", "wb", "ov")
_LIST_KEYS = ("ctx", "rw", "steps", "td")
_MSG_STR = ("via", "to", "subj", "body", "acct")


def _as_str(v) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return None


def _est(v) -> str | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{int(v)}m"
    return v if isinstance(v, str) else None


def clean_row(r) -> dict | None:
    if not isinstance(r, dict):
        return None
    r = dict(r)
    for k in _STR_KEYS:
        if k in r:
            v = _as_str(r[k])
            if v is None:
                r.pop(k)
            else:
                r[k] = v
    for k in ("e", "ne"):
        if k in r:
            v = _est(r[k])
            if v is None:
                r.pop(k)
            else:
                r[k] = v
    if "lk" in r and not (isinstance(r["lk"], str) and r["lk"]):
        # "lk": 1 meant "linked": keep the link when the title is a task note's name.
        if r["lk"] and isinstance(r.get("n"), str) and task_path(r["n"]):
            r["lk"] = r["n"]
        else:
            r.pop("lk")
    if not r.get("n"):
        if not r.get("lk"):
            return None
        r["n"] = r["lk"]
    for k in _LIST_KEYS:
        if k in r and not isinstance(r[k], list):
            r.pop(k)
    ch = []
    for a in r.get("ch") if isinstance(r.get("ch"), list) else []:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if "t" in a:
            a["t"] = _as_str(a["t"]) or ""
        if "msg" in a:
            msg = a["msg"]
            if isinstance(msg, dict) and msg:
                msg = {**msg, **{k: _as_str(msg[k]) or "" for k in _MSG_STR if k in msg}}
                if "slots" in msg and not isinstance(msg["slots"], list):
                    msg.pop("slots")
                a["msg"] = msg
            else:
                a.pop("msg")
        if a.get("msg") or a.get("t"):
            ch.append(a)
    if "ch" in r:
        r["ch"] = ch
    if "cp" in r:
        r["cp"] = [{**a, "t": _as_str(a.get("t")) or ""} for a in r["cp"]
                   if isinstance(a, dict) and _as_str(a.get("t"))] if isinstance(r["cp"], list) else []
    return r


def clean_rows(rows) -> list:
    """[[group, [row, ...]], ...] with every group a string and every row through clean_row.
    A dict of groups is accepted too; anything else is no rows."""
    if isinstance(rows, dict):
        rows = list(rows.items())
    out = []
    for pair in rows if isinstance(rows, list) else []:
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[1], list)):
            continue
        group = _as_str(pair[0])
        if group is None:
            continue
        items = [c for c in (clean_row(r) for r in pair[1]) if c]
        if items:
            out.append([group, items])
    return out


def load_prerun(path: Path = PAGE, overlay: bool = True) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"```json\n(.*?)\n```", text, re.S)
    try:
        data = json.loads(m.group(1)) if m else {}
    except ValueError:   # a half-written pre-run: no rows rather than no app
        data = {}
    data = data if isinstance(data, dict) else {}
    data["rows"] = clean_rows(data.get("rows"))
    data["notes"] = [n for n in data.get("notes") or [] if isinstance(n, str)] if isinstance(data.get("notes"), list) else []
    if not isinstance(data.get("journal"), dict):
        data["journal"] = {}
    gen = re.search(r"^generated:\s*(\S+)", text, re.M)
    data["generated"] = gen.group(1) if gen else ""
    # The quick update (update.py: messages since the pre-run and today's meditation) stands
    # in for the pre-run's rows while it was built on this very pre-run (Oct 31).
    if overlay:
        try:
            up = json.loads(UPDATE.read_text(encoding="utf-8"))
            rows = clean_rows(up.get("rows")) if isinstance(up, dict) else []
            if up.get("prerun") == data["generated"] and rows:
                notes = up.get("notes") if isinstance(up.get("notes"), list) else []
                data = {**data, "rows": rows,
                        "journal": up.get("journal") if isinstance(up.get("journal"), dict) and up["journal"] else data["journal"],
                        "updated": _as_str(up.get("at")) or "",
                        "notes": [n for n in notes if isinstance(n, str)] or data["notes"]}
        except (OSError, ValueError, AttributeError):
            pass
    return data


def task_path(name: str) -> Path | None:
    if not isinstance(name, str) or not name or "/" in name or name.startswith("."):
        return None
    p = config.INBOX / f"{name}.md"
    return p if p.is_file() else None


def read_note(p: Path) -> dict:
    """The live state of a task note: what the app shows over the pre-run's copy,
    so a change made anywhere (a tap, Claude in chat, Obsidian) shows at once."""
    text = p.read_text(encoding="utf-8", errors="replace")
    head, _, body = text.partition("\n---\n")

    def fm(key):
        m = re.search(rf"^{key}:\s*(.*)$", head, re.M)
        return m.group(1).strip().strip('"') if m else ""

    nxt = re.search(r"^- \[ \] Next: (.*)$", body, re.M)
    wait = re.search(r"^- \[ \] Waiting: (.*)$", body, re.M)
    return {"status": fm("status"), "scheduled": fm("scheduled")[:10], "due": fm("due")[:10],
            "waiting": fm("waiting").lower() == "true",
            "next": nxt.group(1).strip() if nxt else "",
            "wait_line": wait.group(1).strip() if wait else "",
            "priority": fm("priority"), "time_est": fm("timeEstimate"),
            "description": _description(body), "steps": _steps(body), **_dod(body)}


def _dod(body: str) -> dict:
    """The Definition of Done (Nov 1, format from the DoD design session): the line
    "DoD: <text>" or "DoD (Claude's guess): <text>" inside the Next action block."""
    block = re.search(r"^## Next action\s*\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    m = re.search(r"^DoD( \(Claude's guess\))?: (.+)$", block.group(1), re.M) if block else None
    return ({"dod": re.sub(r"\s*\(the owner[^)]*\)\s*$", "", m.group(2)).strip(), "dod_guess": bool(m.group(1))}
            if m else {"dod": "", "dod_guess": False})


def _steps(body: str, limit: int = 8) -> list[str]:
    """The open steps after Next (Nov 1, the Start panel: "a short overview of the steps
    afterwards"): the note's unchecked boxes outside Current context and the Log."""
    body = re.sub(r"## Current context.*?%% end current context %%", "", body, flags=re.S)
    body = body.split("\n## Log", 1)[0]
    out = [re.sub(r"^Then:\s*", "", m.group(1).strip()) for m in re.finditer(r"^\s*- \[ \] (.+)$", body, re.M)
           if not re.match(r"(Next|Waiting):", m.group(1).strip())]
    return out[:limit]


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
            "steps": live.get("steps", []), "dod": live.get("dod", ""), "dod_guess": live.get("dod_guess", False)}


def _minutes(est: str) -> int | None:
    # A model can write an estimate as a bare number of minutes (update.py, Oct 31).
    est = f"{est}m" if isinstance(est, (int, float)) else (est or "")
    m = re.match(r"^\s*(?:(\d+)h)?\s*(\d+)?\s*m?\s*$", est)
    if not m or not (m.group(1) or m.group(2)):
        return None
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def home_tab(r: dict) -> str:
    """Each task has one home tab (owner decision, Oct 30). A drafted message and a next
    step of 5 min or less: Replies. Claude can act without a message: Claude.
    Everything else: Today."""
    mins = _minutes(r["est"])
    if r["draft"] and mins is not None and mins <= 5:
        return "replies"
    if r["claude"] and not r["draft"]:
        return "claude"
    return "today"


def _plan_today_mod(name: str):
    """A module from the plan-today skill folder, which the Mac panel runs from too."""
    import importlib
    import sys
    skill = str(config.VAULT / "_system" / "skills" / "plan-today")
    if skill not in sys.path:
        sys.path.insert(0, skill)
    return importlib.import_module(name)


def _dropped_mod():
    """plan-today's 14-day memory of dropped taskless rows (dropped.py), shared with the
    Mac panel so a row dropped in either place stays gone (owner decision, Oct 30: "wire it")."""
    return _plan_today_mod("dropped")


MED = "From today's meditation"
JOURNAL_DIR = config.VAULT / "Areas" / "Personal" / "Meditation Journal"


def with_meditation(data: dict, today: dt.date, entry: Path | None = None) -> dict:
    """Today's meditation, read live on every load, so one written during the day counts
    at once (owner decision, Oct 31). The pre-run leaves this group empty. plan-today's own matcher
    (build_panel.py) ties each of the "Two things" to its task, or makes it a row of its own,
    exactly as the Mac panel does."""
    entry = entry or JOURNAL_DIR / f"{today.isoformat()}.md"
    if not entry.exists():
        return data
    try:
        bp = _plan_today_mod("build_panel")
        if (data.get("journal") or {}).get("sig") == bp.journal_sig(entry.read_text(encoding="utf-8")):
            return data
        items = bp.journal_items(str(entry))
        if not items:
            return data
        groups = {g: list(rows) for g, rows in data.get("rows", [])}
        # The pre-run's taskless rows (a fraud alert straight from mail) are matched too: on
        # the Mac the meditation agent catches those, here nothing would (Oct 31).
        taskless = [{"title": r["n"], "closed": False, "closed_on": "",
                     "tv": {t for t, _, _ in bp._tokens(r["n"])},
                     "bv": {t for t, _, _ in bp._tokens(" ".join(str(r.get(k, "")) for k in ("s", "nx")))},
                     "proper": {t for t, pp, _ in bp._tokens(r["n"]) if pp}}
                    for rows in groups.values() for r in rows if r.get("n") and not r.get("lk")]
        rows, _ = bp.meditation_rows(items, groups, bp.open_tasks() + taskless)
        for r in rows:   # a match to a taskless row stays taskless, with that row's status and draft
            if r.get("lk") and not task_path(r["lk"]):
                r.pop("lk")
            r.pop("pend", None)
    except Exception as exc:   # the meditation is a layer on top; the view must never fail on it
        log_event("error", where="meditation", detail=str(exc)[:200])
        return data
    return {**data, "rows": [[MED, rows]] + [[g, groups[g]] for g, _ in data.get("rows", []) if g != MED]}


def build_view(data: dict, today: dt.date | None = None) -> dict:
    """Today (the one thing, at most TOP_N decisions, folds), Replies and Claude.

    The pre-run supplies the ranking, the why and the drafts; the task note
    supplies everything that can change during the day. Closed tasks and tasks
    moved past today drop out of today's lists."""
    today = today or dt.datetime.now(TZ).date()
    data = with_meditation(data, today)
    try:
        dmod = _dropped_mod(); gone = dmod.load()
    except Exception:   # the memory is a nicety; the view must never fail on it
        dmod, gone = None, []
    rows = []
    ignored = _ignored_props()
    for group, items in data.get("rows", []):
        for r in items:
            try:
                p = task_path(r.get("lk") or "")
                if r.get("lk") and not p:
                    continue
                drop_note = ""
                if dmod and gone and not r.get("lk") and "meditation" not in group.lower():
                    rec = dmod.check(r, gone)
                    if rec and rec.get("action") == "hide":
                        continue
                    if rec and rec.get("action") == "show":
                        drop_note = "You dropped this" + (f" on {_label(dt.date.fromisoformat(rec['on']))}"
                                                     if re.match(r"^\d{4}-\d{2}-\d{2}$", str(rec.get("on", ""))) else "") + "; new message since"
                live = read_note(p) if p else {}
                if live.get("status") in ("done", "cancelled"):
                    continue
                ch = [a for a in (r.get("ch") or []) if isinstance(a, dict)]
                draft = next((a["msg"] for a in ch if a.get("msg")), None)
                row = {
                    "id": r.get("lk") or r.get("n"),
                    "task": bool(p),
                    "title": r.get("n", ""),
                    "group": group,
                    "prio": r.get("p", "Z"),
                    "guess": r.get("g", ""),
                    "unsure": bool(r.get("lc")),
                    "next": live.get("next") or r.get("nx", ""),
                    "why": r.get("s", ""),
                    # An update's note (update.py "upd"; older overlays wrote it into m) is shown
                    # in Details and the summary on top, never in the card's tag line (Oct 31).
                    "meta": drop_note or ("" if str(r.get("m", "")).startswith("Update") else r.get("m", "")),
                    "update": r.get("upd") or (r.get("m", "") if str(r.get("m", "")).startswith("Update") else ""),
                    "waiting": live.get("wait_line") or r.get("wf", ""),
                    # A "(N min)" he set on the Next line wins over the pre-run's guess.
                    "est": (lambda m: f"{m.group(1)}m" if m else "")(re.search(r"\((\d+)\s*min\)\s*$", live.get("next", "")))
                           or (lambda x: f"{x}m" if isinstance(x, (int, float)) else x)(r.get("ne") or r.get("e")) or "",
                    "due": live.get("due") or "",
                    "anx": bool(r.get("anx")),
                    "urgent": bool(r.get("u")),
                    "claude": [a.get("t", "") for a in ch if not a.get("msg")],
                    # cp (Oct 30): steps Claude can take itself to move the task, even if he finishes it.
                    "prep": [a.get("t", "") for a in (r.get("cp") or []) if isinstance(a, dict) and a.get("t")][:3],
                # Rewrites the pre-run only proposed for his own step lines (Nov 1): one tap each.
                "prop": [{"old": x["old"], "new": x.get("new") or "", "why": str(x.get("why") or "")[:200]}
                         for x in (r.get("prop") if isinstance(r.get("prop"), list) else [])
                         if isinstance(x, dict) and isinstance(x.get("old"), str) and isinstance(x.get("new", ""), str)
                         and x["old"] not in ignored.get(r.get("lk") or r.get("n") or "", [])][:3],
                    # acct, needs_slot and the draft's time feed Send in Replies (Oct 31).
                    "draft": {**{k: draft.get(k, "") for k in ("via", "to", "subj", "body", "acct")},
                              "needs_slot": bool(draft.get("needs_slot") or SLOT.search(draft.get("body") or "")),
                              "at": data.get("updated") or data.get("generated", "")} if draft else None,
                    "later": bool(live.get("scheduled") and live["scheduled"] > today.isoformat()
                                  and not (live.get("due") and live["due"] <= today.isoformat())),
                    "detail": _detail(live, r.get("e", ""), r.get("ne", "")),
                }
                row["tab"] = home_tab(row)
                rows.append(row)
            except Exception as exc:   # one bad row hides itself; the rest of the day still shows (Nov 1)
                log_event("error", where="row", row=str(r.get("n") or r.get("lk") or "")[:120], detail=repr(exc)[:200])

    def rank(r):
        return (0 if r["anx"] else 1, 0 if r["urgent"] else 1,
                PRIO.get(r["prio"], 3), 0 if r["group"] == "Overdue" else 1)

    tomorrow = today + dt.timedelta(days=1)
    ov = load_overrides()
    hidden = set(ov["hidden"].get(today.isoformat(), [])) | set(ov["hidden"].get(tomorrow.isoformat(), []))
    rows = [r for r in rows if r["id"] not in hidden]
    # What his meditations said about each task in the last 30 days (owner decision, Oct 31: a worry
    # named again and again is eating mental space, so it ranks higher each time; priority
    # itself is only offered, after 3 mentions, never changed for him).
    try:
        from . import mentions as _mn
        mload, mcount = _mn.load(today), _mn.count(today)
    except Exception:   # the ledger is a layer on top; the view must never fail on it
        _mn, mload, mcount = None, {}, {}
    for r in rows:
        r["mind"], r["load"] = mcount.get(r["id"], 0), round(mload.get(r["id"], 0.0), 1)
        r["raise"] = bool(_mn and r["task"] and r["mind"] >= _mn.OFFER_AT and r["prio"] != "H"
                          and not raise_dismissed(r["id"], today))
    by_id = {r["id"]: r for r in rows}

    live_rows = [r for r in rows if not r["later"]]
    # A reply the pre-run or a Refresh judged answered, or now waiting on the other side,
    # leaves Replies (Nov 2: Refresh set it to done and it stayed); Plan's folds still have it.
    replies = sorted([r for r in live_rows if r["tab"] == "replies" and r["guess"] != "done"
                      and r["group"] != "Waiting"], key=rank)
    # ToDo is his day, picked by what matters rather than by which tab a task lives in
    # (owner decision, Oct 31): every task competes for it, Claude tasks included.
    # Replies and ToDo are not exclusive (owner decision, Oct 31): a task with a draft is listed in
    # Replies for sending and competes for ToDo like any other, by the same ranking.
    mine = list(live_rows)
    done = [r for r in mine if r["guess"] == "done"]
    waiting = [r for r in mine if r["group"] == "Waiting" and r not in done]

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
        return (0 if due else 1, 0 if heavy else 1, 0 if r["group"] == MED else 1,
                0 if r["guess"] == "today" else 1, -r.get("load", 0)) + rank(r)

    # A task he pulled up from below stays out of Claude's ranking and keeps his order after
    # Claude's picks (Oct 31: promoting one made it jump into the middle of the list).
    pulled_today = set(ov["pulled"].get(today.isoformat(), []))
    pool = [r for r in mine if r not in waiting + done and r["id"] not in pulled_today]
    fz = frozen_keys(pick, pool, "today", today)
    pool.sort(key=lambda r: fz[r["id"]])
    now, rest = pool[:TOP_N + 1], pool[TOP_N + 1:] + [r for r in mine if r["id"] in pulled_today]
    # Tasks dated today that the morning pre-run never saw (added since, e.g. via + Task).
    rest += [r for r in _safe(day_list, today, {}, where="day_list today", default=[]) if r["id"] not in by_id and r["id"] not in hidden]
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
    dated = [r for r in _safe(day_list, tomorrow, by_id, where="day_list tomorrow", default=[]) if r["id"] not in hidden]
    dated_ids = {r["id"] for r in dated}
    carry = [r for r in mine if r not in waiting + done and r["id"] not in dated_ids]

    def pick_tom(r):
        """Due tomorrow is hard; then heavy mental load; then what he dated for tomorrow; then
        today's meditation and the pre-run's today or soon call, as pick does for today."""
        heavy = r.get("load", 0) >= 3
        return (0 if r.get("due") and r["due"] <= tomorrow.isoformat() else 1, 0 if heavy else 1,
                0 if r["id"] in dated_ids else 1, 0 if r.get("group") == MED else 1,
                0 if r.get("guess") in ("today", "soon") else 1, -r.get("load", 0)) + rank(r)
    pulled_tom = set(ov["pulled"].get(tomorrow.isoformat(), []))
    tfz = frozen_keys(pick_tom, dated + carry, "tomorrow", tomorrow)
    tpool = [for_tom(r) for r in sorted(dated + carry, key=lambda r: tfz[r["id"]]) if r["id"] not in pulled_tom]
    tpool_pulled = [for_tom(r) for r in dated + carry if r["id"] in pulled_tom]
    nxt, nrest = tpool[:TOP_N + 1], tpool[TOP_N + 1:] + tpool_pulled
    nxt = _swap_one(nxt, nrest, ov["one"].get(tomorrow.isoformat()), ov["notone"].get(tomorrow.isoformat()))
    nrest = [r for r in nrest if not nxt or not nxt[0] or r["id"] != nxt[0]["id"]]
    tom_more = nxt[TOP_N + 1:] + nrest + [for_tom(r) for r in later if r["id"] not in dated_ids]
    tom_done, tom_wait = [for_tom(r) for r in done], [for_tom(r) for r in waiting if r["id"] not in dated_ids]
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
                  "closed": _safe(closed_recently, today, where="closed_recently", default=[]),
                  "selection": sel_rows_today, "budget": budget(today, sel_rows_today)},
        "tomorrow": {"date": tomorrow.isoformat(), "one": nxt[0] if nxt else None,
                     "needs": nxt[1:TOP_N + 1], "more": tom_more, "done": tom_done, "waiting": tom_wait, "closed": [],
                     "suggest": suggest[:8],
                     "selection": sel_rows_tom, "budget": budget(tomorrow, sel_rows_tom)},
        "prerun": prerun_state(),
        "replies": replies,
        "claude": claude,
        "counts": {"today": min(len(now), TOP_N + 1), "replies": len(replies), "claude": len(claude)},
        "notes": data.get("notes", [])[:3],
    }
    # Do (Nov 1): the day he executes from, which this ranking never reorders.
    view["do"] = _safe(do_view, view, ov, where="do", default=None)
    if view["do"]:
        view["counts"]["do"] = len(view["do"]["rows"])
    return view


def do_view(view: dict, ov: dict) -> dict:
    from . import do as dmod
    now = dt.datetime.now(TZ)
    day = dmod.do_day(now)
    by_id = {}
    for r in all_rows(view, with_do=False):
        by_id.setdefault(r["id"], r)

    def note(i):
        p = task_path(i)
        return read_note(p) if p else None

    def make_row(i):
        p = task_path(i)
        if not p:
            return None
        live = read_note(p)
        return {"id": i, "task": True, "title": i, "group": "Do", "prio": FM_PRIO.get(live.get("priority", ""), "Z"),
                "guess": "", "unsure": False, "next": live.get("next", ""), "why": "", "meta": "", "update": "",
                "waiting": live.get("wait_line", ""), "est": "", "due": live.get("due", ""), "anx": False,
                "urgent": False, "claude": [], "prep": [], "draft": None, "later": False, "tab": "today",
                "detail": _detail(live, "", "")}

    same = day == now.date()   # between midnight and 4:00 the ranking already shows the new date
    t = view["today"]
    top = (([t["one"]] if t["one"] else []) + t["needs"]) if same else []
    closed = t["closed"] if same else _safe(closed_recently, day, where="closed_recently", default=[])
    dated = _safe(day_list, day, by_id, where="do day_list", default=[])
    return dmod.view(day, by_id, closed, dated, top, day_events(day), note, make_row, _label,
                     ov["selected"].get(day.isoformat(), []))


FM_PRIO = {"4-high": "H", "3-normal": "N"}
PRIO_STEPS = ["1-none", "2-low", "3-normal", "4-high"]


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


# --- the day's time budget ------------------------------------------------------------
# owner decision, Oct 30: show how much estimated time is already planned against how much
# the day has. Every timed event is one of three kinds (owner decision, Nov 2: free time ignored his
# Week Structure, and Big rocks counted as busy although big-rock tasks go in there):
#   fixed: accepted events on the blocking calendars, and the standing structure on Focus
#          (morning routine, commute, lunch, dinner, email batch, recordings). Used time.
#   slot:  blocks that exist to hold tasks: ❗ Big rocks, Admin backup, the 🔒 focus mornings
#          (SCHEDULABLE BLOCKER). Time for tasks, shown apart from free.
#   free:  the rest of the window, including task blocks Claude booked on Focus (a `Task:`
#          line), whose time the planned side already counts, and invites not yet accepted.

DAY_START, DAY_END = 9 * 60, 21 * 60     # 09:00 to the 21:00 wind-down (Week Structure)
_BUSY_CACHE: dict[str, tuple[float, list]] = {}


# The day view shows his calendars merged, the ones ticked in his Calendar app (owner decision, Oct 31,
# with a screenshot of it: "whatever i am seeing here"): all but a class-wide events
# calendar and a scheduling overlay. Only the blocking calendars count as busy, so the budget is unchanged.
DAY_VIEW_SKIP: tuple[str, ...] = ()   # calendar ids to leave out of the day view (a class-wide events feed, a scheduling overlay)
SLOT_RE = re.compile(r"big rocks?|admin backup|^\W*🔒", re.I)
CAL_KIND = {"Focus": "focus", "Social": "social", "Luma": "social", "Partiful": "social",
            "<YOUR_PERSONAL_EMAIL>": "personal"}


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
        lo = dt.datetime.combine(day, dt.time(0), TZ).isoformat()
        hi = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), TZ).isoformat()
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
                info = {"cal": name if "@" not in name else ("Work" if "<WORK_MAIL_DOMAIN>" in name else "Personal"),
                        "loc": (e.get("location") or "")[:160],
                        "desc": re.sub(r"<[^>]+>", "", desc).strip()[:600]}
                raw = e.get("description") or ""
                slot = bool(SLOT_RE.search(title)) or raw.lstrip().startswith("SCHEDULABLE BLOCKER")
                if not st.get("dateTime"):
                    events.append({"s": 0, "f": 24 * 60, "title": title, "allday": True, "busy": False, "kind": kind, **info})
                    continue
                a = dt.datetime.fromisoformat(st["dateTime"]).astimezone(TZ)
                b = dt.datetime.fromisoformat(en.get("dateTime", st["dateTime"])).astimezone(TZ)
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
        log_event("error", where="calendar", detail=repr(exc)[:200])
        if key in _BUSY_CACHE and time.time() - _BUSY_CACHE[key][0] < 3600:
            return _BUSY_CACHE[key][1]
        return None


def _merge(spans) -> list[tuple[int, int]]:
    merged = []
    for s, f in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], f))
        else:
            merged.append((s, f))
    return merged


def busy_spans(day: dt.date) -> list[tuple[int, int]] | None:
    """Merged fixed minutes-of-day (the kinds above). None if unreadable."""
    events = day_events(day)
    if events is None:
        return None
    return _merge((e["s"], e["f"]) for e in events if e["busy"])


def slot_spans(day: dt.date, fixed: list[tuple[int, int]]) -> list[dict]:
    """The day's slots, minus what fixed time sits inside them, each with its title."""
    out = []
    for e in day_events(day) or []:
        if not e.get("slot") or e.get("allday"):
            continue
        parts = [(e["s"], e["f"])]
        for fs, ff in fixed:
            parts = [q for a, b in parts for q in ((a, min(b, fs)), (max(a, ff), b)) if q[1] > q[0]]
        t = "Focus block" if "🔒" in e["title"] else re.sub(r"^\W+", "", e["title"]).strip()
        out += [{"s": a, "f": b, "title": t} for a, b in parts]
    return out


def budget(day: dt.date, selection: list[dict]) -> dict:
    now = dt.datetime.now(TZ)
    start = max(DAY_START, now.hour * 60 + now.minute) if day == now.date() else DAY_START
    spans = busy_spans(day)
    free, slots = None, []
    if spans is not None:
        slots = slot_spans(day, spans)
        free = max(0, DAY_END - start)
        for s, f in _merge(spans + [(x["s"], x["f"]) for x in slots]):
            free -= max(0, min(f, DAY_END) - max(s, start))
        free = max(0, free)
    planned = sum(_minutes(r.get("est", "")) or 0 for r in selection)
    unestimated = sum(1 for r in selection if _minutes(r.get("est", "")) is None)
    # The page recomputes planned on every Add and free from these each minute, so it is live.
    return {"free_min": free, "planned_min": planned, "unestimated": unestimated,
            "window": f"{start // 60:02d}:{start % 60:02d}-21:00",
            "busy": spans or [], "slots": slots, "day_start": DAY_START, "day_end": DAY_END}


# --- per-day overrides: the one thing he swapped in, rows he removed ---------------

OVERRIDES = config.STATE_DIR / "checkin_app" / "overrides.json"
PICK_FROZEN = config.STATE_DIR / "checkin_app" / "pick_frozen.json"


def frozen_keys(key_fn, rows, tag: str, day: dt.date) -> dict:
    """Each row's rank key as first seen that day, so the list holds still (owner decision, Nov 1:
    a task jumped up the list just because he marked it as added; Add schedules
    the task, which changed its key). A task new to the day slots in by its own key."""
    try:
        d = json.loads(PICK_FROZEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    if not isinstance(d, dict):
        d = {}
    name = f"{tag}:{day.isoformat()}"
    m = d.get(name) if isinstance(d.get(name), dict) else {}
    out, changed = {}, False
    for r in rows:
        k = m.get(r["id"])
        if not isinstance(k, list):
            k, changed = list(key_fn(r)), True
            m[r["id"]] = k
        out[r["id"]] = tuple(k)
    if changed:
        old = (day - dt.timedelta(days=3)).isoformat()
        d = {n: v for n, v in d.items() if n.rsplit(":", 1)[-1] >= old}
        d[name] = m
        try:
            PICK_FROZEN.parent.mkdir(parents=True, exist_ok=True)
            tmp = PICK_FROZEN.with_suffix(".tmp")
            tmp.write_text(json.dumps(d), encoding="utf-8")
            os.replace(tmp, PICK_FROZEN)
        except OSError:
            pass
    return out


NORAISE = config.STATE_DIR / "checkin_app" / "noraise.json"   # "Not now" on Raise priority, per task


def raise_dismissed(task: str, today: dt.date) -> bool:
    try:
        on = json.loads(NORAISE.read_text()).get(task, "")
        return bool(on) and (today - dt.date.fromisoformat(on)).days < 7
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def dismiss_raise(task: str, today: dt.date) -> None:
    try:
        d = json.loads(NORAISE.read_text())
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    d[task] = today.isoformat()
    NORAISE.parent.mkdir(parents=True, exist_ok=True)
    NORAISE.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def load_overrides() -> dict:
    try:
        ov = json.loads(OVERRIDES.read_text())
    except (OSError, ValueError):
        ov = {}
    ov = ov if isinstance(ov, dict) else {}
    # one: a task name per day; the rest: a list of names per day. Anything else is dropped,
    # so a hand-edited or half-written file cannot take the view down (Nov 1).
    out = {}
    for kind in ("one", "hidden", "selected", "notone", "pulled"):
        days = ov.get(kind) if isinstance(ov.get(kind), dict) else {}
        if kind == "one":
            out[kind] = {d: v for d, v in days.items() if isinstance(v, str)}
        else:
            out[kind] = {d: [i for i in v if isinstance(i, str)] for d, v in days.items() if isinstance(v, list)}
    return out


def save_override(kind: str, day: dt.date, value: str) -> None:
    ov = load_overrides()
    keep = {(day - dt.timedelta(days=i)).isoformat() for i in range(-2, 7)}
    for k in ov:
        ov[k] = {d: v for d, v in ov[k].items() if d in keep}   # prune old days
    if kind == "one":
        ov["one"][day.isoformat()] = value
        ov["notone"][day.isoformat()] = [i for i in ov["notone"].get(day.isoformat(), []) if i != value]
    elif kind == "notone":
        if ov["one"].get(day.isoformat()) == value:
            ov["one"].pop(day.isoformat())
        ov["notone"].setdefault(day.isoformat(), [])
        if value not in ov["notone"][day.isoformat()]:
            ov["notone"][day.isoformat()].append(value)
    elif kind == "unselected":
        ov["selected"][day.isoformat()] = [i for i in ov["selected"].get(day.isoformat(), []) if i != value]
        ov["pulled"][day.isoformat()] = [i for i in ov["pulled"].get(day.isoformat(), []) if i != value]
    else:   # hidden | selected: a list per day
        ov[kind].setdefault(day.isoformat(), [])
        if value not in ov[kind][day.isoformat()]:
            ov[kind][day.isoformat()].append(value)
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERRIDES.with_suffix(".tmp")
    tmp.write_text(json.dumps(ov, ensure_ascii=False, indent=1))
    os.replace(tmp, OVERRIDES)


def day_list(day: dt.date, pre_rows: dict) -> list[dict]:
    """The Tomorrow side of the ToDo switch (owner decision, Oct 30): every open task
    scheduled or due that day, read live from Inbox/, so it works before the
    next pre-run. The pre-run's row (why, drafts, estimate) is reused where it has one."""
    from tools.todos import load_tasks
    iso, out = day.isoformat(), []
    for t in load_tasks():
        if t["status"] in ("done", "cancelled") or iso not in (t["scheduled"][:10], t["due"][:10]):
            continue
        live = read_note(Path(t["path"]))
        base = dict(pre_rows.get(t["name"]) or {
            "id": t["name"], "task": True, "title": t["name"], "group": "Scheduled",
            "guess": "", "unsure": False, "why": "", "meta": "", "anx": False,
            "claude": [], "draft": None})
        base.update({
            "prio": FM_PRIO.get(t["priority"], base.get("prio", "Z")),
            "urgent": t["urgent"] or base.get("urgent", False),
            "next": live["next"] or base.get("next", ""),
            "waiting": live["wait_line"] or base.get("waiting", ""),
            "est": base.get("est") or (f"{t['timeEstimate']}m" if t["timeEstimate"] else ""),
            "meta": base.get("meta") or ("Due " + _label(day) if t["due"][:10] == iso else "Scheduled " + _label(day)),
            "later": False, "tab": "today",
            "detail": _detail(live, (base.get("detail") or {}).get("task_est", ""), base.get("est", ""))})
        out.append(base)
    return sorted(out, key=lambda r: (0 if r["urgent"] else 1, PRIO.get(r["prio"], 3), r["title"].lower()))


def _safe(fn, *args, where: str, default):
    """A side list of the view (Done fold, tasks dated that day) that fails gives way to
    its default; the rest of the view still loads (Nov 1)."""
    try:
        return fn(*args)
    except Exception as exc:
        log_event("error", where=where, detail=repr(exc)[:200])
        return default


def all_rows(view: dict, with_do: bool = True) -> list[dict]:
    rows = []
    for side in ("today", "tomorrow"):
        t = view[side]
        rows += ([t["one"]] if t["one"] else []) + t["needs"] + t["more"] + t["done"] + \
            t["waiting"] + t.get("suggest", []) + t.get("closed", [])
    return rows + view["replies"] + view["claude"] + (((view.get("do") or {}).get("rows") or []) if with_do else [])


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
            out.append({"id": p.stem, "task": folder == config.INBOX, "title": p.stem, "group": "Done",
                        "prio": FM_PRIO.get(live.get("priority", ""), "Z"), "guess": "done",
                        "next": f"Done {days[cd.group(1)]}", "why": "", "meta": "", "waiting": "", "est": "",
                        "due": "", "anx": False, "urgent": False, "claude": [], "prep": [], "draft": None,
                        "later": False, "tab": "today", "closed": cd.group(1),
                        "detail": _detail(live, "", "") if live else {}})
    return sorted(out, key=lambda r: r["closed"], reverse=True)


def _fit_lines(day: dt.date, rows: list[dict]) -> list[str]:
    """The day's time budget for the plan, and when the selection is over it, the decisions it
    needs (owner decision, Nov 2: "if i go over budget (which is ok) then the plan step needs to take
    decisions / see how things fit ideally or would need to change to make it work")."""
    try:
        b = budget(day, rows)
    except Exception:
        return []
    if b["free_min"] is None:
        return []
    by = {}
    for x in b["slots"]:
        by[x["title"]] = by.get(x["title"], 0) + x["f"] - x["s"]
    room = b["free_min"] + sum(by.values())
    h = lambda m: f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m}m"
    out = [f"Time ({b['window']}): {h(b['planned_min'])} planned"
           + (f", {b['unestimated']} without an estimate" if b["unestimated"] else "")
           + f"; {h(b['free_min'])} free" + "".join(f" + {h(m)} in {t}" for t, m in by.items())
           + ". Fixed time (accepted events, the Week Structure routines on Focus) is already taken out;"
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


def plan_text(day: dt.date, rows: list[dict], ending: bool = True) -> str:
    lines = [f"🗓 Plan {day.strftime('%A')}, {_label(day)}",
             "Put these into the calendar with the schedule-work rules (Week Structure, "
             "working conditions, buffers). Propose the blocks first and wait for my OK:"]
    lines += [f"{i}. {r['title']}" + (f" ({r['est']})" if r.get("est") else "")
              + (f": next, {r['next']}" if r.get("next") else "") for i, r in enumerate(rows, 1)]
    lines += _fit_lines(day, rows)
    if ending:   # Telegram only: in the app's chat the tap itself is the go (Oct 31)
        lines.append("Reply \"go\" to have me plan it.")
    else:   # the app draws the proposal on his calendar above the chat (Oct 31)
        lines.append("Each time you propose or change blocks, end the message with a ```blocks fence, "
                     "one line per new block you would create (not what is already in the calendar): "
                     "HH:MM-HH:MM Title, the title a short name he recognises at a glance "
                     "(\"Batch: Delta code, CS 351Z quiz, haircut\"). The app draws it on his day and hides the fence.")
    return "\n".join(lines)


# --- the meditation journal in the app ---------------------------------------------
# owner decision, Oct 30: "move daily meditation to this app as well, it should still fill out
# obsidian page as it does right now - everything stays same, just interface changes".
# The questions are read from the template (the journal's only copy of them, per
# Meditation Journal.md), and the entry is written exactly as QuickAdd would.

JOURNAL = config.VAULT / "Areas" / "Personal" / "Meditation Journal"
TEMPLATE = JOURNAL / "_Daily Meditation Template.md"


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for every ## section, in order."""
    parts = re.split(r"^## (.+)$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def template_questions() -> list[dict]:
    out = []
    for head, body in _sections(TEMPLATE.read_text(encoding="utf-8")):
        checks = re.findall(r"^- \[[ x]\] (.+)$", body, re.M)
        kind = ("checks" if checks else "numbered" if re.search(r"^\d+\.\s*$", body, re.M)
                else "line" if "(one line)" in head else "list")
        out.append({"heading": head, "kind": kind, "items": checks,
                    "label": re.sub(r"\s*#\w+", "", head).strip()})
    return out


def _body_to_value(kind: str, body: str):
    lines = [l.rstrip() for l in body.strip().splitlines() if l.strip()]
    if kind == "checks":
        return {m.group(2): m.group(1) == "x" for m in (re.match(r"^- \[([ x])\] (.+)$", l) for l in lines) if m}
    if kind == "numbered":
        return "\n".join(re.sub(r"^\d+\.\s*", "", l) for l in lines if re.sub(r"^\d+\.\s*", "", l))
    if kind == "list":
        return "\n".join(re.sub(r"^[-*]\s*", "", l) for l in lines if re.sub(r"^[-*]\s*", "", l))
    return " ".join(lines)


def _value_to_body(q: dict, value) -> str:
    if q["kind"] == "checks":
        value = value if isinstance(value, dict) else {}
        return "\n" + "\n".join(f"- [{'x' if value.get(i) else ' '}] {i}" for i in q["items"]) + "\n"
    rows = [r.strip() for r in str(value or "").splitlines() if r.strip()]
    if q["kind"] == "numbered":
        rows = rows or ["", ""]
        return "\n" + "\n".join(f"{n}. {r}" for n, r in enumerate(rows, 1)) + "\n"
    if q["kind"] == "list":
        return "\n" + "\n".join(f"- {r}" for r in rows) + "\n" if rows else "\n\n"
    return ("\n" + " ".join(rows) + "\n") if rows else "\n\n"


def journal_entry(day: dt.date) -> dict:
    qs = template_questions()
    p = JOURNAL / f"{day.isoformat()}.md"
    have = dict(_sections(p.read_text(encoding="utf-8", errors="replace"))) if p.exists() else {}
    return {"date": day.isoformat(), "exists": p.exists(),
            "questions": [{**q, "value": _body_to_value(q["kind"], have.get(q["heading"], ""))} for q in qs]}


def save_journal(day: dt.date, values: dict, now: dt.datetime | None = None) -> Path:
    """Write the day's entry in the template's exact shape. An existing entry keeps its
    frontmatter, title and any section the template does not know; only the template's
    sections get his new answers."""
    qs = template_questions()
    p = JOURNAL / f"{day.isoformat()}.md"
    now = now or dt.datetime.now(TZ)
    bodies = {q["heading"]: _value_to_body(q, values.get(q["heading"])) for q in qs}
    if p.exists():
        text = p.read_text(encoding="utf-8")
        for head, new in bodies.items():
            pat = re.compile(rf"(^## {re.escape(head)}\s*$)(.*?)(?=^## |\Z)", re.M | re.S)
            if pat.search(text):
                text = pat.sub(lambda m: m.group(1) + new + "\n", text, count=1)
            else:
                text = text.rstrip() + f"\n\n## {head}\n{new}"
    else:
        title = f"{day.strftime('%A')}, {day.day} {day.strftime('%B %Y')}"
        text = (f"---\ntype: note\ncaptured: {now.strftime('%Y-%m-%dT%H:%M')}\n---\n\n# {title}\n\n" +
                "\n".join(f"## {q['heading']}\n{bodies[q['heading']]}" for q in qs))
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


# --- the meditation button ----------------------------------------------------------
# "From meditation" re-runs the plan-today pre-run now (stage 5's button, Oct 30):
# it reads the latest journal entry with mail, chats and calendar and rewrites
# the rows. One run at a time; its exit code says whether it worked.

RUN_DIR = config.STATE_DIR / "checkin_app"
UPDATE = RUN_DIR / "update.json"
RUN_PID, RUN_RC = RUN_DIR / "prerun.pid", RUN_DIR / "prerun.rc"


# Each background run, by a word in its command line, and how long it may take: the sum of
# its own timeouts plus slack. update.py: fetch 180 s + ask 600 s; the pre-run: ask 1200 s plus
# its fetches; prep: up to 3 tasks at 900 s each.
RUN_LIMITS = {"jobs.checkin_app.update": 20 * 60, "plan_today_prerun": 45 * 60, "jobs.checkin_app.prep": 60 * 60}


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _alive(pid: int, pidfile: Path | None = None) -> bool:
    # Runs are our children: reap a finished one, or it lingers as a zombie that
    # os.kill still reports alive and every later start is refused.
    if pid <= 0:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    # After a server restart the run is no longer our child, and its pid can be reused by
    # anything; only a process that is still one of our runs counts (Nov 1).
    cmd = _cmdline(pid)
    kind = next((k for k in RUN_LIMITS if k in cmd), None)
    if not kind:
        return False
    # A run past every timeout it has is hung: end it, so Refresh and Prepare work again.
    try:
        age = time.time() - pidfile.stat().st_mtime if pidfile else 0
    except OSError:
        age = 0
    if age > RUN_LIMITS[kind]:
        try:
            os.killpg(pid, signal_mod.SIGTERM)
        except OSError:
            pass
        log_event("error", where="run timeout", run=kind, minutes=round(age / 60))
        return False
    return True


def prerun_state() -> dict:
    try:
        pid = int(RUN_PID.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    rc = RUN_RC.read_text().strip() if RUN_RC.exists() else ""
    return {"running": bool(pid and _alive(pid, RUN_PID)), "last_ok": rc == "0" if rc else None}


def start_update() -> bool:
    """The quick update in the pre-run's slot: one at a time, same pid and exit-code files,
    so the page's polling and "Updating…" work unchanged."""
    import subprocess
    if prerun_state()["running"]:
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_RC.unlink(missing_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "update.log").open("a")
    py = home / ".venv" / "bin" / "python"
    proc = subprocess.Popen(
        ["sh", "-c", f'PYTHONPATH=. "{py}" -m jobs.checkin_app.update; echo $? > "{RUN_RC}"'],
        cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    RUN_PID.write_text(str(proc.pid))
    return True


def start_prerun() -> bool:
    import subprocess
    if prerun_state()["running"]:
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_RC.unlink(missing_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "prerun.log").open("w")
    proc = subprocess.Popen(
        ["sh", "-c", f'"{home}/bin/run-job" plan_today_prerun; echo $? > "{RUN_RC}"'],
        cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    RUN_PID.write_text(str(proc.pid))
    return True


# --- Claude prepares tasks (prep.py) ------------------------------------------------
PREP_PID, PREP_GEN = RUN_DIR / "prep.pid", RUN_DIR / "prep.generation"


def prep_running() -> bool:
    try:
        return _alive(int(PREP_PID.read_text().strip()), PREP_PID)
    except (OSError, ValueError):
        return False


def start_prep(task: str | None = None) -> bool:
    """One prep process at a time; a task-specific one waits for the next turn."""
    import subprocess
    if prep_running():
        return False
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    home = Path.home() / "automation"
    log = (RUN_DIR / "prep.log").open("a")
    cmd = [str(home / ".venv" / "bin" / "python"), "-m", "jobs.checkin_app.prep"] + (["--task", task] if task else [])
    proc = subprocess.Popen(cmd, cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PREP_PID.write_text(str(proc.pid))
    return True


def maybe_start_prep(generated: str) -> None:
    """Once per pre-run: prepare the next few tasks in the background."""
    try:
        from . import prep
        if generated and not (PREP_GEN.exists() and PREP_GEN.read_text().strip() == generated):
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            if start_prep():
                PREP_GEN.write_text(generated)
            return
        # An answer that arrived while Claude was busy is picked up as soon as it is free.
        if not prep_running() and any(isinstance(st := prep.load_state_file(f), dict) and st.get("pending")
                                      for f in prep.STATE.glob("*.json")):
            start_prep()
    except Exception as exc:   # background work; the view never waits on or fails for it
        log_event("error", where="maybe_start_prep", detail=repr(exc)[:200])


def attach_prep(view: dict) -> None:
    """Each card that Claude prepared carries done / asks / then for the Claude tab."""
    from . import prep
    t = view["today"]
    for r in view["claude"] + ([t["one"]] if t["one"] else []) + t["needs"] + t["more"]:
        st = prep.load_state(r["id"])
        if isinstance(st, dict) and st:
            r["prepared"] = {k: st.get(k) for k in ("result", "answers", "pending", "at", "error", "asked")}
    view["prep_running"] = prep_running()


# --- sending mail from Replies --------------------------------------------------------
# Send on a mail draft (owner decision, Oct 31: act without switching apps). plan-today's mailer.py
# finds the thread and builds the reply, the same code the Mac's two-minute page sends with.
# Only his tap in the sheet sends; test mode reads the thread and stops before the send.

SLOT = re.compile(r"\[slot\]", re.I)


def _mailer():
    return _plan_today_mod("mailer")


def _since(at: str) -> float:
    try:
        return dt.datetime.fromisoformat(at).timestamp()
    except ValueError:
        return 0.0


def mail_of(row: dict, edit: dict) -> tuple[dict, dict]:
    """The mail to send: the draft with his changes. -> (msg, edits), edits = {field: {draft, sent}}."""
    d = row.get("draft") or {}
    msg = {k: str(edit[k] if k in edit else d.get(k, "")) for k in ("to", "subj", "body")}
    msg["acct"] = str(edit.get("acct") or d.get("acct") or "").strip().lower()
    if edit.get("thread_id"):
        msg["thread_id"] = str(edit["thread_id"])
    if edit.get("new_thread"):
        msg["new_thread"] = True
    edits = {k: {"draft": d.get(k, ""), "sent": msg[k]} for k in ("to", "subj", "body")
             if k in edit and msg[k].strip() != (d.get(k) or "").strip()}
    return msg, edits


# --- the tap log ------------------------------------------------------------------
# Replaces the asked retros (the owner stopped answering them after Oct 15): every
# open and every tap, so the weekly summary can say how the check-in is used.

LOG = config.STATE_DIR / "checkin_app" / "taps.jsonl"


def signal(name: str, row: dict | None, day: str = "today", **extra) -> None:
    """One disagreement or agreement with Claude, with what Claude had suggested,
    so the weekly summary and the pre-run can learn from it (adopted Oct 30)."""
    claude = {}
    if row:
        claude = {k: row.get(k) for k in ("guess", "group", "prio", "unsure", "tab", "next", "est")}
    log_event("signal", signal=name, day=day, id=(row or {}).get("id", ""),
              title=(row or {}).get("title", ""), claude=claude, **extra)


TALK_PENDING = config.STATE_DIR / "checkin_app" / "talk_pending.json"


def _snapshot(p: Path) -> dict:
    s = read_note(p)
    m = re.search(r"^priority:\s*(\S+)", p.read_text(encoding="utf-8").partition("\n---\n")[0], re.M)
    s["priority"] = m.group(1) if m else ""
    return s


def remember_talk(row: dict) -> None:
    """Before a Talk: snapshot the note, so the change Claude makes in chat is logged."""
    p = task_path(row["id"]) if row.get("task") else None
    if not p:
        return
    try:
        pend = json.loads(TALK_PENDING.read_text())
    except (OSError, ValueError):
        pend = {}
    pend[row["id"]] = {"at": time.time(), "before": _snapshot(p), "title": row.get("title", "")}
    TALK_PENDING.parent.mkdir(parents=True, exist_ok=True)
    TALK_PENDING.write_text(json.dumps(pend, ensure_ascii=False))


def check_talk_deltas() -> None:
    """On every view: log what changed in a talked-about note, once, then forget it.
    Unchanged snapshots expire after 3 days."""
    try:
        pend = json.loads(TALK_PENDING.read_text())
    except (OSError, ValueError):
        return
    keep = {}
    for tid, e in pend.items():
        p = task_path(tid)
        after = _snapshot(p) if p else {"status": "missing"}
        delta = {k: [e["before"].get(k), after.get(k)] for k in after if e["before"].get(k) != after.get(k)}
        if delta:
            log_event("signal", signal="talk_delta", id=tid, title=e.get("title", ""), delta=delta,
                      minutes=round((time.time() - e["at"]) / 60))
        elif time.time() - e["at"] < 3 * 86400:
            keep[tid] = e
    if keep != pend:
        TALK_PENDING.write_text(json.dumps(keep, ensure_ascii=False))


# --- what became of the day (Oct 31) ---------------------------------------------------
# owner decision, Oct 31, "already do 1+2": the log knew what he picked in the morning, never whether it
# got done, so a good pick could not be told from one that sat all day. At 23:50 Pacific the
# server writes one `day_outcome` entry: every task Claude pre-selected or he selected, with what
# became of it, and the tasks he closed that were in neither. Days the server missed (down, or
# restarted past midnight) are written on the next start from the notes as they are then, marked
# `late`, so "moved" there means moved by now.

OUTCOMES = config.STATE_DIR / "checkin_app" / "outcomes.json"
OUTCOME_AT = dt.time(23, 50)


def _task_file(name: str) -> Path | None:
    for folder in (config.INBOX, config.VAULT / "Archive" / "Inbox"):
        p = folder / f"{name}.md"
        if "/" not in name and p.is_file():
            return p
    return None


def _fm_of(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    m = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    out = {}
    for ln in (m.group(1) if m else "").splitlines():
        k, sep, v = ln.partition(":")
        if sep and not ln.startswith((" ", "-")):
            out[k.strip()] = v.strip().strip("'\"")
    out["_body"] = text[m.end():] if m else text
    return out


def outcome_of(name: str, day: dt.date, hidden: set) -> dict:
    p = _task_file(name)
    if not p:   # a row with no task note (from mail or chats)
        return {"id": name, "task": False, "outcome": "hidden" if name in hidden else "unknown"}
    fm, iso = _fm_of(p), day.isoformat()
    done_on = fm.get("completedDate", "")[:10]
    status, sched = fm.get("status", ""), fm.get("scheduled", "")[:10]
    if status == "done":
        outcome = "done" if done_on == iso else "done_later" if done_on > iso else "done_before"
    elif status == "cancelled":
        outcome = "cancelled"
    elif sched and sched > iso:
        outcome = "moved"
    else:
        outcome = "open"
    # The day's Log lines verbatim, minus Claude's own preparation: what happened to it that day.
    lines = [ln[2:].strip() for ln in re.findall(rf"^- {re.escape(_label(day))} · .*$", fm["_body"], re.M)
             if "prepared by Claude" not in ln]
    est = fm.get("timeEstimate", "")
    return {"id": name, "task": True, "outcome": outcome, "log": lines[:8], "scheduled": sched,
            "completed": done_on, "priority": fm.get("priority", ""), "est": est}


def day_outcome(day: dt.date, late: bool = False) -> dict:
    iso = day.isoformat()
    ov = load_overrides()
    selected = list(ov["selected"].get(iso, []))
    hidden = set(ov["hidden"].get(iso, []))
    pre, opens = [], 0
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for ln in lines:   # one torn line (two processes append here) must not hide the rest
        try:
            e = json.loads(ln)
            if e.get("kind") == "open" and e.get("at", "")[:10] == iso:
                opens += 1
                pre = [i for i in (e.get("shown") or {}).get("today") or [] if isinstance(i, str)] or pre
        except (ValueError, AttributeError, TypeError):
            continue
    tasks = []
    for name in dict.fromkeys(pre + selected):
        r = outcome_of(name, day, hidden)
        r.update(pre_selected=name in pre, selected=name in selected, one=bool(pre) and name == pre[0])
        tasks.append(r)
    seen = {t["id"] for t in tasks}
    unplanned = []
    for folder in (config.INBOX, config.VAULT / "Archive" / "Inbox"):
        for p in folder.glob("*.md") if folder.exists() else []:
            try:
                head = p.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            if p.stem not in seen and re.search(rf"^completedDate:\s*['\"]?{iso}", head, re.M) \
                    and re.search(r"^type:\s*['\"]?action", head, re.M):
                unplanned.append(p.stem)
    count = lambda pred: sum(1 for t in tasks if pred(t))
    return {"day": iso, "late": late, "opens": opens, "tasks": tasks, "closed_unplanned": unplanned,
            "counts": {"selected": len(selected), "pre_selected": len(pre),
                       "selected_done": count(lambda t: t["selected"] and t["outcome"] == "done"),
                       "pre_selected_done": count(lambda t: t["pre_selected"] and t["outcome"] == "done"),
                       "open": count(lambda t: t["outcome"] == "open"), "moved": count(lambda t: t["outcome"] == "moved"),
                       "closed_unplanned": len(unplanned)}}


def record_outcomes(now: dt.datetime | None = None, catch_up: int = 3) -> list[str]:
    """Write today's at 23:50, and any of the last few days that were missed. Returns the days written."""
    now = now or dt.datetime.now(TZ)
    try:
        done = json.loads(OUTCOMES.read_text())
    except (OSError, ValueError):
        done = {}
    done = done if isinstance(done, dict) else {}
    wrote = []
    days = [(now.date() - dt.timedelta(days=i), True) for i in range(catch_up, 0, -1)]
    if now.time() >= OUTCOME_AT:
        days.append((now.date(), False))
    ov = load_overrides()
    for day, late in days:
        iso = day.isoformat()
        if iso in done:
            continue
        # A missed day counts only if the app was used: something selected, or the day opened.
        out = day_outcome(day, late=late)
        if late and not (ov["selected"].get(iso) or out["opens"]):
            done[iso] = "skipped: app not used"
            continue
        log_event("day_outcome", **out)
        done[iso] = now.isoformat(timespec="seconds")
        wrote.append(iso)
    keep = {(now.date() - dt.timedelta(days=i)).isoformat() for i in range(30)}
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    OUTCOMES.write_text(json.dumps({d: v for d, v in done.items() if d in keep}, indent=1))
    return wrote


def log_event(kind: str, **fields) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                                "kind": kind, **fields}, ensure_ascii=False) + "\n")
    except OSError:
        pass   # the log must never break a tap


# --- writing a task ----------------------------------------------------------------

def _label(d: dt.date) -> str:
    return d.strftime("%b ") + str(d.day)


def _set_fm(head: str, key: str, value: str) -> str:
    pat = re.compile(rf"^{key}:.*$", re.M)
    return pat.sub(lambda _: f"{key}: {value}", head, count=1) if pat.search(head) else f"{head}\n{key}: {value}"


def _write(p: Path, text: str) -> None:
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


# --- the DoD and his step lines (Nov 1, SYSTEM.md § Capture note schema, "A task states its
# Definition of Done"). The DoD is the first line of ## Next action; a step line carrying
# "(the owner" is his direction, so everything written here from his taps carries it.

DOD_RE = re.compile(r"^DoD( \(Claude's guess\))?: (.+)$", re.M)
STEP_OPEN = re.compile(r"^(\s*)- \[ \] (?:(Next|Then|Waiting): )?(.*)$")
HIS_TAG = re.compile(r"\s*\(the owner[^)]*\)\s*$")
PROP_IGNORED = config.STATE_DIR / "checkin_app" / "prop_ignored.json"


def _norm_step(t: str) -> str:
    return " ".join(HIS_TAG.sub("", t or "").lower().split())


def _next_block(text: str) -> tuple[int, int]:
    m = re.search(r"^## Next action[ \t]*\n", text, re.M)
    if not m:
        raise ValueError("this task has no Next action block")
    nxt = re.search(r"^## ", text[m.end():], re.M)
    return m.end(), m.end() + (nxt.start() if nxt else len(text) - m.end())


def _with_log(p: Path, text: str, line: str) -> None:
    head, sep, body = text.partition("\n---\n")
    _write(p, f"{head}{sep}{_log(body, line)}" if sep else _log(text, line))


def write_dod(p: Path, dod: str, confirmed: bool, day: dt.date, src: str = "check-in app") -> None:
    """Set the DoD line: confirmed carries his attribution, a guess is marked as Claude's."""
    dod = _one_line(dod).strip()
    if not dod:
        raise ValueError("empty Definition of Done")
    line = f"DoD: {HIS_TAG.sub('', dod)} (the owner, {_label(day)})" if confirmed else f"DoD (Claude's guess): {dod}"
    text = p.read_text(encoding="utf-8")
    a, b = _next_block(text)
    block = text[a:b]
    block = DOD_RE.sub(lambda _: line, block, count=1) if DOD_RE.search(block) else line + "\n" + block
    _with_log(p, text[:a] + block + text[b:],
              f"- {_label(day)} · DoD {'set' if confirmed else 'proposed'}: {HIS_TAG.sub('', dod)} · {src}")


def apply_prop(p: Path, old: str, new: str, day: dt.date) -> None:
    """He accepted a rewrite the pre-run only proposed for one of his lines: the new line
    carries his attribution, the old one stays, struck, as the pre-run does it."""
    kind, _, old_text = old.partition(": ")
    text = p.read_text(encoding="utf-8")
    a, b = _next_block(text)
    lines = text[a:b].split("\n")
    for i, ln in enumerate(lines):
        m = STEP_OPEN.match(ln)
        if m and (m.group(2) or "") == kind and _norm_step(m.group(3)) == _norm_step(old_text):
            ind = m.group(1)
            new_kind, _, new_text = new.partition(": ") if new else ("", "", "")
            lines[i:i + 1] = ([f"{ind}- [ ] {new_kind or kind}: {HIS_TAG.sub('', new_text)} (the owner, {_label(day)})"] if new else []) + \
                [f"{ind}- ~~{kind}: {m.group(3)}~~ ({'replaced' if new else 'struck'}, accepted in the check-in app, {_label(day)})"]
            _with_log(p, text[:a] + "\n".join(lines) + text[b:],
                      f"- {_label(day)} · accepted Claude's proposal: {new or 'struck ' + old} · check-in app")
            return
    raise ValueError("that step is no longer in the note")


def save_steps(p: Path, steps: list[str], day: dt.date) -> None:
    """His edit of the planned steps (Next, then the rest): unchanged lines stay as they were,
    changed and new ones carry his attribution, removed ones stay struck."""
    steps = [_one_line(x) for x in steps if _one_line(x)][:12]
    if not steps:
        raise ValueError("at least a next step is needed")
    text = p.read_text(encoding="utf-8")
    a, b = _next_block(text)
    lines = text[a:b].split("\n")
    old = [(i, STEP_OPEN.match(ln)) for i, ln in enumerate(lines)]
    old = [(i, m) for i, m in old if m and m.group(2) != "Waiting"]
    by_norm = {_norm_step(m.group(3)): m for _, m in old}
    new_lines = []
    for n, st in enumerate(steps):
        m = by_norm.pop(_norm_step(st), None)
        body = m.group(3) if m else f"{HIS_TAG.sub('', st)} (the owner, {_label(day)})"
        prefix = "Next: " if n == 0 else ("Then: " if m and m.group(2) == "Then" else "")
        new_lines.append(f"- [ ] {prefix}{body}")
    struck = [f"- ~~{m.group(3)}~~ (removed in the check-in app, {_label(day)})" for m in by_norm.values()]
    at = old[0][0] if old else next((i + 1 for i, ln in enumerate(lines) if DOD_RE.match(ln)), 0)
    keep = [ln for i, ln in enumerate(lines) if i not in {j for j, _ in old}]
    at = min(at, len(keep)) - sum(1 for j, _ in old if j < at)
    keep[max(at, 0):max(at, 0)] = new_lines + struck
    _with_log(p, text[:a] + "\n".join(keep) + text[b:], f"- {_label(day)} · steps edited · check-in app")


def _ignored_props() -> dict:
    try:
        d = json.loads(PROP_IGNORED.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _log(body: str, line: str) -> str:
    body = body.rstrip()
    if re.search(r"^## Log\s*$", body, re.M):
        return f"{body}\n{line}\n"
    return f"{body}\n\n## Log\n{line}\n"


def _one_line(s: str) -> str:
    return " ".join((s or "").split())


def apply(p: Path, action: str, today: dt.date | None = None, text_in: str = "",
          dry: bool = False, base: dt.date | None = None, why: str = "",
          fields: dict | None = None) -> str:
    """done | tomorrow | next | note. Same schema the vt tasks hook enforces:
    completedDate on done, the open Next ticked, and a Log line for today.
    base: the day the ToDo switch shows; "tomorrow" means the day after it.
    dry: compute the message, write nothing (the app's test mode)."""
    today = today or dt.datetime.now(TZ).date()
    base = base or today
    text = p.read_text(encoding="utf-8")
    # Without a frontmatter block the partition below finds none (or a rule in the body), and
    # status: done would land in the text. Refuse instead of guessing (Nov 1).
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError("this note has no frontmatter, fix it in Obsidian")
    head, sep, body = text.partition("\n---\n")
    note = _one_line(text_in)
    if action in ("next", "note") and not note:
        raise ValueError("empty text")
    if action == "next":
        if re.search(r"^- \[ \] Next: .*$", body, re.M):
            body = re.sub(r"^- \[ \] Next: .*$", lambda m: f"- [ ] Next: {note}", body,
                          count=1, flags=re.M)
        else:
            body = f"\n## Next action\n- [ ] Next: {note}\n" + body
        body = _log(body, f"- {_label(today)} · Next set to: {note} · check-in app")
        msg = "Next updated"
    elif action == "note":
        # Verbatim, in his words: the next pre-run and any session read the Log.
        body = _log(body, f"- {_label(today)} · the owner: \"{note}\" · check-in app")
        msg = "Note saved"
    elif action == "done":
        head = _set_fm(_set_fm(head, "status", "done"), "completedDate", today.isoformat())
        body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
        body = _log(body, f"- {_label(today)} · done · check-in app")
        msg = "Done"
    elif action == "sent":
        # Send in Replies (Oct 31): closes the task as Replied does; the log names who got it.
        head = _set_fm(_set_fm(head, "status", "done"), "completedDate", today.isoformat())
        body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
        body = _log(body, f"- {_label(today)} · sent mail to {note} · check-in app")
        msg = f"Sent to {note}"
    elif action == "update":
        # The Edit task form (Oct 30): only the fields he changed, each logged old → new.
        f = fields or {}
        before = read_note(p)
        changes = []
        # Status in the form too (owner decision, Oct 31). done and cancelled close the task the way
        # Done and Shouldn't exist do; reopening clears completedDate.
        st = f.get("status")
        if st in ("inbox", "open", "in-progress", "done", "cancelled") and st != before.get("status"):
            head = _set_fm(head, "status", st)
            if st in ("done", "cancelled"):
                head = _set_fm(head, "completedDate", today.isoformat())
                if st == "done":
                    body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
            else:
                head = "\n".join(l for l in head.split("\n") if not l.startswith("completedDate:"))
            changes.append(("status", before.get("status") or "", st))
        if f.get("priority") in PRIO_STEPS and f["priority"] != before["priority"]:
            head = _set_fm(head, "priority", f["priority"])
            changes.append(("priority", before["priority"], f["priority"]))
        for key in ("due", "scheduled"):
            if key not in f or f[key] == before[key]:
                continue
            val = str(f[key])[:10]
            if val == "":
                head = "\n".join(l for l in head.split("\n") if not l.startswith(f"{key}:"))
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                head = _set_fm(head, key, val)
            else:
                continue
            changes.append((key, before[key], val))
        if "time_est" in f and str(f["time_est"]).strip() != str(before["time_est"]):
            v = str(f["time_est"]).strip()
            if v.isdigit():
                head = _set_fm(head, "timeEstimate", v)
                changes.append(("whole task", before["time_est"], v + " min"))
        if "next" in f or "next_min" in f:
            old = before["next"]
            base_next = _one_line(f.get("next", old)) or old
            base_next = re.sub(r"\s*\(\d+\s*min\)\s*$", "", base_next)
            mins = str(f.get("next_min", "")).strip()
            if not mins:
                m = re.search(r"\((\d+)\s*min\)\s*$", old)
                mins = m.group(1) if m else ""
            new = base_next + (f" ({mins} min)" if mins.isdigit() else "")
            if new and new != old:
                if re.search(r"^- \[ \] Next: .*$", body, re.M):
                    body = re.sub(r"^- \[ \] Next: .*$", lambda m: f"- [ ] Next: {new}", body, count=1, flags=re.M)
                else:
                    body = f"\n## Next action\n- [ ] Next: {new}\n" + body
                changes.append(("next", old, new))
        if not changes:
            return "Nothing changed"
        def show(v):
            return _label(dt.date.fromisoformat(v)) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(v or "")) else v
        body = _log(body, f"- {_label(today)} · edited: " +
                    "; ".join(f"{k} {show(o) or '–'} → {show(n) or 'cleared'}" for k, o, n in changes) + " · check-in app")
        msg = "Saved"
        if fields is not None:
            fields["_changes"] = changes
    elif action == "lower":
        m = re.search(r"^priority:\s*(\S+)", head, re.M)
        cur = m.group(1) if m else "3-normal"
        new = PRIO_STEPS[max(0, PRIO_STEPS.index(cur) - 1)] if cur in PRIO_STEPS else "2-low"
        head = _set_fm(head, "priority", new)
        head = "\n".join(l for l in head.split("\n") if not l.startswith("scheduled:"))
        body = _log(body, f"- {_label(today)} · removed from the selection: not important, priority {cur} → {new}, date cleared · check-in app")
        msg = f"Removed, priority now {new}"
    elif action == "cancel":
        head = _set_fm(head, "status", "cancelled")
        body = _log(body, f"- {_label(today)} · dropped: shouldn't exist · check-in app")
        msg = "Dropped"
    elif action == "schedule":
        head = _set_fm(head, "scheduled", base.isoformat())
        body = _log(body, f"- {_label(today)} · scheduled for {_label(base)} · check-in app")
        msg = f"Added to {_label(base)}"
    elif action == "tomorrow":
        tmrw = base + dt.timedelta(days=1)
        head = _set_fm(head, "scheduled", tmrw.isoformat())
        body = _log(body, f"- {_label(today)} · moved to {_label(tmrw)} · check-in app")
        msg = f"Moved to {_label(tmrw)}"
    else:
        raise ValueError(action)
    if why:   # his optional reason, verbatim (Oct 30)
        body = _log(body, f"- {_label(today)} · the owner, why: \"{_one_line(why)}\" · check-in app")
    if dry:
        return f"Test mode, nothing written: {msg[0].lower()}{msg[1:]}"
    _write(p, f"{head}{sep}{body}")
    return msg


def talk_text(row: dict, note: str = "", ending: bool = True) -> str:
    parts = [f"💬 {row['title']}"]
    if note:
        parts.append(f"Your note: {note}")
    if row.get("next"):
        parts.append(f"Next: {row['next']}")
    if row.get("waiting"):
        parts.append(f"Waiting for: {row['waiting']}")
    if row.get("why"):
        parts.append(row["why"])
    if row.get("claude"):
        parts.append("Claude could: " + "; ".join(row["claude"]))
    if row.get("task"):
        parts.append(f"Task note: Inbox/{row['id']}.md")
    if ending:   # Telegram only; the app's chat sends this as context under his own message
        parts.append("Reply \"go\" to have me apply your note to the task, or reply to talk it through."
                     if note else "Reply to this message to talk it through, or tell me to do it.")
    return "\n\n".join(parts)


# --- the server ----------------------------------------------------------------------

def make_app():
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    env = config.load_env()
    token, owner = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_OWNER_ID", "")
    password, secret = env.get("CHECKIN_PASSWORD", ""), env.get("CHECKIN_SECRET", "")
    lockout = Lockout()

    def authed(req: Request) -> bool:
        return (verify_init_data(req.headers.get("x-init-data", ""), token, owner)
                or verify_cookie(req.cookies.get(COOKIE, ""), secret))

    async def login(req: Request):
        if req.method == "GET":
            return HTMLResponse(LOGIN_HTML.replace("{msg}", ""))
        if lockout.locked():
            return HTMLResponse(LOGIN_HTML.replace("{msg}", "<p>Too many tries, wait 10 minutes.</p>"),
                                status_code=429)
        form = await req.form()
        given = str(form.get("password", ""))
        if not password or not secret or not hmac.compare_digest(given.encode(), password.encode()):
            lockout.fail()
            return HTMLResponse(LOGIN_HTML.replace("{msg}", "<p>Wrong password.</p>"), status_code=403)
        resp = RedirectResponse("./", status_code=303)
        resp.set_cookie(COOKIE, make_cookie(secret), max_age=COOKIE_AGE, path="/",
                        secure=True, httponly=True, samesite="lax")
        return resp

    def current_view() -> dict:
        try:
            return build_view(load_prerun())
        except Exception as exc:   # a bad quick update must never take the app down (Oct 31)
            log_event("error", where="update overlay", detail=str(exc)[:200])
        try:
            return build_view(load_prerun(overlay=False))
        except Exception as exc:   # nor a bad pre-run: an empty day that says so (Nov 1)
            log_event("error", where="bare pre-run", detail=str(exc)[:200])
        try:
            v = build_view({"rows": [], "generated": "", "notes": []})
        except Exception as exc:
            log_event("error", where="empty view", detail=str(exc)[:200])
            day = {"one": None, "needs": [], "more": [], "done": [], "waiting": [], "closed": [],
                   "selection": [], "suggest": [], "budget": {"free_min": None, "planned_min": 0, "unestimated": 0,
                                                              "window": "", "busy": [], "day_start": DAY_START, "day_end": DAY_END}}
            v = {"generated": "", "updated": "", "update_summary": [], "today": dict(day),
                 "tomorrow": {**day, "date": (dt.datetime.now(TZ).date() + dt.timedelta(days=1)).isoformat()},
                 "prerun": {"running": False, "last_ok": None}, "replies": [], "claude": [],
                 "counts": {"today": 0, "replies": 0, "claude": 0}, "notes": []}
        v["notes"] = ["The morning list could not be read, so only what is safe shows. It is logged; a Refresh or the next pre-run fixes it."] + v["notes"]
        return v

    def find(row_id: str) -> dict | None:
        return next((r for r in all_rows(current_view()) if r["id"] == row_id), None)

    def source(req: Request) -> str:
        return "telegram" if req.headers.get("x-init-data") else "browser"

    STATIC = {"manifest.webmanifest": "application/manifest+json",
              "icon-180.png": "image/png", "icon-192.png": "image/png", "icon-512.png": "image/png"}

    async def static(req: Request):
        # The installable-app bits (Chrome Install, Safari Add to Dock). No data, no auth.
        name = req.url.path.rsplit("/", 1)[-1]
        return Response((HTML.parent / name).read_bytes(), media_type=STATIC[name],
                        headers={"Cache-Control": "max-age=86400"})

    async def page(req: Request):
        return HTMLResponse(HTML.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store"})

    async def view(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        await asyncio.to_thread(check_talk_deltas)
        v = await asyncio.to_thread(current_view)
        maybe_start_prep(v.get("generated", ""))
        try:
            attach_prep(v)
        except Exception as exc:
            log_event("error", where="attach_prep", detail=str(exc)[:200])
        try:   # which cards already have a chat in the app: their Talk shows it
            from .chat import summaries
            v["chats"] = summaries()
        except Exception as exc:
            log_event("error", where="chat summaries", detail=str(exc)[:200])
        if req.query_params.get("open"):   # a real open, not an auto-refresh
            # What Claude pre-selected, so rows shown but never touched can be found later.
            shown = {side: [r["id"] for r in ([v[side]["one"]] if v[side]["one"] else []) + v[side]["needs"]]
                     for side in ("today", "tomorrow")}
            log_event("open", src=source(req), counts=v["counts"], shown=shown,
                      replies=[r["id"] for r in v["replies"]], claude=[r["id"] for r in v["claude"]])
        return JSONResponse(v)

    async def act(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        try:
            # Off the event loop (Nov 1: a slow calendar read inside a tap froze the chat's polling
            # too), and one tap at a time, as before, since taps read, change and write notes.
            return await asyncio.to_thread(_act_locked, req, body)
        except ValueError as exc:   # apply() refusing: say why, write nothing
            return JSONResponse({"error": str(exc)[:200] or "could not do that"}, status_code=400)
        except Exception as exc:   # a tap never ends in a bare 500; the log has what broke
            log_event("error", where=f"act {body.get('action')}", detail=repr(exc)[:300])
            return JSONResponse({"error": "something broke, it is logged; refresh and try again"}, status_code=500)

    write_lock = threading.Lock()

    def _act_locked(req: Request, body: dict):
        with write_lock:
            return _act(req, body)

    def _act(req: Request, body: dict):
        action, row_id = body.get("action"), body.get("id", "")
        action = action if isinstance(action, str) else ""
        row_id = row_id if isinstance(row_id, str) else str(row_id)
        text_in = str(body.get("text", ""))[:2000]
        dry = bool(body.get("test"))
        log_event("tap", action=action, id=row_id, test=dry, src=source(req),
                  tab=body.get("tab", ""), edited=bool(text_in))
        if action == "add":
            from tools.todos import add_task
            title = _one_line(text_in)
            if not title:
                return JSONResponse({"error": "empty task"}, status_code=400)
            if dry:
                return JSONResponse({"ok": True, "msg": f"Test mode, nothing written: would add \"{title}\""})
            # Added from the pre-selection (Oct 30): dated for that day and in his selection.
            add_day = dt.datetime.now(TZ).date() + dt.timedelta(days=1 if body.get("day") == "tomorrow" else 0)
            path = add_task(title, False, None, add_day.isoformat(), "", None)
            save_override("selected", add_day, Path(path).stem)
            signal("added", None, body.get("day", "today"), source="manual", title_added=title)
            return JSONResponse({"ok": True, "msg": f"Added for {_label(add_day)}"})
        day_name = "tomorrow" if body.get("day") == "tomorrow" else "today"
        base_day = dt.datetime.now(TZ).date() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
        tab_name = body.get("tab", "")
        if action in ("dod_confirm", "dod_set", "prop_accept", "prop_ignore", "steps_save"):
            p = task_path(row_id)
            if not p:
                return JSONResponse({"error": "no task note"}, status_code=400)
            today_d = dt.datetime.now(TZ).date()
            # The DoD as it was, so the log shows what Claude guessed against what he kept (Nov 1).
            was = read_note(p)
            signal({"dod_confirm": "dod_confirmed", "dod_set": "dod_set", "prop_accept": "prop_accepted",
                    "prop_ignore": "prop_ignored", "steps_save": "steps_edited"}[action],
                   {"id": row_id, "title": row_id}, "today", test=dry, text=text_in[:400],
                   old=str(body.get("old", ""))[:300], new=str(body.get("new", ""))[:300],
                   dod_before=was.get("dod", ""), dod_before_guess=was.get("dod_guess", False))
            if dry:
                return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
            if action == "dod_confirm":
                live = read_note(p)
                if not live.get("dod"):
                    return JSONResponse({"error": "no Definition of Done to confirm"}, status_code=400)
                write_dod(p, live["dod"], True, today_d)
                return JSONResponse({"ok": True, "keep": True, "msg": "Definition of Done confirmed"})
            if action == "dod_set":
                write_dod(p, text_in, True, today_d)
                return JSONResponse({"ok": True, "keep": True, "msg": "Definition of Done saved"})
            if action == "prop_accept":
                apply_prop(p, str(body.get("old", "")), str(body.get("new", "")), today_d)
                return JSONResponse({"ok": True, "keep": True, "msg": "Step updated"})
            if action == "prop_ignore":
                d = _ignored_props()
                d.setdefault(row_id, []).append(str(body.get("old", ""))[:300])
                PROP_IGNORED.parent.mkdir(parents=True, exist_ok=True)
                PROP_IGNORED.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
                return JSONResponse({"ok": True, "keep": True, "msg": "Kept your line"})
            steps = body.get("steps") if isinstance(body.get("steps"), list) else []
            save_steps(p, [str(x) for x in steps], today_d)
            return JSONResponse({"ok": True, "keep": True, "msg": "Steps saved"})
        if action.startswith("do_"):
            # Do (Nov 1): his taps on the day's list. The list changes only here.
            from . import do as dmod
            dday = dmod.do_day(dt.datetime.now(TZ))
            if action == "do_commit":   # Create or Update plan in Plan, for that day
                ids = [str(i) for i in body.get("ids")] if isinstance(body.get("ids"), list) else []
                log_event("do_commit", day=base_day.isoformat(), n=len(ids), test=dry)
                if not dry:
                    dmod.commit(base_day, ids)
                return JSONResponse({"ok": True, "keep": True, "msg": ""})
            if action not in ("do_add", "do_drop", "do_keep", "do_dismiss", "do_unmove") or not row_id:
                return JSONResponse({"error": "bad request"}, status_code=400)
            signal({"do_add": "do_added", "do_drop": "do_dropped", "do_keep": "do_kept",
                    "do_dismiss": "do_suggestion_dismissed", "do_unmove": "do_move_undone"}[action],
                   {"id": row_id, "title": row_id}, "today", test=dry, source=str(body.get("from", ""))[:20])
            if dry:
                return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
            p = task_path(row_id)
            if action == "do_add":
                dmod.add(dday, row_id)
                if p and read_note(p)["scheduled"] != dday.isoformat():
                    apply(p, "schedule", base=dday)
                return JSONResponse({"ok": True, "keep": True, "msg": "Added to today"})
            if action == "do_unmove":
                if p:
                    apply(p, "schedule", base=dday)
                dmod.add(dday, row_id)
                return JSONResponse({"ok": True, "keep": True, "msg": "Back on today"})
            getattr(dmod, action[3:])(dday, row_id)
            return JSONResponse({"ok": True, "keep": True,
                                 "msg": {"do_drop": "Taken off today", "do_keep": "Kept", "do_dismiss": "Not today"}[action]})
        if action == "confirm":
            # He confirms Claude's pre-selection before planning (signal 1).
            v = current_view()
            pre = [r["id"] for r in ([v[day_name]["one"]] if v[day_name]["one"] else []) + v[day_name]["needs"]]
            ids = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else [])]
            signal("selection_confirmed", None, day_name, test=dry, ids=ids,
                   pre_selected=pre, kept=[i for i in ids if i in pre])
            return JSONResponse({"ok": True, "keep": True, "msg": "Selection confirmed"})
        if action == "journal_save":
            jday = dt.datetime.now(TZ).date()
            values = body.get("values") if isinstance(body.get("values"), dict) else {}
            filled = sum(1 for v in values.values() if (v if not isinstance(v, dict) else any(v.values())))
            log_event("journal", test=dry, filled=filled, update=bool(body.get("update")))
            if dry:
                return JSONResponse({"ok": True, "msg": "Test mode, nothing written: would save today's meditation"})
            save_journal(jday, values)
            started = start_update() if body.get("update") else False
            return JSONResponse({"ok": True, "running": started,
                                 "msg": "Meditation saved" + (", updating ToDo from it" if started else "")})
        if action == "meditation":
            # Refresh (Oct 31) runs in test mode too: the pre-run is the same one that runs
            # every morning, and test mode is about his taps, not about fresh data.
            started = start_update()
            return JSONResponse({"ok": True, "running": True,
                                 "msg": "Updating from new messages and your meditation, about a minute" if started
                                 else "Already updating"})
        if action == "plan":
            v = current_view()
            ids = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else [])][:TOP_N + 1]
            rows = [r for i in ids for r in all_rows(v) if r["id"] == i][:len(ids)]
            seen, picked = set(), []
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"]); picked.append(r)
            if not picked:
                return JSONResponse({"error": "nothing selected to plan"}, status_code=400)
            if dry:
                return JSONResponse({"ok": True, "keep": True,
                                     "msg": f"Test mode, nothing sent: would plan {len(picked)} items"})
            notify.send_telegram(plan_text(base_day, picked), silent=False)
            return JSONResponse({"ok": True, "msg": "Sent to Claude in Telegram, reply go", "close": True})
        row = find(row_id)
        if not row:
            return JSONResponse({"error": "row not found, refresh"}, status_code=404)
        why = _one_line(str(body.get("why", "")))[:500]

        # Client-only moments that still say something about Claude's suggestion.
        if action == "mark":
            name = str(body.get("signal", ""))[:40]
            if name in ("claude_adjust", "edit_opened"):
                signal(name, row, day_name, test=dry, tab=tab_name)
            elif name == "reply_copied":   # Copy on a draft he sends himself (Oct 31), the text as copied
                sent = str(body.get("text", ""))[:2000]
                d = (row.get("draft") or {}).get("body", "")
                signal(name, row, day_name, test=dry, via=(row.get("draft") or {}).get("via", ""),
                       edited=sent.strip() != d.strip(), **({"edit": {"draft": d, "sent": sent}} if sent.strip() != d.strip() else {}),
                       wa=bool(body.get("wa")))
            return JSONResponse({"ok": True, "keep": True, "msg": ""})
        if action == "swap":
            signal("one_swapped", row, day_name, test=dry, previous=body.get("previous", ""))
            if not dry:
                save_override("one", base_day, row_id)
                p = task_path(row_id) if row["task"] else None
                if p and read_note(p)["scheduled"] != base_day.isoformat():
                    apply(p, "schedule", base=base_day)
            return JSONResponse({"ok": True, "keep": dry,
                                 "msg": ("Test mode, nothing changed: " if dry else "") + "Now the one thing"})
        if action in ("select", "unselect"):
            # Add (Oct 30): he actively takes Claude's suggestion into his selection.
            # from: one | needs (Claude's pre-selection) | waiting | done | more | suggest. Anything
            # but one or needs is a pull from below: what Claude missed (owner decision, Oct 30).
            src = str(body.get("from", ""))[:12]
            pulled = action == "select" and src in ("waiting", "done", "more", "suggest")
            signal("pulled_up" if pulled else ("selected" if action == "select" else "unselected"),
                   row, day_name, test=dry, section=src, suggest=row.get("suggest") or "")
            if dry:
                return JSONResponse({"ok": True, "keep": True,
                                     "msg": "Test mode, nothing changed: " + ("added" if action == "select" else "taken out")})
            save_override("selected" if action == "select" else "unselected", base_day, row_id)
            if pulled:
                save_override("pulled", base_day, row_id)
            p = task_path(row_id) if row["task"] else None
            if action == "select" and p:
                live = read_note(p)
                if live["scheduled"] != base_day.isoformat():
                    apply(p, "schedule", base=base_day)   # the date in the note follows his choice
            return JSONResponse({"ok": True, "keep": True,
                                 "msg": "Added" if action == "select" else "Taken out"})
        if action in ("answer", "prepare"):
            # His answer to one of Claude's questions continues that task (Oct 30).
            from . import prep
            st = prep.load_state(row_id) or {"id": row_id, "answers": []}
            if action == "answer":
                q, ans = _one_line(str(body.get("q", "")))[:300], _one_line(text_in)[:500]
                if not ans:
                    return JSONResponse({"error": "empty answer"}, status_code=400)
                signal("answered", row, day_name, test=dry, q=q, answer=ans)
                if dry:
                    return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing sent to Claude"})
                st.setdefault("answers", []).append({"q": q, "answer": ans,
                                                     "at": dt.datetime.now(TZ).isoformat(timespec="minutes")})
                p = task_path(row_id) if row["task"] else None
                if p:
                    t0 = p.read_text(encoding="utf-8")
                    hb, sp, bd = t0.partition("\n---\n")
                    _write(p, f"{hb}{sp}{_log(bd, f'- {_label(dt.datetime.now(TZ).date())} · the owner answered Claude: {q} → {ans} · check-in app')}")
            elif dry:
                return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing started"})
            st["pending"] = True
            st["asked"] = dt.datetime.now(TZ).isoformat(timespec="minutes")
            prep.save_state(row, st)
            started = start_prep(task=row_id)
            return JSONResponse({"ok": True, "keep": True,
                                 "msg": "Claude is on it, back here in 5 to 15 min" if started
                                 else "Queued, Claude finishes another task first"})
        if action in ("mailprep", "sendmail"):
            d = row.get("draft") or {}
            if d.get("via") != "mail":
                return JSONResponse({"error": "only mail drafts send from here; copy the rest"}, status_code=400)
            edit = body.get("mail") if isinstance(body.get("mail"), dict) else {}
            msg, edits = mail_of(row, edit)
            mailer = _mailer()
            try:
                # The mailbox the conversation lives in wins over the draft's acct (Oct 31); his tap on From wins over both.
                why = "you"
                if not edit.get("acct"):
                    msg["acct"], why = mailer.pick_mailbox(msg["subj"], msg["to"], prefer=msg["acct"])
                if action == "mailprep":
                    # Opening the sheet: who it goes to and whether it is in the thread, nothing kept.
                    res = mailer.prepare(msg, _since(d.get("at", "")))
                    mailer._pending.pop(res.get("id"), None)
                    try:   # where the earlier mail with them is (Oct 31); the sheet works without it
                        rel = mailer.related(res.get("to") or msg["to"], res.get("thread_id", ""))
                    except Exception as exc:
                        log_event("error", where="mail related", detail=str(exc)[:200])
                        rel = []
                    return JSONResponse({"ok": True, "keep": True, "msg": "",
                                         "mail": {**{k: res.get(k) for k in ("to", "cc", "subj", "thread", "thread_id",
                                                                              "held", "error")},
                                                  "acct": res.get("acct") or msg["acct"], "acct_why": why,
                                                  "history": res.get("history") or [], "related": rel}})
                if SLOT.search(msg["body"]) or (d.get("needs_slot") and msg["body"].strip() == (d.get("body") or "").strip()):
                    return JSONResponse({"error": "replace [slot] with a time first"}, status_code=400)
                if msg["acct"] not in mailer.MAILBOXES:
                    return JSONResponse({"error": "pick the mailbox to send from"}, status_code=400)
                res = mailer.prepare(msg, _since(d.get("at", "")), force=bool(body.get("force")))
                if res.get("held"):   # someone wrote since the draft: show it, his second tap sends
                    return JSONResponse({"ok": False, "keep": True, "held": res["held"],
                                         "msg": "New in the thread since the draft"})
                if not res.get("ok"):
                    return JSONResponse({"error": res.get("error") or "could not build the mail"}, status_code=400)
                to = ", ".join(res["to"])
                facts = {"edited": bool(edits), "edit": edits, "acct": res["acct"], "to": res["to"],
                         "cc": res["cc"], "thread": res["thread"], "forced": bool(body.get("force"))}
                if dry:
                    mailer._pending.pop(res["id"], None)
                    signal("reply_sent", row, day_name, test=True, **facts)
                    return JSONResponse({"ok": True, "keep": True,
                                         "msg": f"Test mode: would send to {to}" + (f", cc {', '.join(res['cc'])}" if res["cc"] else "")
                                                + (" in the thread" if res["thread"] else " as a new mail")})
                sent = mailer.commit(res["id"])
            except Exception as exc:   # Gmail down or a token gone: say so, never half-send
                log_event("error", where="sendmail", detail=str(exc)[:200])
                return JSONResponse({"error": f"could not send: {str(exc)[:120]}"}, status_code=502)
            if not sent.get("ok"):
                return JSONResponse({"error": sent.get("error") or "send failed"}, status_code=502)
            log_event("mail_sent", id=row_id, acct=sent["acct"], to=res["to"], cc=res["cc"],
                      gmail_id=sent.get("gmail_id"), thread_id=sent.get("thread_id"))
            signal("reply_sent", row, day_name, test=False, **facts)
            p = task_path(row_id) if row["task"] else None
            if p:
                return JSONResponse({"ok": True, "msg": apply(p, "sent", base=base_day, text_in=to)})
            save_override("hidden", base_day, row_id)
            try:
                raw = next((r for _, items in load_prerun().get("rows", []) for r in items
                            if not r.get("lk") and r.get("n") == row.get("title")), None)
                if raw:
                    _dropped_mod().record([raw], run=f"checkin-app-{base_day.isoformat()}", via="check-in app (mail sent)")
            except Exception as exc:
                log_event("error", where="dropped.record", detail=str(exc)[:200])
            return JSONResponse({"ok": True, "msg": f"Sent to {to}"})
        if action == "skipreply":
            # Skip on a reply (Oct 30: "similar skip should be stored for replies"): a task
            # moves a day; a taskless reply is hidden today and goes into the 14-day drop
            # memory, so the next pre-run does not draft it again unless the person writes.
            signal("reply_skipped", row, day_name, test=dry, draft=(row.get("draft") or {}).get("body", "")[:400])
            if dry:
                return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed: skipped"})
            p = task_path(row_id) if row["task"] else None
            if p:
                return JSONResponse({"ok": True, "msg": apply(p, "tomorrow", base=base_day)})
            save_override("hidden", base_day, row_id)
            try:
                raw = next((r for _, items in load_prerun().get("rows", []) for r in items
                            if not r.get("lk") and r.get("n") == row.get("title")), None)
                if raw:
                    _dropped_mod().record([raw], run=f"checkin-app-{base_day.isoformat()}", via="check-in app (reply skipped)")
            except Exception as exc:
                log_event("error", where="dropped.record", detail=str(exc)[:200])
            return JSONResponse({"ok": True, "msg": "Skipped"})
        if action == "notone":
            # "Remove from the one thing" (Oct 30): a ranking correction; the task stays in the list.
            signal("one_rejected", row, day_name, test=dry, why=why)
            if not dry:
                save_override("notone", base_day, row_id)
            return JSONResponse({"ok": True, "keep": dry,
                                 "msg": ("Test mode, nothing changed: " if dry else "") + "No longer the one thing"})
        if action == "notdone":
            signal("probably_done_rejected", row, day_name, test=dry)
            return JSONResponse({"ok": True, "keep": True, "msg": "Noted: still open"})

        if action == "talk":
            signal({"claude": "claude_go", "replies": "reply_talk"}.get(tab_name, "talk"), row, day_name,
                   test=dry, note=_one_line(text_in))
            if dry:
                return JSONResponse({"ok": True, "msg": "Test mode, nothing sent to the chat", "keep": True})
            remember_talk(row)
            notify.send_telegram(talk_text(row, _one_line(text_in)), silent=False)
            return JSONResponse({"ok": True, "msg": "Sent to the chat", "close": True})

        # Remove, with one of the four reasons (adopted Oct 30) and an optional why.
        if action == "remove":
            reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
            do = {"done": "done", "nottoday": "tomorrow", "notimportant": "lower",
                  "shouldnt": "cancel"}.get(reason)
            if not do:
                return JSONResponse({"error": "pick a reason"}, status_code=400)
            signal("removed", row, day_name, test=dry, reason=reason, why=why)
            p = task_path(row_id) if row["task"] else None
            if not p:
                # A row with no task note (new from mail or chats): hide it for that day, and
                # put it in the 14-day drop memory so the next pre-run does not bring it back.
                if not dry:
                    save_override("hidden", base_day, row_id)
                    try:
                        raw = next((r for _, items in load_prerun().get("rows", []) for r in items
                                    if not r.get("lk") and r.get("n") == row.get("title")), None)
                        if raw:
                            _dropped_mod().record([raw], run=f"checkin-app-{base_day.isoformat()}",
                                                  via=f"check-in app ({reason})")
                    except Exception as exc:
                        log_event("error", where="dropped.record", detail=str(exc)[:200])
                return JSONResponse({"ok": True, "keep": dry,
                                     "msg": ("Test mode, nothing changed: " if dry else "") + "Removed"})
            return JSONResponse({"ok": True, "keep": dry,
                                 "msg": apply(p, do, base=base_day, dry=dry, why=why)})

        if action == "update":
            p = task_path(row_id) if row["task"] else None
            if not p:
                return JSONResponse({"error": "no task note yet; use Send to Claude"}, status_code=400)
            # Every field is text from the form; a number is accepted as text, anything else dropped.
            fields = {k: v if isinstance(v, str) else str(v)
                      for k, v in (body.get("fields") if isinstance(body.get("fields"), dict) else {}).items()
                      if k in ("status", "priority", "due", "scheduled", "time_est", "next", "next_min")
                      and isinstance(v, (str, int, float)) and not isinstance(v, bool)}
            msg = apply(p, "update", base=base_day, dry=dry, fields=fields)
            if fields.get("_changes"):
                signal("edited", row, day_name, test=dry,
                       changes=[{"field": k, "old": o, "new": n} for k, o, n in fields["_changes"]])
            return JSONResponse({"ok": True, "keep": True, "msg": msg})
        if action in ("raise", "noraise"):
            # The offer after 3 meditation mentions (Oct 31): one step up, or not now for a week.
            p = task_path(row_id) if row["task"] else None
            if not p:
                return JSONResponse({"error": "no task note"}, status_code=400)
            if action == "noraise":
                signal("raise_dismissed", row, day_name, test=dry, mentions=row.get("mind", 0))
                if not dry:
                    dismiss_raise(row_id, base_day)
                return JSONResponse({"ok": True, "keep": True, "msg": "Not now"})
            cur = read_note(p).get("priority") or "1-none"
            nxt = PRIO_STEPS[min(PRIO_STEPS.index(cur) + 1, len(PRIO_STEPS) - 1)] if cur in PRIO_STEPS else "3-normal"
            signal("priority_raised", row, day_name, test=dry, mentions=row.get("mind", 0), old=cur, new=nxt)
            msg = apply(p, "update", base=base_day, dry=dry, fields={"priority": nxt})
            return JSONResponse({"ok": True, "keep": True, "msg": "Priority raised" if msg == "Saved" else msg})
        if action == "reopen":
            # Reopen from the Done fold: back to open, completedDate cleared, logged (Oct 31).
            p = task_path(row_id) if row["task"] else None
            if not p:
                return JSONResponse({"error": "this task is archived; reopen it in Obsidian"}, status_code=400)
            signal("reopened", row, day_name, test=dry)
            fields = {"status": "open"}
            msg = apply(p, "update", base=base_day, dry=dry, fields=fields)
            return JSONResponse({"ok": True, "keep": True, "msg": "Reopened" if msg == "Saved" else msg})
        if action == "done" and not (row["task"] and task_path(row_id)):
            # Done on a row with no task note (from mail or chats, Oct 31: the Luciana payment sat
            # in Probably done with Done greyed out): hide it for the day and remember the drop,
            # exactly as Remove > Already done does.
            signal("removed", row, day_name, test=dry, reason="done", why="")
            if not dry:
                save_override("hidden", base_day, row_id)
                try:
                    raw = next((r for _, items in load_prerun().get("rows", []) for r in items
                                if not r.get("lk") and r.get("n") == row.get("title")), None)
                    if raw:
                        _dropped_mod().record([raw], run=f"checkin-app-{base_day.isoformat()}",
                                              via="check-in app (done)")
                except Exception as exc:
                    log_event("error", where="dropped.record", detail=str(exc)[:200])
            return JSONResponse({"ok": True, "keep": dry,
                                 "msg": ("Test mode, nothing changed: " if dry else "") + "Done"})
        if action in ("done", "tomorrow", "next", "note", "schedule"):
            p = task_path(row_id) if row["task"] else None
            if not p:
                return JSONResponse({"error": "no task note for this row; use Talk"},
                                    status_code=400)
            if action == "done":
                name = {"replies": "reply_done_himself"}.get(tab_name) or (
                    "probably_done_confirmed" if row.get("guess") == "done" else "done_missed")
                signal(name, row, day_name, test=dry, why=why)
            elif action == "tomorrow":
                signal({"replies": "reply_skipped", "claude": "claude_not_now"}.get(tab_name, "moved"),
                       row, day_name, test=dry)
            elif action == "next":
                signal("next_rewritten", row, day_name, test=dry, old=row.get("next", ""), new=_one_line(text_in))
            elif action == "note":
                signal("note", row, day_name, test=dry, note=_one_line(text_in))
            elif action == "schedule":
                signal("added", row, day_name, test=dry, source="suggested", suggest=row.get("suggest", ""))
            try:
                return JSONResponse({"ok": True, "msg": apply(p, action, base=base_day,
                                                              text_in=text_in, dry=dry, why=why),
                                     "keep": dry or action in ("next", "note")})
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"error": "unknown action"}, status_code=400)

    # --- chat in the app (chat.py, Oct 31): Talk, Let Claude, Save & send, Plan it with Claude
    hub_box: dict = {}

    def hub():
        if "hub" not in hub_box:
            import asyncio
            from .chat import Hub
            hub_box["hub"] = Hub()
            hub_box["reaper"] = asyncio.create_task(hub_box["hub"].housekeeping())
        return hub_box["hub"]

    def chat_key(kind: str, row_id: str, day_name: str) -> str:
        if kind == "plan":
            return "plan:" + (dt.datetime.now(TZ).date() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)).isoformat()
        return "task:" + row_id

    async def day_view(req: Request):
        """The day's calendar for the Plan chat's day view (Oct 31)."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        now = dt.datetime.now(TZ)
        d = now.date() + dt.timedelta(days=1 if req.query_params.get("day") == "tomorrow" else 0)
        ev = day_events(d, fresh=req.query_params.get("fresh") == "1")
        return JSONResponse({"date": d.isoformat(), "events": ev or [], "ok": ev is not None,
                             "day_start": DAY_START, "day_end": DAY_END,
                             "now": now.hour * 60 + now.minute if d == now.date() else None})

    async def chat_get(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        q = req.query_params
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        key = chat_key(q.get("kind", "task"), q.get("id", ""), q.get("day", "today"))
        return JSONResponse(h.state(key))

    async def chat_post(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from .chat import Busy
        try:
            body = await req.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad request"}, status_code=400)
        op, kind, row_id = body.get("op", "send"), body.get("kind", "task"), str(body.get("id", ""))
        day_name = "tomorrow" if body.get("day") == "tomorrow" else "today"
        dry, tab_name, first = bool(body.get("test")), body.get("tab", ""), bool(body.get("first"))
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        key = chat_key(kind, row_id, day_name)
        if op == "approve":
            allow = bool(body.get("allow"))
            log_event("chat_approval", key=key, allow=allow, test=dry, src=source(req))
            if not h.answer(str(body.get("req", "")), allow):
                return JSONResponse({"error": "that request has expired"}, status_code=410)
            return JSONResponse(h.state(key))
        if op == "stop":
            await h.stop(key)
            return JSONResponse(h.state(key))
        return await send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first)

    async def send_message(req, h, body, key, kind, row_id, day_name, dry, tab_name, first, voice=False):
        from .chat import Busy
        text = str(body.get("text", ""))[:4000].strip()
        base_day = dt.datetime.now(TZ).date() + dt.timedelta(days=1 if day_name == "tomorrow" else 0)
        context, row = "", None
        if kind == "plan":
            title = f"Plan {_label(base_day)}"
            if not text:
                v = await asyncio.to_thread(current_view)
                ids = [str(i) for i in (body.get("ids") if isinstance(body.get("ids"), list) else [])][:TOP_N + 1]
                picked, seen = [], set()
                for i in ids:
                    r = next((r for r in all_rows(v) if r["id"] == i), None)
                    if r and r["id"] not in seen:
                        seen.add(r["id"]); picked.append(r)
                if not picked:
                    return JSONResponse({"error": "nothing selected to plan"}, status_code=400)
                text = plan_text(base_day, picked, ending=False)
            if first:
                log_event("tap", action="plan", id="", test=dry, src=source(req), tab=tab_name, edited=False)
        else:
            row = await asyncio.to_thread(find, row_id)
            title = (row or {}).get("title") or load_title(key) or row_id
            if not text:
                return JSONResponse({"error": "empty message"}, status_code=400)
            if first and row:
                # The same signals the Telegram Talk logged (claude_go, reply_talk, talk), at the
                # moment the first message goes, as the old tap sent at once.
                log_event("tap", action="talk", id=row_id, test=dry, src=source(req), tab=tab_name, edited=True)
                signal({"claude": "claude_go", "replies": "reply_talk"}.get(tab_name, "talk"), row, day_name,
                       test=dry, note=_one_line(text), via="app chat", opened_by=str(body.get("from", ""))[:20])
                if not dry:
                    remember_talk(row)
                context = talk_text(row, ending=False)
            elif not row and not load_title(key):
                return JSONResponse({"error": "row not found, refresh"}, status_code=404)
            else:
                log_event("tap", action="chat", id=row_id, test=dry, src=source(req), tab=tab_name, edited=True)
        # Every message he writes is judgment data (Oct 31, the owner: chat should feed the signals too):
        # his words verbatim, next to what Claude had suggested for the row (signal's `claude`).
        signal("chat_message", row or {"id": row_id or key, "title": title}, day_name, test=dry, key=key,
               text=text[:2000], voice=voice, first=first, src=source(req),
               **({"ids": [str(i) for i in (body.get("ids") or [])]} if kind == "plan" else {}))
        try:
            if voice:
                from jobs.tg_bridge.media import frame_transcript
                return JSONResponse(h.send(key, title, text, context=context, test=dry, kind=kind,
                                           prompt_text=frame_transcript(text), voice=True))
            # Start on Do (Nov 1): the request to begin goes to Claude but not on his screen.
            return JSONResponse(h.send(key, title, text, context=context, test=dry, kind=kind,
                                       hidden=bool(body.get("hidden"))))
        except Busy as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    async def chat_voice(req: Request):
        """A voice message (Oct 31): the recording goes up as the raw body, is transcribed by the
        bridge's own Transcriber (Groq, then OpenAI), never kept, and continues as a message."""
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        from jobs.tg_bridge import media
        q = req.query_params
        audio = await req.body()
        if len(audio) > VOICE_MAX_BYTES:
            return JSONResponse({"error": "recording too long"}, status_code=413)
        try:
            h = hub()
        except Exception as exc:
            return JSONResponse({"error": f"chat unavailable: {exc}"}, status_code=503)
        ctype = req.headers.get("content-type", "")
        ext = "m4a" if "mp4" in ctype or "m4a" in ctype else "webm" if "webm" in ctype else "ogg"
        try:
            text, provider = await media.Transcriber(media.providers_from_env()).transcribe(audio, f"voice.{ext}")
        except media.MediaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        finally:
            del audio
        log_event("chat_voice", provider=provider, chars=len(text), src=source(req))
        if not text:
            return JSONResponse({"error": "no speech could be made out"}, status_code=422)
        body = {"text": text, "ids": q.getlist("ids"), "from": q.get("from", "")}
        day_name = "tomorrow" if q.get("day") == "tomorrow" else "today"
        kind, row_id = q.get("kind", "task"), q.get("id", "")
        return await send_message(req, h, body, chat_key(kind, row_id, day_name), kind, row_id, day_name,
                            q.get("test") == "1", q.get("tab", ""), q.get("first") == "1", voice=True)

    def load_title(key: str) -> str:
        from .chat import load
        return load(key).get("title", "")

    async def shutdown():
        if "hub" in hub_box:
            hub_box["reaper"].cancel()
            await hub_box["hub"].close()

    async def journal(req: Request):
        if not authed(req):
            return JSONResponse({"error": "unauthorised"}, status_code=403)
        try:
            return JSONResponse(await asyncio.to_thread(journal_entry, dt.datetime.now(TZ).date()))
        except OSError as exc:   # the template moved or the entry is unreadable: say which, never a 500
            log_event("error", where="journal", detail=repr(exc)[:200])
            return JSONResponse({"error": f"the meditation could not be read ({getattr(exc, 'filename', '') or exc})"},
                                status_code=503)

    routes = []
    for base in ("", "/checkin"):   # with or without the Funnel stripping its path
        routes += [Route(f"{base}/", page), Route(f"{base}/api/view", view),
                   Route(f"{base}/api/journal", journal),
                   Route(f"{base}/login", login, methods=["GET", "POST"]),
                   *[Route(f"{base}/{name}", static) for name in STATIC],
                   Route(f"{base}/api/act", act, methods=["POST"]),
                   Route(f"{base}/api/chat", chat_get), Route(f"{base}/api/chat", chat_post, methods=["POST"]),
                   Route(f"{base}/api/chat/voice", chat_voice, methods=["POST"]),
                   Route(f"{base}/api/day", day_view)]
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):   # Starlette 1.x: no on_shutdown; warm Claude clients close here
        import asyncio

        async def outcomes_loop():   # the server runs all day, so it keeps the 23:50 snapshot itself
            while True:
                try:
                    await asyncio.to_thread(record_outcomes)
                except Exception as exc:
                    log_event("error", where="day_outcome", detail=str(exc)[:200])
                await asyncio.sleep(300)
        task = asyncio.create_task(outcomes_loop())
        yield
        task.cancel()
        await shutdown()
    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    import uvicorn
    uvicorn.run(make_app(), host="127.0.0.1", port=PORT, log_level="warning")
