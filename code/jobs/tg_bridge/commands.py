"""/new /stop /status /sessions, and the stop words.

A command is only a command when it is the whole message (`/new <text>` aside,
where the rest becomes the first prompt). Anything looser would eat a message
that merely mentions one, the same rule as the WhatsApp bridge's.

Stop words are matched exactly after normalisation: case, surrounding space and
trailing punctuation do not matter, anything else does. "stop" stops; "stop
the dentist reminder" is a message. They bypass the queue entirely: a stop
that waits behind the run it is meant to stop is not a stop.
"""

import html
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

STOP_WORDS = {"stop", "stopp", "aufhören", "abort", "halt", "/stop"}
SESSION_CALLBACK = "tgs1"

# Shown in the / menu, set once at startup.
MENU = [
    ("new", "Fresh conversation in this topic"),
    ("stop", "Stop the running turn and clear the queue"),
    ("status", "Session, activity, queue"),
    ("sessions", "List or switch named sessions"),
]
_KNOWN = {name for name, _ in MENU} | {"start", "help"}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").strip().lower()
    text = re.sub(r"[\s.!?…,;:]+$", "", text)
    return re.sub(r"\s+", " ", text)


def is_stop(text: str, bot_username: str = "") -> bool:
    norm = normalize(text)
    if bot_username and norm == f"/stop@{bot_username.lower()}":
        return True
    return norm in STOP_WORDS


@dataclass
class Command:
    name: str
    arg: str = ""


def parse(text: str, bot_username: str = "") -> Command | None:
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    name, _, target = head[1:].partition("@")
    if target and bot_username and target.lower() != bot_username.lower():
        return None                     # addressed to another bot
    name = name.lower()
    if name not in _KNOWN:
        return None
    return Command(name, rest.strip())


def _ago(stamp: str | None) -> str:
    if not stamp:
        return "never"
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - when).total_seconds())
    if secs < 90:
        return f"{secs}s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def status_text(session, *, busy_for: float | None, queued: int, warm: int,
                approvals: int) -> str:
    lines = [
        f"<b>Session</b> {html.escape(session.name)} "
        f"(<code>{session.claude_session_id[:8]}</code>)",
        f"Started {_ago(session.session_started_at)}, last message {_ago(session.last_interaction_at)}",
        f"Running: {'yes, for ' + str(int(busy_for)) + 's' if busy_for is not None else 'no'}",
        f"Queued: {queued}" + (f" · waiting for your tap: {approvals}" if approvals else ""),
        f"Warm Claude processes: {warm}",
    ]
    if session.aborted_last_run:
        lines.append("The last run was stopped; the next one is told so.")
    return "\n".join(lines)


def sessions_text(current: str, sessions: list) -> tuple[str, dict | None]:
    if not sessions:
        return "No sessions yet. Send a message to start one.", None
    lines = ["<b>Sessions here</b>"]
    for s in sessions:
        mark = " (current)" if s.name == current else ""
        lines.append(f"• {html.escape(s.name)}{mark}, last message {_ago(s.last_interaction_at)}")
    lines.append("\nTap to switch, or <code>/sessions name</code> to create one.")
    rows = [[{"text": s.name + (" ✓" if s.name == current else ""),
              "callback_data": f"{SESSION_CALLBACK}:{s.id}"}] for s in sessions[:20]]
    return "\n".join(lines), {"inline_keyboard": rows}


def parse_session_callback(data: str) -> int | None:
    prefix, _, ident = (data or "").partition(":")
    if prefix != SESSION_CALLBACK or not ident.isdigit():
        return None
    return int(ident)


_NAME = re.compile(r"^[\w.-]{1,40}$")


def valid_session_name(name: str) -> bool:
    return bool(_NAME.match(name))
