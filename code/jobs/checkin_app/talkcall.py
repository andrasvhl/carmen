"""Talk sessions (Nov 11): one Talk from its first turn to its end, for two things the chat
engine does not know about. It owns `talk_sessions.json`; the conversation stays a Chat thread.

Resume (owner decision, Nov 11: continue instead of restart). The iPhone app and its Siri intent name
every Talk `talk-<yyyyMMdd-HHmm>`. A Talk whose first request comes within RESUME_S of the last
Talk turn continues that thread: the new name becomes an alias that /api/talk/stream,
/api/talk/voice and /api/talk/end apply (`resolve`, `target`), logged as `talk_resume`. Claude
gets RESUME_NOTE once, on the first turn. Only `talk-*` names: web chat, the Siri Shortcut's
`siri-*` threads and Telegram are untouched.

Recap (owner decision, Nov 11: what did the call actually do). A session ends by POST /api/talk/end from
the app, by [end talk] in Claude's answer, or IDLE_S after its last turn (the fallback for Siri
and app builds without the end call). Then the recap: built from what the turns logged (`chat`
events with their tools, `chat_delta` with the notes touched, approvals answered and still
waiting), worded by one Haiku call with a plain fallback, sent as a push and written as a note at
the end of the thread. Nothing changed and nothing waiting: no push, no note. Once per segment:
`claim` flips `recapped` under the file lock, so a second end call or the idle fallback after it
never pushes twice; a turn after the recap opens a new segment.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time

from runner import config

from .adapters import state
from .adapters import taplog

PATH = config.STATE_DIR / "checkin_app" / "talk_sessions.json"
RESUME_S = 2 * 3600      # a Talk started within 2 h of the last Talk turn continues that thread
IDLE_S = 180             # no turn for 3 min after the last one: the session has ended
KEEP_S = 3 * 86400       # sessions and aliases are forgotten after 3 days
TAIL_BYTES = 3_000_000   # how much of taps.jsonl a recap reads (about a day and a half)

RESUME_NOTE = ("\n[Talk note: this Talk continues the conversation in this thread from {gap} ago, so the owner "
               "may pick up where he left off. If it fits, say so in a few words (\"Weiter mit Iceland?\"); "
               "otherwise just answer. Never mention this note.]")

# Which tools change something outside the vault (the vault's own writes come as touched notes).
_DRAFT = re.compile(r"draft", re.I)
_SEND = re.compile(r"send_(gmail|message|email|mail)|whatsapp.*send|send_whatsapp", re.I)
_CAL = re.compile(r"(create|update|delete)_event|respond_to_event", re.I)


def _iso(ts: float) -> str:
    from .adapters import clock
    return dt.datetime.fromtimestamp(ts, clock.TZ).isoformat(timespec="seconds")


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read() -> dict:
    return state.read(PATH, {})


def _ago(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    return f"{m} min" if m < 90 else f"{round(m / 60, 1):g} h"


# ---- resume ---------------------------------------------------------------------------

def is_talk(name: str) -> bool:
    return name.startswith("talk-")


def _has_messages(name: str) -> bool:
    from .chat import load
    return bool(load("general:" + name).get("messages"))


def last_talk(exclude: str = "") -> tuple[str, float] | None:
    """The talk-* thread with the latest message, and that message's time."""
    from .chat import threads
    best = None
    for t in threads(limit=500):
        if is_talk(t["id"]) and t["id"] != exclude:
            ts = _ts(t.get("at", ""))
            if ts and (best is None or ts > best[1]):
                best = (t["id"], ts)
    return best


def target(name: str) -> str:
    """The thread a Talk name stands for: its alias, else itself. Never creates an alias."""
    a = (_read().get("alias") or {}).get(name)
    return a["to"] if a else name


def resolve(name: str, now: float | None = None) -> tuple[str, dict | None]:
    """(thread, alias info when this call just created it). A talk-* name seen for the first time,
    with no messages of its own, continues the last Talk thread if that one's last turn is under
    RESUME_S old."""
    now = time.time() if now is None else now
    if not is_talk(name):
        return name, None
    d = _read()
    a = (d.get("alias") or {}).get(name)
    if a:
        return a["to"], None
    if name in (d.get("sessions") or {}) or _has_messages(name):
        return name, None
    best = last_talk(exclude=name)
    if not best or now - best[1] >= RESUME_S:
        return name, None
    to = target(best[0])
    info = {"to": to, "at": _iso(now), "gap_s": round(now - best[1])}
    state.update(PATH, lambda d: d.setdefault("alias", {}).__setitem__(name, info), {}, indent=None)
    taplog.log_event("talk_resume", requested=name, thread=to, gap_min=round(info["gap_s"] / 60))
    return to, info


def resume_note(name: str) -> str:
    """The one-line continuation note for Claude, on the first turn of a resumed Talk only."""
    out = {"note": ""}

    def fn(d):
        a = (d.get("alias") or {}).get(name)
        if a and not a.get("noted"):
            a["noted"] = True
            out["note"] = RESUME_NOTE.format(gap=_ago(a.get("gap_s", 0)))
    state.update(PATH, fn, {}, indent=None)
    return out["note"]


# ---- sessions -------------------------------------------------------------------------

def touch(name: str, thread: str, *, src: str, test: bool = False, now: float | None = None) -> dict:
    """A turn of Talk `name` (in `thread`): starts a session or a new segment after a recap."""
    now = time.time() if now is None else now
    out = {}

    def fn(d):
        ss = d.setdefault("sessions", {})
        s = ss.get(name)
        if not s or s.get("recapped"):
            s = {"thread": thread, "started": now, "seg": (s or {}).get("seg", 0) + 1, "turns": 0, "src": src}
            ss[name] = s
        s.update(thread=thread, last=now, turns=s.get("turns", 0) + 1, live=bool(s.get("live")) or not test)
        s.pop("ended", None)
        for k, v in ss.items():   # an earlier Talk in this thread, not yet recapped: it ends here
            if k != name and v.get("thread") == thread and not v.get("recapped") and not v.get("until"):
                v.update(ended=now, until=now)
        for k in [k for k, v in ss.items() if now - v.get("last", 0) > KEEP_S]:
            del ss[k]
        al = d.setdefault("alias", {})
        for k in [k for k, v in al.items() if now - _ts(v.get("at", "")) > KEEP_S]:
            del al[k]
        out.update(s)
    state.update(PATH, fn, {}, indent=None)
    return out


def end(name: str, now: float | None = None) -> dict | None:
    """The Talk ended (the app's end call, or [end talk]): due for its recap once the turn is over."""
    now = time.time() if now is None else now
    out = {}

    def fn(d):
        s = (d.get("sessions") or {}).get(name)
        if s and not s.get("recapped"):
            s["ended"] = now
            out.update(s)
        elif s:
            out.update(s)
    state.update(PATH, fn, {}, indent=None)
    return out or None


def due(busy, last_at=None, now: float | None = None) -> list[tuple[str, dict]]:
    """Sessions whose recap is due: live, not recapped, their thread not busy, and ended or idle
    IDLE_S since the last turn (the later of the last request and the thread's last message)."""
    now = time.time() if now is None else now
    out = []
    for name, s in (_read().get("sessions") or {}).items():
        if s.get("recapped") or not s.get("live"):
            continue
        try:
            if busy(s["thread"]):
                continue
        except Exception:
            continue
        last = max(s.get("last", 0), last_at(s["thread"]) if last_at else 0)
        if s.get("ended") or now - last >= IDLE_S:
            out.append((name, s))
    return out


def claim(name: str, seg: int) -> bool:
    """True once per session segment: the caller makes the recap."""
    out = {"ok": False}

    def fn(d):
        s = (d.get("sessions") or {}).get(name)
        if s and s.get("seg") == seg and not s.get("recapped"):
            s["recapped"] = time.time()
            out["ok"] = True
    state.update(PATH, fn, {}, indent=None)
    return out["ok"]


def last_message_at(thread: str) -> float:
    from .chat import load
    msgs = load("general:" + thread).get("messages") or []
    return _ts(msgs[-1].get("at", "")) if msgs else 0.0


# ---- the recap ------------------------------------------------------------------------

def _events(key: str, since: float) -> list[dict]:
    try:
        with taplog.LOG.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        if key not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("key") == key and _ts(e.get("at", "")) >= since - 1:
            out.append(e)
    return out


def facts(key: str, since: float, pending: list[dict], until: float | None = None) -> dict:
    """What the session did, from the log only: tasks and notes touched, drafts, mail sent,
    calendar changes, approvals answered and waiting, and Claude's replies (for names)."""
    tasks, notes, tools, replies, answered, touched = [], [], [], [], [], []
    hints, has_hints = [], False
    for e in _events(key, since):
        if until and _ts(e.get("at", "")) > until + 1:
            continue
        if e.get("kind") == "chat" and not e.get("test"):
            tools += [str(t) for t in e.get("tools") or []]
            if "hints" in e:
                has_hints = True
                hints += [str(x) for x in e.get("hints") or []]
            if e.get("reply"):
                replies.append(str(e["reply"])[:600])
        elif e.get("kind") == "signal" and e.get("signal") == "chat_delta":
            touched += list(e.get("touched") or [])
        elif e.get("kind") == "chat_approval":
            answered.append("allowed" if e.get("allow") else "denied")
    if has_hints:
        # touched is every note written during the turn, by anyone (Nov 11: the recap credited a
        # doc edit made from the Mac to the Talk). Count a note only when one of the turn's tools named it.
        blob = "\n".join(hints)
        touched = [p for p in touched if p in blob or p.rsplit("/", 1)[-1] in blob
                   or p.rsplit("/", 1)[-1][:-3] in blob]
    for p in touched:
        bucket = tasks if p.startswith("Inbox/") and p.count("/") == 1 else notes
        if p not in bucket:
            bucket.append(p)
    waiting = [{"verb": a.get("verb", ""), "shown": str(a.get("shown", ""))[:120]}
               for a in pending if a.get("key") == key]
    return {"tasks": [p[len("Inbox/"):-3] if p.endswith(".md") else p for p in tasks],
            "notes": [p[:-3] if p.endswith(".md") else p for p in notes],
            "drafts": sum(1 for t in tools if _DRAFT.search(t)),
            "sent": sum(1 for t in tools if _SEND.search(t)),
            "calendar": sum(1 for t in tools if _CAL.search(t)),
            "answered": answered, "waiting": waiting, "turns": len(replies), "replies": replies[-8:]}


def changed(f: dict) -> bool:
    return bool(f["tasks"] or f["notes"] or f["drafts"] or f["sent"] or f["calendar"] or f["waiting"])


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def plain(f: dict) -> tuple[str, str]:
    """(push line, thread note) without the model."""
    parts = []
    if f["tasks"]:
        parts.append(("edited " + ", ".join(f["tasks"])) if len(f["tasks"]) <= 2 else "edited " + _n(len(f["tasks"]), "task", "tasks"))
    if f["notes"]:
        parts.append("updated " + _n(len(f["notes"]), "note", "notes"))
    if f["drafts"]:
        parts.append("drafted " + _n(f["drafts"], "email", "emails"))
    if f["sent"]:
        parts.append("sent " + _n(f["sent"], "message", "messages"))
    if f["calendar"]:
        parts.append(_n(f["calendar"], "calendar change", "calendar changes"))
    if f["waiting"]:
        parts.append(f"{len(f['waiting'])} {'needs' if len(f['waiting']) == 1 else 'need'} your OK")
    push = "Talk: " + ", ".join(parts)
    lines = []
    if f["tasks"]:
        lines.append("Tasks: " + ", ".join(f["tasks"]) + ".")
    if f["notes"]:
        lines.append("Notes: " + ", ".join(f["notes"]) + ".")
    rest = [p for p in parts if not p.startswith(("edited", "updated"))]
    if rest:
        lines.append(rest[0][0].upper() + ", ".join(rest)[1:] + ".")
    return push, "Recap: " + " ".join(lines)


PROMPT = """You write the recap of a voice conversation the owner just had with Carmen, his assistant.
The JSON on stdin is the record of the session: `tasks` and `notes` are vault notes it changed,
`drafts`, `sent` and `calendar` count email drafts, sent messages and calendar changes, `waiting`
are approvals still waiting for his OK, `replies` is what Carmen said. State only what the record
shows was done; use the replies only to name things (whose email, which task, what moved).
Do not use any tools. Answer with exactly two lines and nothing else:
PUSH: <at most 100 characters, starting "Talk: ", short past-tense items joined by commas, e.g. "Talk: moved 3 tasks, drafted Leo reply, 1 needs your OK">
RECAP: <one to three short sentences for the chat thread naming each changed task, note or draft; last, what still needs his OK, if anything>"""


def _ask(prompt: str, data: str) -> str:
    from runner import ask
    return ask.ask(prompt, model="haiku", timeout=90, data=data)


def _push(title: str, message: str, priority: int) -> None:
    from runner import notify
    notify.send_push(title, message, priority=priority)


ASK, PUSH = _ask, _push   # replaced by the gate tests


def compose(f: dict) -> tuple[str, str, str]:
    """(push line, thread note, "haiku" or "plain")."""
    push, note = plain(f)
    try:
        out = ASK(PROMPT, json.dumps(f, ensure_ascii=False))
        p = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().upper().startswith("PUSH:")), "")
        r = next((l.split(":", 1)[1].strip() for l in out.splitlines() if l.strip().upper().startswith("RECAP:")), "")
        if p and r:
            p = p if p.lower().startswith("talk:") else "Talk: " + p
            return p[:140], "Recap: " + r, "haiku"
    except Exception as exc:
        taplog.log_event("error", where="talk_recap_haiku", detail=str(exc)[:200])
    return push, note, "plain"


async def deliver(name: str, s: dict, pending: list[dict]) -> dict:
    """The recap of one claimed session: facts, words, push, note at the end of the thread."""
    import asyncio
    key = "general:" + s["thread"]
    f = await asyncio.to_thread(facts, key, s.get("started", 0), [] if s.get("until") else pending,
                                s.get("until"))
    if not changed(f):
        taplog.log_event("talk_recap", name=name, key=key, seg=s.get("seg"), pushed=False, why="nothing changed",
                         turns=f["turns"])
        return {"pushed": False}
    push, note, how = await asyncio.to_thread(compose, f)
    pushed = True
    try:
        await asyncio.to_thread(PUSH, "Carmen", push, 0 if f["waiting"] else -1)
    except Exception as exc:
        pushed = False
        taplog.log_event("error", where="talk_recap_push", detail=str(exc)[:200])
    from .chat import load, save, _now
    conv = load(key)   # no await between load and save: a turn's own save reloads first
    conv["messages"].append({"role": "note", "text": note, "at": _now(), "test": False, "recap": True})
    save(conv)
    taplog.log_event("talk_recap", name=name, key=key, seg=s.get("seg"), pushed=pushed, how=how, push=push,
                     note=note[:600], tasks=len(f["tasks"]), notes=len(f["notes"]), drafts=f["drafts"],
                     waiting=len(f["waiting"]))
    return {"pushed": pushed, "push": push, "note": note}
