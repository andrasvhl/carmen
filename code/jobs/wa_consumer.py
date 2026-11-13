"""Long-lived: the WhatsApp bridge consumer. WAHA webhooks in, Claude turns out.

Every other file in `jobs/` runs, exits and reports. This one does not: it is a
daemon, so it has no `Job()` context manager, no exit-code contract and no
digest state. What it *does* keep is everything the runner already got right --
`runner.config` for paths and credentials, `runner.job.log` for journald-only
logging, `runner.notify.send_mail` for the alert path, and `fcntl` locking so
two of these can never run at once. Nothing here is a second implementation of
something the runner already has.

Design and the decisions behind it: `_system/whatsapp-bridge.md` in the vault.
The three that shape this file most:

  * **Parity, minus the shell.** The agent gets the real vault as cwd, so the
    vault CLAUDE.md, the Router and every skill load exactly as in a terminal
    session. `Bash` is deliberately off the allowlist for v1: whoever holds
    this thread would otherwise hold a shell on the box.
  * **No second surface.** How the bridge writes lives in the vault, at
    `_system/whatsapp-bridge/reply-style.md`, read fresh every turn and passed
    as `--append-system-prompt`. Never a bridge-local prompt file.
  * **Silence on failure is the worst outcome available.** A timeout, a
    non-zero exit or an exception sends a WhatsApp message AND mails. A turn
    that produced nothing must never look like a turn nobody sent.

Run it under systemd (its own unit, not `automation@.service`, which is
`Type=oneshot`):

    ssh vault-host 'cd ~/automation && python3 -m jobs.wa_consumer'

Configuration is entirely environment, falling back to `~/.automation.env`:

    WA_HMAC_KEY       required. WAHA's webhook HMAC secret
    WA_ALLOWLIST      required. Comma-separated JIDs allowed to talk to it
    WA_API_KEY        WAHA's X-Api-Key (session-scoped, never the admin key)
    WA_BASE_URL       default http://127.0.0.1:3000
    WA_SESSION        WAHA session name, default "default"
    WA_CONSUMER_PORT  default 8099, bound to loopback only
    WA_IDLE_HOURS     mint a fresh Claude session after this much quiet, default 8
    WA_TURN_TIMEOUT   seconds a single turn may take, default 300
"""

import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid as uuidlib
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from runner import config, notify
from runner.job import log

# Read once at startup. A daemon re-reading the credential file on every turn
# would pick up a half-written edit; restarting it is the way to change config.
_ENV = config.load_env()


def _env(name: str, default: str = "") -> str:
    """Process environment first, ~/.automation.env second."""
    value = os.environ.get(name)
    if value:
        return value
    return _ENV.get(name, default)


PORT = int(_env("WA_CONSUMER_PORT", "8099"))
HMAC_KEY = _env("WA_HMAC_KEY")
ALLOWLIST = [a.strip() for a in _env("WA_ALLOWLIST").split(",") if a.strip()]
BASE_URL = _env("WA_BASE_URL", "http://127.0.0.1:3000").rstrip("/")
# Bind address. Defaults to loopback, but WAHA runs in a container and cannot reach the
# host's 127.0.0.1, so on vault-host this is the docker bridge (172.17.0.1): reachable
# from the container and the host, not from the tailnet or anywhere else.
HOST = _env("WA_CONSUMER_HOST", "127.0.0.1")
WAHA_SESSION = _env("WA_SESSION", "default")
API_KEY = _env("WA_API_KEY")
IDLE_HOURS = float(_env("WA_IDLE_HOURS", "8"))
TURN_TIMEOUT = int(_env("WA_TURN_TIMEOUT", "300"))

STATE_PATH = config.STATE_DIR / "wa-sessions.json"
LOCK_PATH = config.LOCK_DIR / "wa_consumer.lock"
REPLY_STYLE = config.VAULT / "_system" / "whatsapp-bridge" / "reply-style.md"

# Read, search, and write inside the vault. No Bash: see the module docstring.
ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]

DEFAULT_SESSION = "vault"

# WhatsApp collapses anything past roughly 700 characters behind "Read more",
# so this is not a rendering limit, it is the point past which a message stops
# being a message and should be a file instead.
CHUNK_LIMIT = 3000

# The message-routing map, bounded two ways so it cannot grow forever.
MESSAGE_TTL_DAYS = 30
MESSAGE_CAP = 5000

# Identical failure mails inside this window are logged and not re-sent. The
# WhatsApp alert always goes out, so nothing is ever silent; this only stops a
# crash loop turning the mail channel into one the owner mutes.
MAIL_THROTTLE_S = 600


# --- WAHA ------------------------------------------------------------------

class WahaError(Exception):
    pass


def waha(path: str, payload: dict) -> dict:
    """POST to WAHA. Raises WahaError on anything that is not a 2xx."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/api/{path.lstrip('/')}", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise WahaError(f"{path} -> HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise WahaError(f"{path} -> {type(exc).__name__}: {exc}")
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def waha_get(path: str) -> dict:
    """GET from WAHA. Same error contract as waha()."""
    req = urllib.request.Request(
        f"{BASE_URL}/api/{path.lstrip('/')}", method="GET",
        headers={"X-Api-Key": API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise WahaError(f"{path} -> HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise WahaError(f"{path} -> {type(exc).__name__}: {exc}")
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {}


def _message_id(result: dict) -> str:
    """WAHA's send responses put the id in one of two shapes, per engine."""
    ident = result.get("id")
    if isinstance(ident, str):
        return ident
    if isinstance(ident, dict):
        return ident.get("_serialized") or ident.get("id") or ""
    return ""


# Ids of messages this consumer sent. `fromMe` cannot be the echo filter,
# because in a self-chat it is true for the owner's messages and the bot's alike.
_sent_ids: set[str] = set()
_sent_order: deque[str] = deque(maxlen=500)
_sent_lock = threading.Lock()


def _remember_sent(msg_id: str) -> None:
    if not msg_id:
        return
    with _sent_lock:
        if len(_sent_order) == _sent_order.maxlen and _sent_order:
            _sent_ids.discard(_sent_order[0])
        _sent_order.append(msg_id)
        _sent_ids.add(msg_id)


def is_echo(msg_id: str) -> bool:
    with _sent_lock:
        return msg_id in _sent_ids


# Outbound pacing. Message velocity is one of the few ban signals we actually
# control, and WhatsApp throttles well before it bans. Every outbound send goes
# through here, so a long chunked answer cannot burst. See whatsapp-source.md.
SEND_GAP_S = float(os.environ.get("WA_SEND_GAP_S", "3"))
_pace_lock = threading.Lock()
_last_send = 0.0


def _pace() -> None:
    global _last_send
    with _pace_lock:
        wait = SEND_GAP_S - (time.monotonic() - _last_send)
        if wait > 0:
            time.sleep(wait)
        _last_send = time.monotonic()


def _refuse_if_tripped(what: str) -> None:
    """The latch has to bite at the last possible moment, not only at the top of
    a handler. Anything already in flight when the tripwire fires must not get
    one more packet onto a flagged account."""
    if is_tripped():
        raise WahaError(f"{what} refused: the ban tripwire is latched")


def send_text(chat: str, text: str, reply_to: str = "") -> str:
    _refuse_if_tripped("sendText")
    _pace()
    result = waha("sendText", {"session": WAHA_SESSION, "chatId": chat,
                               "text": text, "reply_to": reply_to or None})
    msg_id = _message_id(result)
    _remember_sent(msg_id)
    return msg_id


def send_file(chat: str, filename: str, content: str, caption: str,
              reply_to: str = "") -> str:
    """Send a locally generated file as base64.

    Never `file.url`: WAHA would have to fetch it, and a loopback-bound box is
    not reachable from wherever it would fetch from.
    """
    _refuse_if_tripped("sendFile")
    result = waha("sendFile", {
        "session": WAHA_SESSION, "chatId": chat, "caption": caption,
        "reply_to": reply_to or None,
        "file": {"mimetype": "text/markdown", "filename": filename,
                 "data": base64.b64encode(content.encode("utf-8")).decode("ascii")},
    })
    msg_id = _message_id(result)
    _remember_sent(msg_id)
    return msg_id


def _quietly(what: str, path: str, payload: dict) -> None:
    """Presence and read receipts are courtesies. They never fail a turn."""
    try:
        waha(path, payload)
    except WahaError as exc:
        log(f"wa_consumer: {what} failed, continuing ({exc})")


def start_typing(chat: str) -> None:
    _quietly("startTyping", "startTyping", {"session": WAHA_SESSION, "chatId": chat})


def stop_typing(chat: str) -> None:
    _quietly("stopTyping", "stopTyping", {"session": WAHA_SESSION, "chatId": chat})


def mark_read(chat: str, msg_id: str) -> None:
    _quietly("sendSeen", "sendSeen", {"session": WAHA_SESSION, "chatId": chat,
                                      "messageId": msg_id})


# --- session state ---------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale(stamp: str | None, hours: float) -> bool:
    """True when the last activity is older than the idle window.

    An unparseable or missing timestamp counts as stale: minting a fresh
    session costs a lost context, resuming a session whose age is unknown costs
    an unbounded bill.
    """
    if not stamp:
        return True
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > timedelta(hours=hours)


class Sessions:
    """chat -> named Claude sessions, plus message id -> which one answered.

    The second map is the whole threading story. WhatsApp has no threads, so a
    quoted reply is the one affordance it gives: an inbound message carrying
    `replyTo.id` routes to whatever session produced that message and makes it
    current, overriding the chat's pointer. Outbound ids are recorded for
    exactly that reason -- quoting the bot's own answer is the common case.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("chats", {})
        data.setdefault("messages", {})
        return data

    def _save(self) -> None:
        """Atomic, for the reason in GOTCHAS: a process killed mid-write must
        never leave truncated JSON behind for the next start to choke on."""
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def _prune(self) -> None:
        kept = {k: v for k, v in self.data["messages"].items()
                if not _stale(v.get("at"), MESSAGE_TTL_DAYS * 24)}
        if len(kept) > MESSAGE_CAP:
            newest = sorted(kept.items(), key=lambda kv: kv[1].get("at") or "",
                            reverse=True)[:MESSAGE_CAP]
            kept = dict(newest)
        self.data["messages"] = kept

    def _chat(self, chat: str) -> dict:
        return self.data["chats"].setdefault(
            chat, {"current": DEFAULT_SESSION, "sessions": {}})

    # ---- reads and routing ------------------------------------------------

    def resolve(self, chat: str, reply_to: str = "") -> tuple[str, str, bool, int]:
        """Pick the session for this turn. Returns (name, uuid, first, count).

        `first` decides `--session-id` versus `--resume`, so it is only cleared
        once a turn has actually succeeded against that uuid: a turn that died
        may never have created the session at all.
        """
        with self._lock:
            entry = self._chat(chat)
            routed = self.data["messages"].get(reply_to) if reply_to else None
            if routed and routed.get("chat") == chat and routed.get("session") in entry["sessions"]:
                entry["current"] = routed["session"]

            name = entry["current"]
            session = entry["sessions"].get(name)
            if session is None or _stale(session.get("last"), IDLE_HOURS):
                # Cost control, not hygiene: without it every message pays for
                # the entire accumulated history of the thread.
                if session is not None:
                    log(f"wa_consumer: [{name}] idle past {IDLE_HOURS}h, new session")
                session = {"uuid": str(uuidlib.uuid4()), "last": _now(), "started": False}
                entry["sessions"][name] = session
            session["last"] = _now()
            self._save()
            return name, session["uuid"], not session["started"], len(entry["sessions"])

    def names(self, chat: str) -> tuple[str, list[str]]:
        with self._lock:
            entry = self._chat(chat)
            return entry["current"], sorted(entry["sessions"])

    # ---- writes -----------------------------------------------------------

    def mark_started(self, chat: str, name: str) -> None:
        with self._lock:
            session = self._chat(chat)["sessions"].get(name)
            if session:
                session["started"] = True
                session["last"] = _now()
                self._save()

    def reset(self, chat: str) -> str:
        """`/new`: a fresh uuid under the same name."""
        with self._lock:
            entry = self._chat(chat)
            name = entry["current"]
            entry["sessions"][name] = {"uuid": str(uuidlib.uuid4()),
                                       "last": _now(), "started": False}
            self._save()
            return name

    def switch(self, chat: str, name: str) -> bool:
        """`/s <name>`: switch to a session, creating it if new. True if new."""
        with self._lock:
            entry = self._chat(chat)
            created = name not in entry["sessions"]
            if created:
                entry["sessions"][name] = {"uuid": str(uuidlib.uuid4()),
                                           "last": _now(), "started": False}
            entry["current"] = name
            self._save()
            return created

    def remember_message(self, msg_id: str, chat: str, name: str) -> None:
        if not msg_id:
            return
        with self._lock:
            self.data["messages"][msg_id] = {"chat": chat, "session": name, "at": _now()}
            self._save()


STATE = Sessions(STATE_PATH)


# --- markdown that WhatsApp can actually render ----------------------------

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def to_whatsapp(text: str) -> str:
    """GitHub markdown -> what WhatsApp renders.

    Without this every reply arrives as literal asterisks, hashes and pipe
    soup. Fenced blocks pass through untouched: WhatsApp does render ``` and
    the contents are usually code, where rewriting punctuation is wrong.
    """
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            body = _BOLD.sub(r"\1", heading.group(2).strip())
            out.append(f"*{body}*" if body else "")
            continue
        line = _BULLET.sub(r"\1• ", line)
        out.append(_BOLD.sub(r"*\1*", line))
    return "\n".join(out)


def chunks(text: str, limit: int) -> list[str]:
    """Split on paragraph boundaries, then lines, then characters."""
    parts: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            if cut <= 0:
                cut = para.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            parts.append(para[:cut].rstrip())
            para = para[cut:].lstrip()
        current = para
    if current:
        parts.append(current)
    return [p for p in parts if p.strip()]


def deliver(chat: str, name: str, many_sessions: bool, raw: str, reply_to: str) -> None:
    """Send one Claude answer, in whatever shape WhatsApp can carry.

    Always a quote-reply, so the question renders above the answer and a
    self-chat thread reads as paired rather than flat.
    """
    text = to_whatsapp(raw).strip() or "(the turn produced no text)"
    prefix = f"[{name}] " if many_sessions else ""

    if len(text) > CHUNK_LIMIT:
        # Past this, a message is not a message. Lead line plus the whole
        # thing as a file, which is one sendFile call.
        lead = next((l for l in text.split("\n") if l.strip()), "Full answer attached.")
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M")
        sent = send_text(chat, f"{prefix}{lead[:600]}\n\n(full answer attached)", reply_to)
        STATE.remember_message(sent, chat, name)
        # The attachment keeps the original markdown: it is a .md file, read in
        # something that renders markdown, not in the WhatsApp bubble.
        sent = send_file(chat, f"reply-{stamp}.md", raw, f"{prefix}full answer", reply_to)
        STATE.remember_message(sent, chat, name)
        return

    for part in chunks(text, CHUNK_LIMIT - len(prefix)):
        sent = send_text(chat, prefix + part, reply_to)
        STATE.remember_message(sent, chat, name)


# --- the agent -------------------------------------------------------------

class TurnFailed(Exception):
    pass


def claude_binary() -> str:
    """The box keeps it off the non-login PATH, so prefer the known path.

    Verified 2026-09-17: Claude Code 2.1.220 at /home/<user>/.local/bin/claude,
    which `ssh host command` cannot find because a non-interactive shell never
    sources the profile that adds that directory.
    """
    if sys.platform.startswith("linux"):
        known = Path("/home/<user>/.local/bin/claude")
        if known.exists():
            return str(known)
    found = shutil.which("claude") or str(config.HOME / ".local" / "bin" / "claude")
    if not Path(found).exists():
        raise TurnFailed(f"claude binary not found (looked for {found})")
    return found


def _strip_frontmatter(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:])
    return text                      # unterminated; treat the whole note as body


def reply_style() -> str:
    """The prompt half of reply-style.md, read fresh from the vault every turn.

    The file is an ordinary vault note, not a prompt file: YAML frontmatter, a
    heading, a paragraph of meta about itself, a `---` rule, and only then the
    text meant for the model. Everything above the rule is stripped -- passing
    it through would spend tokens every single turn telling Claude where the
    file lives and why.

    Editing the bridge's voice is editing a vault note, from the phone if he
    wants. Missing or unreadable degrades to no appended prompt rather than
    failing the turn: the vault CLAUDE.md is still loaded either way.
    """
    try:
        raw = REPLY_STYLE.read_text(encoding="utf-8")
    except OSError as exc:
        log(f"wa_consumer: reply-style.md unreadable, continuing without it ({exc})")
        return ""

    body = _strip_frontmatter(raw)
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[i + 1:]).strip()
    # No rule to cut at. The whole note beats nothing, and the log says why.
    log("wa_consumer: reply-style.md has no `---` rule, appending the whole body")
    return body.strip()


def run_turn(text: str, session_uuid: str, first: bool) -> str:
    """One headless Claude turn against the real vault. Raises TurnFailed."""
    cmd = [claude_binary(), "-p", text]
    cmd += ["--session-id", session_uuid] if first else ["--resume", session_uuid]
    cmd += ["--permission-mode", "dontAsk", "--allowedTools", *ALLOWED_TOOLS]
    style = reply_style()
    if style:
        cmd += ["--append-system-prompt", style]

    started = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=TURN_TIMEOUT, cwd=str(config.VAULT))
    except subprocess.TimeoutExpired:
        raise TurnFailed(f"no answer within {TURN_TIMEOUT}s")
    except OSError as exc:
        raise TurnFailed(f"could not start claude: {exc}")

    out = (result.stdout or "").strip()
    if result.returncode != 0:
        raise TurnFailed(f"exit {result.returncode}: {(result.stderr or out)[:400]}")
    if not out:
        raise TurnFailed("empty response")
    log(f"wa_consumer: turn ok, {len(out)} chars in {time.monotonic() - started:.0f}s")
    return out


# --- commands --------------------------------------------------------------

def handle_command(chat: str, body: str, reply_to: str) -> bool:
    """`/new` and `/s`. Matched only when they are the WHOLE message.

    Anything looser would eat a message that merely mentions one, and these are
    plain WhatsApp text with no way to escape them.
    """
    stripped = body.strip()
    if stripped == "/new":
        name = STATE.reset(chat)
        _remember_sent(send_text(chat, f"[{name}] new session started.", reply_to))
        return True
    if stripped == "/s":
        current, names = STATE.names(chat)
        listing = "\n".join(f"• {n}{' (current)' if n == current else ''}"
                            for n in names) or "• (none yet)"
        _remember_sent(send_text(chat, f"*Sessions in this chat*\n{listing}", reply_to))
        return True
    if stripped.startswith("/s "):
        name = stripped[3:].strip()
        if not name:
            return False
        created = STATE.switch(chat, name)
        _remember_sent(send_text(
            chat, f"[{name}] {'created and switched' if created else 'switched'}.", reply_to))
        return True
    return False


# --- failure, which is never allowed to be quiet ---------------------------

_last_mail: dict[str, float] = {}
_mail_lock = threading.Lock()


def report_failure(chat: str, reply_to: str, headline: str, detail: str) -> None:
    """Both channels, in the order that matters most if one of them is broken.

    A timeout or a crash that produced no reply is indistinguishable, from the
    phone, from a message nobody read. That is the outcome this exists to stop.
    """
    log(f"wa_consumer: FAILED, {headline}: {detail}")

    if chat:
        try:
            send_text(chat, f"⚠️ {headline}\n\n`{detail[:600]}`\n\n"
                            f"Nothing was lost — send the message again, or `/new` "
                            f"to start a fresh session.", reply_to)
        except WahaError as exc:
            log(f"wa_consumer: could not deliver the failure notice ({exc})")

    with _mail_lock:
        last = _last_mail.get(headline, 0.0)
        if time.monotonic() - last < MAIL_THROTTLE_S:
            log(f"wa_consumer: mail suppressed, same failure inside {MAIL_THROTTLE_S}s")
            return
        _last_mail[headline] = time.monotonic()

    stamp = datetime.now().astimezone()
    try:
        to = notify.send_urgent(
            f"vault-host: WhatsApp bridge — {headline}",
            f"The WhatsApp bridge failed at {stamp.strftime('%Y-%m-%d %H:%M %Z')}.\n\n"
            f"  chat:    {chat or '(unknown)'}\n"
            f"  problem: {headline}\n\n{detail}\n\n"
            f"A notice was also sent to the chat.\n\n"
            f"    ssh vault-host 'journalctl -u wa-consumer -n 100 --no-pager'\n",
            push_message=f"{headline}. Chat: {chat or 'unknown'}. Details in mail.")
        log(f"wa_consumer: mailed {to}")
    except notify.MailUnavailable as exc:
        log(f"wa_consumer: mail unavailable ({exc})")
    except Exception as exc:
        log(f"wa_consumer: mail FAILED, {type(exc).__name__}: {exc}")


# --- session liveness -------------------------------------------------------
# WAHA emits session.status whenever the WhatsApp link changes state. Without
# this, a dead session is silent and looks exactly like a quiet day — the same
# failure that let the Mac's bridge serve a frozen snapshot for ten weeks.
# Mail is the only channel that works here: if the session is down, the bridge
# cannot tell you over WhatsApp.

STATUS_PATH = config.STATE_DIR / "wa-session-status.json"
HEALTHY_STATUS = "WORKING"
# Statuses that mean the link is gone and only the owner, with the phone, can fix it.
NEEDS_PHONE = {"SCAN_QR_CODE", "UNPAIRED", "FAILED"}


def _write_json_atomic(path: Path, data: dict, **dump_kw) -> None:
    """Atomic, and safe with concurrent writers. Each call gets its own temp
    file in the target directory: WAHA sends session.status in pairs a few ms
    apart, one handler thread each, and a shared fixed `.tmp` name let one
    thread's os.replace move the other's temp away (ENOENT, 16x in a week)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, **dump_kw))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# Serialises read-compare-write on STATUS_PATH. Without it both threads of a
# webhook pair read the same `previous`, both see a transition, and both start
# the debounce, so one real outage could alert twice.
_status_lock = threading.Lock()


def _read_last_status() -> tuple[str, bool]:
    """Returns (status, whether a down-alert was actually sent for it)."""
    try:
        d = json.loads(STATUS_PATH.read_text())
        return d.get("status", ""), bool(d.get("alerted"))
    except Exception:
        return "", False


def _write_last_status(status: str, alerted: bool = False) -> None:
    try:
        _write_json_atomic(STATUS_PATH, {"status": status, "alerted": alerted,
                                         "seen": datetime.now().astimezone().isoformat()})
    except Exception as exc:
        log(f"wa_consumer: could not persist session status ({exc})")


STATUS_DEBOUNCE_S = float(os.environ.get("WA_STATUS_DEBOUNCE_S", "90"))


def _live_status(name: str) -> str:
    """Ask WAHA what the session state actually is now, not what the event said."""
    try:
        return (waha_get(f"sessions/{name}") or {}).get("status", "").upper()
    except Exception as exc:
        log(f"wa_consumer: could not re-read session status ({exc})")
        return ""


def handle_session_status(event: dict) -> None:
    payload = event.get("payload") or {}
    status = (payload.get("status") or "UNKNOWN").upper()
    name = event.get("session") or payload.get("name") or "default"
    with _status_lock:
        previous, was_alerted = _read_last_status()
        tripped = is_tripped()
        if tripped or status != previous:
            _write_last_status(status)

    if tripped:
        # The tripwire stopped this session on purpose. A "bridge DOWN" mail here
        # would bury the alert that matters under one that explains nothing.
        log(f"wa_consumer: session {name!r} is {status} while tripped, no status alert")
        if status == HEALTHY_STATUS:
            _restop_while_tripped(name)
        return

    if status == previous:
        return                      # WAHA repeats these; only transitions matter

    if status == HEALTHY_STATUS:
        log(f"wa_consumer: session {name!r} is WORKING")
        # Only worth a recovery mail if a down-alert actually went out. A planned
        # restart dips through STOPPED and back inside seconds and must stay silent.
        if was_alerted:
            _mail_status(name, status, previous, recovered=True)
        return

    log(f"wa_consumer: session {name!r} is {status} (was {previous or 'unknown'}), "
        f"confirming in {STATUS_DEBOUNCE_S}s before alerting")
    # Debounce. A restart passes through STOPPED and STARTING within seconds, and
    # mailing on those trains you to ignore the channel. Only a state that is
    # still unhealthy after the wait is worth waking someone for.
    time.sleep(STATUS_DEBOUNCE_S)
    current = _live_status(name)
    if current == HEALTHY_STATUS or current == "":
        log(f"wa_consumer: {name!r} settled to {current or 'unknown'}, no alert")
        return
    log(f"wa_consumer: SESSION NOT HEALTHY — {name!r} is still {current}")
    _write_last_status(current, alerted=True)
    _mail_status(name, current, previous, recovered=False)


def _mail_status(name: str, status: str, previous: str, recovered: bool) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    if recovered:
        subject = f"vault-host: WhatsApp bridge recovered ({status})"
        body = (f"The WhatsApp session {name!r} is back to {status} at {stamp}.\n"
                f"It had been {previous}.\n")
    else:
        subject = f"vault-host: WhatsApp bridge DOWN ({status})"
        body = (f"The WhatsApp session {name!r} went to {status} at {stamp} "
                f"(was {previous or 'unknown'}).\n\n"
                f"The bridge cannot reach you over WhatsApp while this is true, "
                f"which is why this is an email.\n\n")
        if status in NEEDS_PHONE:
            body += ("This needs your phone. Re-pairing is not remotely recoverable:\n"
                     "  1. ssh vault-host\n"
                     "  2. ask Claude to fetch a fresh QR from WAHA\n"
                     "  3. WhatsApp > Settings > Linked Devices > Link a Device\n\n")
        body += "    ssh vault-host 'journalctl -u wa-consumer -n 100 --no-pager'\n"
    try:
        # Down pushes, because the bridge cannot reach him over WhatsApp while
        # it is down. Recovered is good news that can wait for mail.
        if recovered:
            to = notify.send_mail(subject, body)
        else:
            to = notify.send_urgent(
                subject, body,
                push_message=f"WhatsApp session {name!r} is {status}. The bridge cannot "
                             f"reach you over WhatsApp until it recovers."
                             + (" Needs your phone to re-pair." if status in NEEDS_PHONE else ""))
        log(f"wa_consumer: mailed {to}")
    except notify.MailUnavailable as exc:
        log(f"wa_consumer: status mail unavailable ({exc})")
    except Exception as exc:
        log(f"wa_consumer: status mail FAILED, {type(exc).__name__}: {exc}")


# --- the ban tripwire -------------------------------------------------------
# whatsapp-source.md safeguard 2: WhatsApp generally warns before it bans, and
# reported bans land weeks after the flag, so the warning is real advance
# notice. It is also the highest-value safeguard available here, because it
# converts a permanent, unappealable loss into a recoverable one -- and it is
# worth more than the choice of library or any ban estimate.
#
# Two things make catching it non-obvious:
#
#   1. **The warning is not allowlisted and never will be.** It comes from
#      WhatsApp, not from the owner's number, so `handle_message` dropped it in
#      silence as an unknown sender. The safeguard designed to save the account
#      was the one thing guaranteed not to reach him. The screen therefore runs
#      *before* the allowlist.
#   2. **The sender JID is not known.** whatsmeow #810 has the wording (via a
#      Portuguese screenshot) but never says where it arrives from, and the
#      widely repeated `0@s.whatsapp.net` answer does not survive checking. So
#      content is the trigger and the sender is only a hint -- the reverse of
#      how an allowlist exception would normally be written.
#
# On a trip the bridge goes off WhatsApp and stays off. It deliberately does
# NOT send a WhatsApp message to say so: the one thing you do not do on an
# account that was just flagged for automation is keep automating on it. Mail
# is the channel, exactly as for session liveness.

TRIP_PATH = config.STATE_DIR / "wa-tripwire.json"
STANDING_PATH = config.STATE_DIR / "wa-account-standing.json"

# Matching is accent-folded and lower-cased, so the patterns are written without
# accents: "gefahrdet" matches "gefährdet", "nao autorizadas" matches "não".
# A trip needs one term from EACH set. Either alone is ordinary language a real
# contact might use; the pair is not. English, German, Portuguese (the language
# of the only screenshot that exists) and Spanish.
_TRIP_RISK = (
    "at risk", "be banned", "being banned", "banned", "suspended",
    "gefahrdet", "gesperrt", "sperrung",
    "em risco", "banida", "banido", "suspensa",
    "en riesgo", "suspendida", "prohibida",
)
_TRIP_CAUSE = (
    "unauthorized tools", "unauthorised tools", "unauthorized third-party",
    "third-party tools", "third party tools", "terms of service",
    "nicht autorisierte tools", "nicht autorisierten tools",
    "drittanbieter", "nutzungsbedingungen",
    "ferramentas nao autorizadas", "termos de servico",
    "herramientas no autorizadas", "terminos del servicio",
)

# Not a whitelist of senders -- see note 2 above. Only a hint that lifts a
# non-matching message from "dropped, not allowlisted" to something worth
# mailing about, because on a bridge that talks to one self-chat, a message
# from WhatsApp's own infrastructure is rare and interesting by itself.
_SYSTEM_USERS = {"0"}


def _fold(text: str) -> str:
    """Lower-case and strip combining marks, so patterns need no accents."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def is_system_sender(jid: str) -> bool:
    user = (jid or "").split("@")[0].split(":")[0]
    return user in _SYSTEM_USERS


def looks_like_ban_warning(body: str) -> bool:
    folded = _fold(body)
    return (any(term in folded for term in _TRIP_RISK)
            and any(term in folded for term in _TRIP_CAUSE))


def _read_trip() -> dict:
    try:
        return json.loads(TRIP_PATH.read_text())
    except Exception:
        return {}


def is_tripped() -> bool:
    return bool(_read_trip().get("tripped"))


_trip_lock = threading.Lock()


def trip(headline: str, detail: str) -> None:
    """Latch, go off WhatsApp, then mail. In that order, and idempotent.

    The latch is written *before* the session is stopped so that the
    `session.status` handler, which is about to see a state change it did not
    cause, can tell a deliberate stop from a real outage and stay quiet.
    """
    with _trip_lock:
        if is_tripped():
            log(f"wa_consumer: already tripped, not re-alerting ({headline})")
            return
        stamp = datetime.now().astimezone().isoformat()
        try:
            _write_json_atomic(TRIP_PATH, {"tripped": True, "headline": headline,
                                           "detail": detail[:2000], "at": stamp},
                               indent=2)
        except Exception as exc:
            # Losing the latch must not stop the session drop or the mail. A
            # tripwire that fails to fire because it could not write a file is
            # the silent-partial shape this whole design keeps guarding against.
            log(f"wa_consumer: TRIPPED but could not persist the latch ({exc})")
        log(f"wa_consumer: BAN TRIPWIRE FIRED -- {headline}")
        stopped = stop_session("tripwire")
        _mail_trip(headline, detail, stopped, stamp)


def stop_session(why: str) -> str:
    """Stop, never log out. Stop keeps the pairing; logout needs the phone.

    Two endpoint shapes are tried because this WAHA's OpenAPI spec sits behind
    basic auth and could not be read: current WAHA takes the session in the
    path, older builds took it in the body.
    """
    attempts = (
        (f"sessions/{WAHA_SESSION}/stop", {}),
        ("sessions/stop", {"name": WAHA_SESSION, "logout": False}),
    )
    errors = []
    for path, payload in attempts:
        try:
            waha(path, payload)
            log(f"wa_consumer: stopped WAHA session {WAHA_SESSION!r} via {path} ({why})")
            return path
        except WahaError as exc:
            errors.append(f"{path}: {exc}")
    log(f"wa_consumer: COULD NOT STOP the session ({'; '.join(errors)})")
    return ""


def _restop_while_tripped(name: str) -> None:
    """WAHA restores previously running sessions when its container restarts
    (`WAHA_WORKER_RESTART_SESSIONS` defaults on), so a reboot would quietly put
    a flagged account back online. The latch outlives the restart; re-stop it.
    """
    log(f"wa_consumer: session {name!r} came back WORKING while tripped, stopping again")
    stop_session("still tripped")


def _mail_trip(headline: str, detail: str, stopped: str, stamp: str) -> None:
    subject = "vault-host: WhatsApp BAN TRIPWIRE — bridge stopped"
    body = (
        f"{headline}\n\n"
        f"Fired at {stamp}.\n\n"
        f"The WhatsApp session was {'STOPPED' if stopped else 'NOT stopped — see below'}. "
        f"The bridge will refuse to send or process anything until the latch is cleared.\n\n"
        f"Nothing was sent over WhatsApp about this on purpose: continuing to automate "
        f"on an account that was just flagged for automation is the one move that turns a "
        f"warning into a ban.\n\n"
        f"--- what triggered it ---\n{detail[:1500]}\n\n"
    )
    if not stopped:
        body += ("THE SESSION COULD NOT BE STOPPED AUTOMATICALLY. Do it by hand now:\n"
                 f"    ssh vault-host 'curl -s -X POST -H \"X-Api-Key: $WA_API_KEY\" "
                 f"http://127.0.0.1:3000/api/sessions/{WAHA_SESSION}/stop'\n"
                 "  or, blunter:  ssh vault-host 'sudo systemctl stop waha'\n\n")
    body += (
        "--- decide before restarting ---\n"
        "Read _system/whatsapp-source.md § Ban risk. The warning is advance notice, not a\n"
        "ban: reported bans land weeks after the flag. Staying off the number is what\n"
        "spends that notice well.\n\n"
        "To clear the latch and come back online, deliberately:\n"
        f"    ssh vault-host 'rm {TRIP_PATH}'\n"
        "    ssh vault-host 'sudo systemctl restart wa-consumer'\n"
        f"    then start the session: POST /api/sessions/{WAHA_SESSION}/start\n\n"
        "    ssh vault-host 'journalctl -u wa-consumer -n 200 --no-pager'\n"
    )
    try:
        # Push is not WhatsApp, so it does not break the rule above.
        log(f"wa_consumer: mailed " + notify.send_urgent(
            subject, body, priority=1,
            push_message="WhatsApp appears to have warned the account about unauthorized "
                         "tools. Bridge " + ("stopped." if stopped else
                                             "could NOT be stopped, stop it by hand now.")
                         + " Stay off automation until you decide. Details in mail."))
    except notify.MailUnavailable as exc:
        log(f"wa_consumer: TRIP MAIL UNAVAILABLE ({exc}) — the trip is only in the journal")
    except Exception as exc:
        log(f"wa_consumer: TRIP MAIL FAILED, {type(exc).__name__}: {exc}")


_notice_lock = threading.Lock()
_last_notice = 0.0


def _notice_system_message(chat: str, body: str) -> None:
    """A message from WhatsApp's own infrastructure that did NOT match the
    warning patterns. Worth telling him about, not worth stopping the bridge
    for -- and throttled, because a channel that nags gets muted.
    """
    global _last_notice
    with _notice_lock:
        if time.monotonic() - _last_notice < MAIL_THROTTLE_S:
            log(f"wa_consumer: system message from {chat}, notice throttled")
            return
        _last_notice = time.monotonic()
    log(f"wa_consumer: system message from {chat}, mailing a notice")
    try:
        notify.send_mail(
            "vault-host: WhatsApp system message received",
            f"A message arrived from {chat}, which looks like WhatsApp's own "
            f"infrastructure rather than a person.\n\n"
            f"It did NOT match the ban-warning patterns, so the bridge is still "
            f"running and the session was not stopped. Read it on your phone and "
            f"judge for yourself.\n\n--- body ---\n```\n{body[:1200]}\n```\n")
    except Exception as exc:
        log(f"wa_consumer: system-message notice failed ({exc})")


def screen_inbound(chat: str, body: str) -> bool:
    """Runs on every inbound message before the allowlist. True means handled.

    Only non-allowlisted senders can trip. That is not a technicality: the owner
    discusses ban risk with the vault *through this bridge*, and a message of
    his own containing the warning's wording must never stop his own bridge.
    WhatsApp is definitionally not on the allowlist, so nothing is lost.
    """
    if allowed(chat):
        return False
    if looks_like_ban_warning(body):
        trip("WhatsApp appears to have warned this account about unauthorized tools.",
             f"From: {chat}\n\n{body[:1200]}")
        return True
    if is_system_sender(chat):
        _notice_system_message(chat, body)
        return True
    return False


# --- account standing, asked rather than waited for -------------------------
# The session payload carries WhatsApp's own view of the account:
# `me.messageCapping.cappingStatus` (NONE / FIRST_WARNING / CAPPED) and
# `me.reachoutTimelock`. This is the only first-party, structured account-health
# signal in any of this research, and it costs one GET.
#
# Be honest about its reach: both fields govern messaging *new contacts*, which
# is the axis a self-chat bridge is not on, so they will most likely read NONE
# forever. They are wired anyway because FIRST_WARNING would be exactly the kind
# of advance notice safeguard 2 is about, and because a signal that costs one
# GET every six hours does not need to be likely to be worth having.
#
# It mails and does not trip. A send quota is recoverable and is not evidence of
# the permanent ban the tripwire exists for; conflating the two would train him
# to ignore the louder alert.

STANDING_POLL_S = float(_env("WA_STANDING_POLL_S", "21600"))
_STANDING_OK = {"", "NONE", "UNKNOWN"}


def _read_standing() -> str:
    try:
        return str(json.loads(STANDING_PATH.read_text()).get("fingerprint", ""))
    except Exception:
        return ""


def _write_standing(fingerprint: str) -> None:
    try:
        _write_json_atomic(STANDING_PATH, {"fingerprint": fingerprint,
                                           "seen": datetime.now().astimezone().isoformat()})
    except Exception as exc:
        log(f"wa_consumer: could not persist account standing ({exc})")


def check_standing() -> None:
    """One GET. Mails only when the answer changes, never on every poll."""
    try:
        me = (waha_get(f"sessions/{WAHA_SESSION}") or {}).get("me") or {}
    except WahaError as exc:
        log(f"wa_consumer: could not read account standing ({exc})")
        return
    capping = me.get("messageCapping") or {}
    status = str(capping.get("cappingStatus") or "").upper()
    timelock = me.get("reachoutTimelock")
    fingerprint = f"{status}|{timelock!r}"
    if fingerprint == _read_standing():
        return
    _write_standing(fingerprint)
    healthy = status in _STANDING_OK and timelock in (None, "", 0)
    if healthy:
        log(f"wa_consumer: account standing clear (capping {status or 'unset'})")
        return
    log(f"wa_consumer: ACCOUNT STANDING CHANGED — capping {status}, timelock {timelock!r}")
    try:
        notify.send_urgent(
            f"vault-host: WhatsApp account standing changed ({status or 'timelock'})",
            f"WhatsApp's own view of the account moved off clear:\n\n"
            f"  messageCapping.cappingStatus : {status or '(unset)'}\n"
            f"  reachoutTimelock             : {timelock!r}\n"
            f"  quota used / total           : {capping.get('usedQuota')} / "
            f"{capping.get('totalQuota')}\n\n"
            f"These govern messaging NEW contacts, so a self-chat bridge should never "
            f"move them. That it moved is the interesting part.\n\n"
            f"The bridge was NOT stopped: a send quota is recoverable and is not the "
            f"permanent-ban signal the tripwire watches for. Read "
            f"_system/whatsapp-source.md § Ban risk before changing anything.\n",
            push_message=f"WhatsApp's view of the account moved off clear (capping "
                         f"{status or 'unset'}, timelock {timelock!r}). Bridge still running. "
                         f"Details in mail.")
    except Exception as exc:
        log(f"wa_consumer: standing mail failed ({exc})")


def standing_poller() -> None:
    while True:
        if not is_tripped():
            check_standing()
        time.sleep(STANDING_POLL_S)


# --- one inbound message ---------------------------------------------------

_chat_locks: dict[str, threading.Lock] = {}
_chat_locks_guard = threading.Lock()


def chat_lock(chat: str) -> threading.Lock:
    """One lock per chat. Turns queue; they are never dropped.

    Two Claude processes on one `--session-id` corrupt the session, and two
    writing the vault at once reintroduces the two-writer gap. WhatsApp
    delivers one stream anyway, so serialising costs nothing real.
    """
    with _chat_locks_guard:
        return _chat_locks.setdefault(chat, threading.Lock())


def allowed(sender: str) -> bool:
    """Compared raw and digits-only, so the allowlist can be written either as
    `+<PHONE>` or as a full `<PHONE>@c.us` JID."""
    if sender in ALLOWLIST:
        return True
    digits = re.sub(r"\D", "", sender.split("@")[0])
    return bool(digits) and any(digits == re.sub(r"\D", "", a.split("@")[0])
                                for a in ALLOWLIST)


def handle_message(payload: dict) -> None:
    msg_id = str(payload.get("id") or "")
    chat = str(payload.get("from") or "")
    reply_to = str((payload.get("replyTo") or {}).get("id") or "")
    body = payload.get("body") or ""

    if is_echo(msg_id):
        return

    # Before the allowlist, deliberately: the message this exists to catch comes
    # from WhatsApp itself and is therefore never allowlisted.
    if screen_inbound(chat, body):
        return
    if is_tripped():
        log(f"wa_consumer: tripped, ignoring message from {chat or '(no sender)'}")
        return

    if not chat or not allowed(chat):
        # Dropped in silence on purpose: an unknown sender learns nothing.
        log(f"wa_consumer: dropped message from {chat or '(no sender)'}, not allowlisted")
        return

    mark_read(chat, msg_id)

    if not body.strip():
        # `hasMedia: true` with no caption is a real state and media is not
        # handled in v1. Saying so beats answering nothing.
        if payload.get("hasMedia"):
            _remember_sent(send_text(
                chat, "I can't read attachments yet — send it as text.", msg_id))
        return

    with chat_lock(chat):
        if handle_command(chat, body, msg_id):
            return

        name, session_uuid, first, count = STATE.resolve(chat, reply_to)
        STATE.remember_message(msg_id, chat, name)
        log(f"wa_consumer: [{name}] turn for {chat}, "
            f"{'new' if first else 'resumed'} session {session_uuid[:8]}")

        # Typing stops before anything else is sent, success or not, so the
        # indicator never outlives the turn it belongs to.
        start_typing(chat)
        failure = ""
        answer = ""
        try:
            answer = run_turn(body, session_uuid, first)
        except TurnFailed as exc:
            failure = str(exc)
        finally:
            stop_typing(chat)

        if failure:
            report_failure(chat, msg_id, "a turn did not complete", failure)
            return

        STATE.mark_started(chat, name)
        try:
            deliver(chat, name, count > 1, answer, msg_id)
        except WahaError as exc:
            report_failure(chat, msg_id, "the answer could not be delivered", str(exc))


def guarded(payload: dict) -> None:
    """The worker thread's outermost catch. Nothing gets to die quietly."""
    try:
        handle_message(payload)
    except Exception as exc:
        import traceback
        report_failure(str(payload.get("from") or ""), str(payload.get("id") or ""),
                       f"the consumer crashed ({type(exc).__name__})",
                       traceback.format_exc()[-1500:])


# --- HTTP ------------------------------------------------------------------

def verify_hmac(raw: bytes, header: str) -> bool:
    """SHA512 of the raw body, constant-time compared."""
    expected = hmac.new(HMAC_KEY.encode("utf-8"), raw, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, (header or "").strip())


class Handler(BaseHTTPRequestHandler):
    server_version = "wa-consumer/1"

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/webhook":
            self._respond(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""

        if not verify_hmac(raw, self.headers.get("X-Webhook-Hmac", "")):
            log("wa_consumer: rejected a webhook, HMAC mismatch")
            self._respond(401, "bad signature")
            return

        # 200 first, always. A turn takes 10 to 90 seconds and WAHA wants the
        # ack now; blocking the response on the turn is how webhooks get retried
        # and one message becomes three.
        self._respond(200, "ok")

        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            log(f"wa_consumer: unparseable webhook body ({exc})")
            return
        # "message" is inbound-only, so a self-chat (where every message is fromMe)
        # never emits it. WAHA is configured for "message.any"; echo suppression
        # below is what stops the bridge answering its own replies.
        if event.get("event") == "session.status":
            threading.Thread(target=handle_session_status, args=(event,),
                             daemon=True).start()
            return
        if event.get("event") not in ("message", "message.any"):
            log(f"wa_consumer: ignoring event {event.get('event')!r}")
            return
        threading.Thread(target=guarded, args=(event.get("payload") or {}, ),
                         daemon=True).start()

    def _respond(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        """Into journald with everything else. No job defines its own log file."""
        log(f"wa_consumer: http {fmt % args}")


# --- entry point -----------------------------------------------------------

def single_instance():
    """flock, the same way runner.job does it.

    Two consumers on one WAHA session is the shape that costs the number:
    connection churn is the one documented mechanism behind a ban.
    """
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def main() -> int:
    config.ensure_dirs()

    # Fail closed. An unset HMAC key makes verification meaningless and an empty
    # allowlist opens a vault-writing agent to anyone who can reach the port.
    missing = []
    if not HMAC_KEY:
        missing.append("WA_HMAC_KEY (webhooks could not be verified)")
    if not ALLOWLIST:
        missing.append("WA_ALLOWLIST (every sender would be accepted)")
    if missing:
        for item in missing:
            log(f"wa_consumer: refusing to start, missing {item}")
        return 1
    if not API_KEY:
        log("wa_consumer: WA_API_KEY is unset; WAHA will reject every send")
    if not REPLY_STYLE.exists():
        log(f"wa_consumer: {REPLY_STYLE} missing, turns run without the reply style")

    lock = single_instance()
    if lock is None:
        log(f"wa_consumer: another consumer holds {LOCK_PATH}, exiting")
        return 1

    if is_tripped():
        state = _read_trip()
        log("wa_consumer: THE BAN TRIPWIRE IS LATCHED — no messages will be "
            f"processed and nothing will be sent. Fired {state.get('at')}: "
            f"{state.get('headline')}")
        log(f"wa_consumer: clear it deliberately with: rm {TRIP_PATH}")
    else:
        threading.Thread(target=standing_poller, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log(f"wa_consumer: listening on {HOST}:{PORT}/webhook, vault {config.VAULT}, "
        f"WAHA {BASE_URL} session {WAHA_SESSION}, {len(ALLOWLIST)} allowlisted")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("wa_consumer: stopping")
    finally:
        server.server_close()
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
