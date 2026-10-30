"""Beeper on vault-host: LinkedIn, Messenger and Instagram, read and (gated) sent.

One read path for every caller (msg_watch, the plan-today pre-run and delta,
the two-minute page's sender), so the field handling lives in one place.
Owner doc: _system/beeper-bridge.md.

Reads always pass --read-only. send_text() is the only write, and its callers
are approval-gated: the two-minute page's Send click (chatsender.py on the
Mac, over ssh) or a Telegram Allow tap. Nothing sends on its own.

Chats come back in the same shape as plan_today_delta's WhatsApp chats:
    {"chat", "chat_id", "network", "group", "messages": [{"t", "date", "from", "me", "text", "id"}]}
"""

import json
import os
import re
import subprocess
from html import unescape
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BEEPER = str(Path.home() / ".local/bin/beeper")
# The npm wrapper unpacks its binary in TMPDIR and renames it into ~/.cache;
# /tmp is a different filesystem on the box, so the rename fails with EXDEV.
TMPDIR = str(Path.home() / ".cache/tmp")
# WhatsApp belongs to WAHA (whatsapp-bridge.md); iMessage is out of scope; "matrix" is
# Beeper's own account (Beeper Updates, Note to self).
SKIP_NETWORKS = ("whatsapp", "imessage", "sms", "matrix", "beeper")
TZ = ZoneInfo("America/Los_Angeles")
LABELS = {"linkedin": "LinkedIn", "facebook": "Messenger", "messenger": "Messenger",
          "instagram": "Instagram"}


class BeeperFailed(Exception):
    pass


def run(*args: str, write: bool = False, timeout: int = 90) -> object:
    """Run the CLI and return its JSON payload. Read-only unless write=True."""
    env = {**os.environ, "TMPDIR": TMPDIR}
    flags = ["--json"]
    if write:
        env.pop("BEEPER_READONLY", None)
    else:
        env["BEEPER_READONLY"] = "1"
        flags.append("--read-only")
    os.makedirs(TMPDIR, exist_ok=True)
    try:
        out = subprocess.run([BEEPER, *args, *flags], env=env, capture_output=True,
                             text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BeeperFailed(f"{type(exc).__name__}: {exc}") from exc
    if out.returncode != 0:
        raise BeeperFailed((out.stderr or out.stdout).strip()[-500:])
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        raise BeeperFailed(f"not JSON: {out.stdout[:200]}") from exc
    # The CLI wraps results in an envelope; tolerate both shapes.
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 4:
        payload = payload["data"]
    return payload


def chat_index(limit: int = 300) -> dict:
    found = run("chats", "list", "--full", "--limit", str(limit))
    rows = found if isinstance(found, list) else (get(found, "items", []) or [])
    return {get(c, "id", ""): c for c in rows}


def chat_messages(chat_id: str, limit: int = 50) -> list:
    """One chat's latest messages, oldest first. Read per chat, never through `messages
    search`: search is backed by an index Beeper builds in the background and missed all of
    a contact's LinkedIn messages on 2026-10-16 while `messages list` had them."""
    found = run("messages", "list", "--chat", chat_id, "--full", "--limit", str(limit))
    items = found if isinstance(found, list) else (get(found, "items", []) or [])
    return sorted(items, key=lambda m: when(m) or datetime.min.replace(tzinfo=timezone.utc))


def active_chats(since: datetime) -> list[dict]:
    """Chats (not skipped, not low priority) with activity at or after `since`."""
    out = []
    for c in chat_index().values():
        net = network(c, {})
        if not net or any(x in net for x in SKIP_NETWORKS) or get(c, "isLowPriority"):
            continue
        last = when({"timestamp": get(c, "lastActivity", "")})
        if last and last >= since:
            out.append(c)
    return out


def recent(since: datetime, *, chars: int = 600) -> list[dict]:
    """Every active chat's messages at or after `since`, both directions."""
    out = []
    for chat in active_chats(since):
        msgs = [m for m in chat_messages(get(chat, "id", "")) if (when(m) or since) >= since]
        if not msgs:
            continue
        out.append({"chat": get(chat, "title", "") or get(chat, "id", ""),
                    "chat_id": get(chat, "id", ""), "network": label(network(chat, {})),
                    "group": is_group(chat), "muted": bool(get(chat, "isMuted")),
                    "messages": [row(m, chars) for m in msgs]})
    out.sort(key=lambda c: (c["group"], -len(c["messages"])))
    return out


def ever_wrote(chat_id: str, since: datetime | None = None) -> bool:
    return any(get(m, "isSender") and (since is None or (when(m) or since) >= since)
               for m in chat_messages(chat_id))


def when(m: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(get(m, "timestamp", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def row(m: dict, chars: int = 600) -> dict:
    ts = get(m, "timestamp", "")
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(TZ)
        t, date = d.strftime("%H:%M"), d.strftime("%Y-%m-%d")
    except ValueError:
        t, date = "", ""
    text = plain(get(m, "text", "") or "")
    if not text:
        kinds = {get(a, "type", "file") for a in get(m, "attachments", []) or []}
        text = f"[{', '.join(sorted(kinds)) or 'no text'}]"
    me = bool(get(m, "isSender", False))
    return {"t": t, "date": date, "from": "me" if me else get(m, "senderName", ""),
            "me": me, "text": text[:chars], "id": get(m, "id", "")}


def since_from_others(chat_id: str, since: datetime) -> list[dict]:
    """Messages from others in one chat at or after `since` (the stale check before a send)."""
    return [row(m) for m in chat_messages(chat_id, 20)
            if not get(m, "isSender") and (when(m) or since) >= since]


def send_text(chat_id: str, text: str) -> object:
    """The one write. Callers must already hold the owner's approval for this exact text."""
    return run("send", "text", "--to", chat_id, "--message", text, "--wait",
               write=True, timeout=60)


def network(chat: dict, msg: dict) -> str:
    """A short key: linkedin, facebook, instagram, matrix, ... Chats name it ("LinkedIn",
    "Facebook/Messenger", "Beeper (Matrix)"); accountIDs start with it ("linkedin.ACo…",
    "facebookgo.1699…")."""
    raw = str(get(chat, "network", "") or "").lower()
    acct = str(get(chat, "accountID", "") or get(msg, "accountID", "")).lower().split(".")[0]
    for key in ("linkedin", "facebook", "messenger", "instagram", "whatsapp", "imessage",
                "matrix", "beeper"):
        if key in raw or acct.startswith(key):
            return key
    return acct or raw


def label(net: str) -> str:
    return LABELS.get(net, net.title() or "Beeper")


def is_group(chat: dict) -> bool:
    """A real group. Marketplace threads come as type "group" with two participants
    (verified 2026-10-16: "Your Name · Example chat"); they are conversations with one
    seller, so they count as direct."""
    if str(get(chat, "type", "single")).lower() != "group":
        return False
    parts = get(chat, "participants", {}) or {}
    total = get(parts, "total") if isinstance(parts, dict) else len(parts)
    return not (isinstance(total, int) and total <= 2)


def plain(text: str) -> str:
    """LinkedIn messages arrive as HTML (<p>, <br>, <li>)."""
    if "<" in text:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", text)
        text = unescape(re.sub(r"<[^>]+>", "", text))
    return " ".join(text.split())


def get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else default


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
