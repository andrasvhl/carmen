"""Long-lived: the Telegram bridge daemon. getUpdates in, Claude Agent SDK turns out.

Like wa_consumer, a daemon and not a Job: no exit-code contract and no digest
state, but everything the runner already got right is reused -- runner.config
for paths and credentials, runner.job.log for journald-only logging,
runner.notify for the alert path, and fcntl locking so two of these can never
poll one token (which Telegram answers with 409 forever).

The #1 invariant, which every report on OpenClaw named: **never two agent runs
on one session.** Each lane (the private chat, or one topic in it) has exactly
one worker task, and the agent pool holds a per-session lock for the whole turn
on top of that. Messages that arrive mid-turn are collected into the next turn;
rapid messages merge (300 ms); /stop and the stop words bypass all of it.

Configuration is environment, falling back to ~/.automation.env:

    TELEGRAM_BOT_TOKEN   required. From @BotFather
    TELEGRAM_OWNER_ID    required. The owner's numeric user id; everyone else is dropped
    GROQ_API_KEY         voice transcription, first choice
    OPENAI_API_KEY       voice transcription, fallback
    TG_MODEL             model alias for the agent, default: the CLI's own default
    TG_VT_RULES          ";"-separated allow rules replacing the two vt forms
    TG_STATUS_LINE       "0" turns the status line off (decision 5: on)
"""

import asyncio
import fcntl
import json
import os
import signal
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from runner import config, notify
from runner.job import log

from . import agent, commands, env, media, proactive, render
from .agent import AgentPool, AuthFailed, Stalled
from .approvals import Approvals
from .inbox import DEAD, DONE, Inbox, Row, topic_of
from .sessions import SessionStore
from .telegram import (Bot, FatalPollError, Poller, PollStalled, TelegramError,
                       stall_action)

DB_PATH = config.STATE_DIR / "tg-bridge.sqlite"
LOCK_PATH = config.LOCK_DIR / "tg_bridge.lock"
REPLY_STYLE = config.VAULT / "_system" / "telegram-bridge" / "reply-style.md"

EX_CONFIG = 78           # systemd: RestartPreventExitStatus, a bad config never crash-loops

# Inbound debounce (research-2 item 3): a quiet window, capped at 5x from the
# first message. Albums arrive as several updates; a long paste is split by
# Telegram into 4096-char messages that need a longer window to land.
DEBOUNCE_S = 0.3
ALBUM_WINDOW_S = 0.5
SPLIT_PASTE_WINDOW_S = 1.5
DEBOUNCE_CAP = 5
QUEUE_CAP = 20                 # collected messages kept for the next turn

TYPING_EVERY_S = 4.0
TYPING_MAX_S = 15 * 60         # hard stop: never "types forever"
STATUS_DELAY_S = 1.5
STATUS_MIN_GAP_S = 1.0
SHUTDOWN_GRACE_S = 120
MAIL_THROTTLE_S = 600

AUTH_MESSAGE = ("Can't reach Claude: the box needs <code>claude auth login</code>. "
                "Nothing was run. claude-auth-watch will push about it too.")


# --- the status line --------------------------------------------------------

def tool_label(name: str, data: dict) -> str:
    """What the status line says for a tool call. Plain text, short."""
    data = data or {}
    if name == "Read":
        return f"Reading {Path(str(data.get('file_path', ''))).name or 'a file'}"
    if name in ("Write", "Edit", "MultiEdit"):
        return f"Writing {Path(str(data.get('file_path', ''))).name or 'a file'}"
    if name == "Grep":
        return f"Searching for “{str(data.get('pattern', ''))[:30]}”"
    if name == "Glob":
        return "Listing files"
    if name == "Bash":
        words = str(data.get("command", "")).split()
        for i, w in enumerate(words):
            if w.endswith("/bin/vt") or w == "vt":
                return "Running vt " + (words[i + 1] if i + 1 < len(words) else "")
        return f"Running {Path(words[0]).name}" if words else "Running a command"
    if name == "Skill":
        return f"Using skill {data.get('skill') or data.get('command') or ''}".strip()
    if name in ("Task", "Agent"):
        return "Delegating to a subagent"
    if name == "WebSearch":
        return "Searching the web"
    if name == "WebFetch":
        return "Fetching a page"
    if name.startswith("mcp__"):
        return "Using " + name.split("__", 2)[-1].replace("_", " ")
    return f"Using {name}"


class StatusLine:
    """One message, edited at most once a second, deleted when the answer lands.

    Sent only once a tool is actually running and STATUS_DELAY_S has passed, so
    a quick answer never flashes a status first. Every send and edit is
    droppable: under a rate limit the answer matters, the status does not.
    """

    def __init__(self, bot: Bot, chat_id: int, thread_id: int, *, enabled: bool = True,
                 delay: float = STATUS_DELAY_S, gap: float = STATUS_MIN_GAP_S):
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.enabled = enabled
        self.delay = delay
        self.gap = gap
        self.labels: list[str] = []
        self.message_id: int | None = None
        self._shown = ""
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()

    async def on_event(self, event: agent.Event) -> None:
        if not self.enabled or event.kind != "tool":
            return
        label = tool_label(event.name, event.data)
        if not self.labels or self.labels[-1] != label:
            self.labels = (self.labels + [label])[-3:]
        self._changed.set()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    def text(self) -> str:
        return "⏳ " + " · ".join(self.labels)

    async def _run(self) -> None:
        await asyncio.sleep(self.delay)
        while True:
            await self._changed.wait()
            self._changed.clear()
            text = self.text()
            if text != self._shown:
                try:
                    if self.message_id is None:
                        sent = await self.bot.call("sendMessage", {
                            "chat_id": self.chat_id, "text": text,
                            "message_thread_id": self.thread_id or None,
                            "disable_notification": True}, droppable=True)
                        if sent:
                            self.message_id = sent.get("message_id")
                    else:
                        await self.bot.edit_message_text(self.chat_id, self.message_id, text,
                                                         parse_mode=None, droppable=True)
                    self._shown = text
                except TelegramError as exc:
                    log(f"tg_bridge: status line update failed ({exc.code})")
            await asyncio.sleep(self.gap)

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        if self.message_id:
            await self.bot.delete_message(self.chat_id, self.message_id)
            self.message_id = None


# Keys that make a message something to answer. Everything else in the private
# chat is a service message: topic created/edited/closed, pinned, and so on.
CONTENT_KEYS = ("text", "caption", "photo", "document", "voice", "audio", "video",
                "video_note", "sticker", "animation", "location", "contact", "poll")


def has_content(msg: dict) -> bool:
    return any(msg.get(k) for k in CONTENT_KEYS)


# --- lanes --------------------------------------------------------------------

@dataclass
class Item:
    row: Row
    queued: bool = False             # arrived while a turn was running (collect mode)
    command: commands.Command | None = None
    text_override: str | None = None


class Lane:
    """One private chat, or one topic in it. Exactly one worker task.

    The worker is the serialization: it takes a batch, runs one turn, and only
    then looks at what arrived meanwhile. Nothing else ever calls the agent.
    """

    def __init__(self, bridge: "Bridge", chat_id: int, thread_id: int):
        self.bridge = bridge
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.pending: list[Item] = []
        self.arrived = asyncio.Event()
        self.first_at = 0.0
        self.last_at = 0.0
        self.busy_since: float | None = None
        self.session_id: int | None = None
        self.stop_requested = False
        self.idle = asyncio.Event()
        self.idle.set()
        self.task = asyncio.create_task(self._loop())

    def submit(self, item: Item) -> None:
        now = time.monotonic()
        if not self.pending:
            self.first_at = now
        self.last_at = now
        item.queued = item.queued or self.busy_since is not None
        self.pending.append(item)
        self.idle.clear()
        if len(self.pending) > QUEUE_CAP:
            dropped = self.pending.pop(0)
            self.bridge.inbox.finish([dropped.row.update_id], DEAD, "queue overflow")
            log(f"tg_bridge: lane queue over {QUEUE_CAP}, dropped the oldest")
        self.arrived.set()

    def window(self) -> float:
        b = self.bridge
        msgs = [it.row.message for it in self.pending]
        if any(render.utf16_len(m.get("text") or "") >= 4000 for m in msgs):
            return b.split_paste_window
        if any(m.get("media_group_id") for m in msgs):
            return b.album_window
        return b.debounce

    async def _debounce(self) -> None:
        while self.pending:
            if self.pending[0].command:
                return                            # commands do not wait
            window = self.window()
            now = time.monotonic()
            quiet = now - self.last_at
            if quiet >= window or now - self.first_at >= window * DEBOUNCE_CAP:
                return
            await asyncio.sleep(min(window - quiet, window * DEBOUNCE_CAP - (now - self.first_at)))

    def take_batch(self) -> list[Item]:
        if not self.pending:
            return []
        if self.pending[0].command:
            return [self.pending.pop(0)]
        n = next((i for i, it in enumerate(self.pending) if it.command), len(self.pending))
        batch, self.pending = self.pending[:n], self.pending[n:]
        return batch

    async def _loop(self) -> None:
        while True:
            await self.arrived.wait()
            await self._debounce()
            batch = self.take_batch()
            if not self.pending:
                self.arrived.clear()
            if not batch:
                self.idle.set()             # a /stop emptied the queue under us
                continue
            self.idle.clear()
            try:
                if batch[0].command:
                    await self.bridge.run_control(self, batch[0])
                else:
                    await self.bridge.run_turn(self, batch)
            except Exception as exc:
                await self.bridge.report_failure(
                    self.chat_id, self.thread_id, batch[-1].row.message_id,
                    f"the bridge crashed ({type(exc).__name__})", traceback.format_exc()[-1500:])
                self.bridge.inbox.finish([it.row.update_id for it in batch], DONE, "crashed")
            finally:
                if not self.pending:
                    self.idle.set()


# --- the bridge -------------------------------------------------------------

class Bridge:
    def __init__(self, bot: Bot, inbox: Inbox, sessions: SessionStore, pool: AgentPool,
                 approvals: Approvals, transcriber: media.Transcriber, *, owner_id: int,
                 vault: Path, status_line: bool = True, mail: bool = True,
                 debounce: float = DEBOUNCE_S, album_window: float = ALBUM_WINDOW_S,
                 split_paste_window: float = SPLIT_PASTE_WINDOW_S,
                 status_delay: float = STATUS_DELAY_S):
        self.bot = bot
        self.inbox = inbox
        self.sessions = sessions
        self.pool = pool
        self.approvals = approvals
        self.transcriber = transcriber
        self.owner_id = owner_id
        self.vault = vault
        self.fixes = proactive.FixLog()
        self.brief_buttons = proactive.BriefButtons(bot, owner_id, vault=vault,
                                                    start_fix=self.start_fix)
        self.status_line = status_line
        self.mail = mail
        self.debounce = debounce
        self.album_window = album_window
        self.split_paste_window = split_paste_window
        self.status_delay = status_delay
        self.lanes: dict[tuple[int, int], Lane] = {}
        self._background: set[asyncio.Task] = set()
        self._last_mail: dict[str, float] = {}

    @property
    def offset_key(self) -> str:
        return f"{self.bot.bot_id}:{self.bot.fingerprint}"

    def next_offset(self) -> int | None:
        return self.inbox.next_offset(self.offset_key)

    def lane(self, chat_id: int, thread_id: int) -> Lane:
        key = (chat_id, thread_id)
        if key not in self.lanes:
            self.lanes[key] = Lane(self, chat_id, thread_id)
        return self.lanes[key]

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def settle(self) -> None:
        """Tests and shutdown: wait until background work and every lane are idle."""
        while True:
            if self._background:
                await asyncio.gather(*list(self._background), return_exceptions=True)
            lanes = list(self.lanes.values())
            await asyncio.gather(*(l.idle.wait() for l in lanes))
            await asyncio.sleep(0)
            if not self._background and all(l.idle.is_set() and not l.pending for l in lanes):
                return

    # ---- startup -----------------------------------------------------------

    async def start(self) -> None:
        me = await self.bot.get_me()
        log(f"tg_bridge: bot @{me.get('username')} ({self.bot.bot_id}), owner {self.owner_id}, "
            f"vault {self.vault}")
        await self.bot.delete_webhook()
        try:
            await self.bot.set_my_commands(commands.MENU)
        except TelegramError as exc:
            log(f"tg_bridge: setMyCommands failed ({exc.code}), continuing")
        self.sessions.clear_warm_flags()
        await self.recover()

    async def recover(self) -> None:
        """Never replay a turn that reached the agent; say so instead (OpenClaw's rule).

        A half-run tool may already have sent the mail or moved the file, so a
        replay could do it twice. Pending rows never reached the agent and are
        simply processed.
        """
        pending, adopted = self.inbox.recover()
        for row in adopted:
            msg = row.message
            said = msg.get("text") or msg.get("caption") or ""
            if row.transcript:
                said = f"(voice) {row.transcript}"
            elif msg.get("voice"):
                said = "(a voice note)"
            elif not said:
                said = "(an attachment)"
            if len(said) > 600:
                said = said[:600] + "…"
            chat = row.chat_id
            if chat and self._is_owner(msg):
                try:
                    await self.bot.send_message(
                        chat, f"I was interrupted by a restart. Your message was: “{said}”. "
                              f"Resend it if you still want it.", parse_mode=None,
                        thread_id=row.thread_id or None, reply_to=row.message_id)
                except TelegramError as exc:
                    log(f"tg_bridge: restart notice failed ({exc.code})")
            self.inbox.finish([row.update_id], DEAD, "interrupted by restart")
        if adopted:
            log(f"tg_bridge: {len(adopted)} message(s) were mid-turn at restart, notified")
        for row in pending:
            await self.dispatch(row)

    # ---- intake --------------------------------------------------------------

    async def on_updates(self, updates: list[dict]) -> None:
        """Persist first (the commit point), then dispatch what was new."""
        for row in self.inbox.store(updates, self.offset_key):
            await self.dispatch(row)

    def _is_owner(self, msg: dict) -> bool:
        sender = msg.get("from") or {}
        chat = msg.get("chat") or {}
        return (sender.get("id") == self.owner_id and not sender.get("is_bot")
                and chat.get("type") == "private")

    async def dispatch(self, row: Row) -> None:
        if row.kind == "callback_query":
            await self.on_callback(row)
            return
        if row.kind != "message":
            self.inbox.finish([row.update_id], DEAD, "unsupported update")
            return
        msg = row.message
        if not self._is_owner(msg):
            # Silence on purpose: an unknown sender learns nothing, not even
            # that the bot is alive.
            self.inbox.finish([row.update_id], DEAD, "not the owner")
            log("tg_bridge: dropped an update that is not from the owner's private chat")
            return
        if not has_content(msg):
            # Service messages carry no words or media: "topic created" arrives
            # before the first message of every new topic (iPhone opens a topic
            # per message sent outside one), and answering it produced a reply
            # about an empty message, 2026-10-03.
            self.inbox.finish([row.update_id], DONE, "service message")
            return
        lane = self.lane(row.chat_id, row.thread_id)
        text = msg.get("text") or ""
        if commands.is_stop(text, self.bot.username):
            self._spawn(self.stop(lane, row))
            return
        cmd = commands.parse(text, self.bot.username)
        if cmd:
            await self.command(lane, row, cmd)
            return
        reply = msg.get("reply_to_message") or {}
        if reply.get("message_id"):
            routed = self.sessions.session_for_message(row.chat_id, reply["message_id"])
            if routed and routed.thread_id == row.thread_id:
                self.sessions.switch_to(routed)
        lane.submit(Item(row))

    async def on_callback(self, row: Row) -> None:
        cq = row.raw.get("callback_query") or {}
        try:
            if await self.approvals.handle_callback(cq):
                return
            if await self.brief_buttons.handle_callback(cq):
                return
            target = commands.parse_session_callback(cq.get("data") or "")
            if target is None or (cq.get("from") or {}).get("id") != self.owner_id:
                await self.bot.answer_callback_query(cq.get("id", ""))
                return
            session = self.sessions.get(target)
            if session is None or session.chat_id != row.chat_id:
                await self.bot.answer_callback_query(cq.get("id", ""), "That session is gone.")
                return
            self.sessions.switch_to(session)
            await self.bot.answer_callback_query(cq.get("id", ""), f"Switched to {session.name}")
            msg = cq.get("message") or {}
            if msg.get("message_id"):
                current, listed = self.sessions.list(session.chat_id, session.thread_id)
                text, keyboard = commands.sessions_text(current, listed)
                await self.bot.edit_message_text(session.chat_id, msg["message_id"], text,
                                                 reply_markup=keyboard)
        finally:
            self.inbox.finish([row.update_id], DONE)

    # ---- commands ------------------------------------------------------------

    async def command(self, lane: Lane, row: Row, cmd: commands.Command) -> None:
        chat, thread, msg_id = row.chat_id, row.thread_id, row.message_id
        if cmd.name == "stop":
            self._spawn(self.stop(lane, row))
            return
        if cmd.name == "new":
            # Through the lane, so it can never reset a session mid-turn.
            lane.submit(Item(row, command=cmd))
            return
        try:
            if cmd.name == "status":
                session = self.sessions.current(chat, thread)
                busy = (time.monotonic() - lane.busy_since) if lane.busy_since else None
                await self.say(chat, thread, commands.status_text(
                    session, busy_for=busy, queued=len(lane.pending),
                    warm=self.pool.warm_count(), approvals=self.approvals.pending_in(chat)),
                    reply_to=msg_id)
            elif cmd.name == "sessions":
                if cmd.arg:
                    if not commands.valid_session_name(cmd.arg):
                        await self.say(chat, thread, "Session names are letters, digits, "
                                       "<code>.-_</code>, up to 40.", reply_to=msg_id)
                    else:
                        session, created = self.sessions.switch(chat, thread, cmd.arg)
                        await self.say(chat, thread, f"{'Created and switched to' if created else 'Switched to'} "
                                       f"<b>{session.name}</b>.", reply_to=msg_id)
                else:
                    current, listed = self.sessions.list(chat, thread)
                    text, keyboard = commands.sessions_text(current, listed)
                    await self.bot.send_message(chat, text, thread_id=thread or None,
                                                reply_to=msg_id, reply_markup=keyboard)
            else:                                   # /start, /help
                await self.say(chat, thread, "This is the vault. Write, speak or send a file. "
                               "/new starts fresh, /stop halts a run, /status shows what is "
                               "going on, /sessions switches context.", reply_to=msg_id)
        except TelegramError as exc:
            log(f"tg_bridge: /{cmd.name} reply failed ({exc.code})")
        finally:
            self.inbox.finish([row.update_id], DONE)

    async def run_control(self, lane: Lane, item: Item) -> None:
        """Commands that must wait their turn in the lane: /new."""
        row = item.row
        session = self.sessions.current(lane.chat_id, lane.thread_id)
        await self.pool.close_session(session.id, "/new")
        session = self.sessions.reset(session.id)
        self.sessions.set_aborted(session.id, False)
        log(f"tg_bridge: /new, session {session.id} -> {session.claude_session_id[:8]}")
        await self.say(lane.chat_id, lane.thread_id, "New session started.",
                       reply_to=row.message_id)
        if item.command and item.command.arg:
            await self.run_turn(lane, [Item(row, text_override=item.command.arg)])
        else:
            self.inbox.finish([row.update_id], DONE)

    async def stop(self, lane: Lane, row: Row) -> None:
        """Bypasses the queue: interrupt, drain, discard what was waiting, say so."""
        discarded = [it for it in lane.pending]
        lane.pending.clear()
        if discarded:
            self.inbox.finish([it.row.update_id for it in discarded], DEAD, "discarded by /stop")
        running = lane.busy_since is not None
        if running:
            lane.stop_requested = True
            self.approvals.cancel_chat(lane.chat_id)
            if lane.session_id is not None:
                self.sessions.set_aborted(lane.session_id, True)
                await self.pool.interrupt(lane.session_id)
        text = "Stopped." if running else "Nothing was running."
        if discarded:
            text += f" Discarded {len(discarded)} queued message{'s' if len(discarded) != 1 else ''}."
        log(f"tg_bridge: stop ({'running' if running else 'idle'}, {len(discarded)} discarded)")
        try:
            await self.say(lane.chat_id, lane.thread_id, text, reply_to=row.message_id)
        except TelegramError as exc:
            log(f"tg_bridge: stop reply failed ({exc.code})")
        self.inbox.finish([row.update_id], DONE)

    # ---- one turn ------------------------------------------------------------

    async def prepare(self, item: Item, lane: Lane) -> str:
        """One message as prompt text. Downloads, saves, transcribes and echoes."""
        row = item.row
        msg = row.message
        if item.text_override is not None:
            return item.text_override
        caption = msg.get("caption") or ""
        body = msg.get("text") or ""
        pieces: list[str] = []
        voice = msg.get("voice") or msg.get("audio")
        try:
            if voice:
                transcript = row.transcript
                if transcript is None:
                    data, path = await media.download(self.bot, voice["file_id"],
                                                      voice.get("file_size"))
                    transcript, provider = await self.transcriber.transcribe(
                        data, Path(path).name or "voice.ogg")
                    del data            # the audio is never kept (decision 2)
                    self.inbox.set_transcript(row.update_id, transcript)
                    row.transcript = transcript
                    await self._echo(lane, row, transcript)
                pieces.append(media.frame_transcript(transcript, caption))
                caption = ""
            elif msg.get("photo"):
                best = media.largest_photo(msg["photo"])
                data, _ = await media.download(self.bot, best["file_id"], best.get("file_size"))
                saved = media.save(self.vault, data, "photo.jpg")
                pieces.append(media.describe_for_agent(self._att("photo", saved)))
            elif msg.get("document"):
                doc = msg["document"]
                data, _ = await media.download(self.bot, doc["file_id"], doc.get("file_size"))
                saved = media.save(self.vault, data, doc.get("file_name") or "file")
                pieces.append(media.describe_for_agent(
                    self._att("document", saved, mime=doc.get("mime_type", ""),
                              name=doc.get("file_name", ""))))
            else:
                other = next((k for k in ("sticker", "video", "video_note", "animation",
                                          "location", "contact", "poll") if k in msg), None)
                if other:
                    pieces.append(media.describe_for_agent(media.Attachment(other)))
        except media.MediaError as exc:
            pieces.append(media.describe_for_agent(media.Attachment(
                "voice" if voice else "file", error=str(exc))))
            await self.say(lane.chat_id, lane.thread_id, f"⚠️ {exc}", reply_to=row.message_id,
                           html=False)
        text = body or caption
        if text:
            forwarded = any(k in msg for k in ("forward_origin", "forward_from",
                                               "forward_from_chat", "forward_sender_name"))
            pieces.append(media.untrusted(text, "forwarded message") if forwarded else text)
        quoted = msg.get("reply_to_message") or {}
        quoted_text = quoted.get("text") or quoted.get("caption") or ""
        if quoted_text:
            pieces.insert(0, "In reply to:\n" + media.untrusted(quoted_text[:1500],
                                                                "quoted message"))
        return "\n".join(pieces) or "[empty message]"

    def _att(self, kind: str, saved: Path, **kw) -> media.Attachment:
        return media.Attachment(kind, vault_path=str(saved.relative_to(self.vault)),
                                abs_path=str(saved), **kw)

    async def _echo(self, lane: Lane, row: Row, transcript: str) -> None:
        """Best effort: an echo failure must never block the turn."""
        try:
            await self.say(lane.chat_id, lane.thread_id, media.echo_text(transcript),
                           reply_to=row.message_id, html=False)
        except TelegramError as exc:
            log(f"tg_bridge: transcript echo failed ({exc.code})")

    def build_prompt(self, items: list[Item], texts: list[str], session) -> str:
        rows = [it.row for it in items]
        last = rows[-1].message
        meta = {"channel": "telegram", "chat": "private", "topic": rows[-1].thread_id or None,
                "session": session.name,
                "message_ids": [r.message_id for r in rows],
                "sent_at": datetime.fromtimestamp(last.get("date") or time.time(),
                                                  timezone.utc).isoformat()}
        # The box runs UTC; the owner's day is Pacific. Log lines dated by the system clock after
        # 16:00 PT said tomorrow and fell out of the app's Progress today (Nov 3).
        from zoneinfo import ZoneInfo
        pac = datetime.fromtimestamp(last.get("date") or time.time(), ZoneInfo("America/Los_Angeles"))
        meta["today"] = (f"{pac.strftime('%b')} {pac.day} (Pacific, the owner's day; "
                         "date every Log line with it, never the system clock)")
        lines = ["Conversation info (untrusted metadata): " + json.dumps(meta, ensure_ascii=False)]
        if session.aborted_last_run:
            lines.append("[Bridge note: the previous run in this session did not finish (stopped "
                         "by the owner, killed by the watchdog, or failed). Check what was half-done "
                         "before continuing.]")
        if any(it.queued for it in items):
            lines.append("[Queued messages while agent was busy]")
        merged = ""
        for t in texts:
            # A fragment of a split paste joins without a break; separate
            # messages get a line between them (OpenClaw's rule).
            if merged and render.utf16_len(merged.split("\n\n")[-1]) < 4000:
                merged += "\n\n"
            merged += t
        lines.append(merged)
        return "\n\n".join(lines)

    async def run_turn(self, lane: Lane, items: list[Item]) -> None:
        chat, thread = lane.chat_id, lane.thread_id
        ids = [it.row.update_id for it in items]
        reply_to = items[-1].row.message_id
        lane.busy_since = time.monotonic()
        lane.stop_requested = False
        session = self.sessions.current(chat, thread)
        lane.session_id = session.id
        typing = asyncio.create_task(self._typing(chat, thread))
        status = StatusLine(self.bot, chat, thread, enabled=self.status_line,
                            delay=self.status_delay)
        ok = adopted = False
        try:
            texts = [await self.prepare(it, lane) for it in items]
            for it in items:
                self.sessions.remember_message(chat, it.row.message_id, session.id)
            if lane.stop_requested:
                return
            prompt = self.build_prompt(items, texts, session)
            self.inbox.adopt(ids)            # from here on, never replayed
            adopted = True
            self.sessions.touch_interaction(session.id)
            try:
                result = await self.pool.run_turn(session, prompt, chat_id=chat,
                                                  thread_id=thread, on_event=status.on_event)
            except AuthFailed as exc:
                log(f"tg_bridge: Claude login problem ({str(exc)[:120]})")
                await self.say(chat, thread, AUTH_MESSAGE, reply_to=reply_to)
                return
            except Stalled as exc:
                await self.say(chat, thread, f"⚠️ Claude stopped producing output ({exc}) and "
                               f"was stopped. Nothing is running now; send it again if you "
                               f"still want it.", reply_to=reply_to, html=False)
                return
            except agent.AgentError as exc:
                await self.report_failure(chat, thread, reply_to, "a turn did not complete",
                                          str(exc))
                return
            if result.interrupted or lane.stop_requested:
                return                        # /stop already answered
            ok = True
            typing.cancel()
            await status.close()
            await self.deliver(chat, thread, session.id, result, reply_to)
            await self._after_fix_turn(lane, session, items, result.text)
        finally:
            typing.cancel()
            await status.close()
            if not ok and adopted:
                await self._fix_failed(lane, session)
            if ok:
                self.sessions.set_aborted(session.id, False)
            elif adopted and not lane.stop_requested:
                # Killed, stalled or failed: the next turn is told, exactly as
                # after a /stop (which set the flag itself).
                self.sessions.set_aborted(session.id, True)
            self.inbox.finish(ids, DONE)
            lane.busy_since = None
            lane.session_id = None

    # ---- fixes started from the maintenance brief (proactive.py) -------------

    async def start_fix(self, item: dict, brief_date: str, task: Path, chat_id: int,
                        msg: dict) -> str:
        """Open a topic for the fix and queue its first turn. Returns where it runs.

        No topic (Telegram refused) falls back to a named session in the brief's
        own thread; that makes it the current session there, said in the reply.
        """
        name = f"🔧 {item.get('title', 'Fix')}"
        thread = await self.bot.create_forum_topic(chat_id, name)
        if thread:
            session = self.sessions.current(chat_id, thread)
            where = name
        else:
            thread = topic_of(msg)
            session, _ = self.sessions.switch(chat_id, thread,
                                              f"fix {brief_date} {item.get('rank')}")
            where = f"session “{session.name}” in this chat"
        self.fixes.add({"session_id": session.id, "chat_id": chat_id, "thread_id": thread,
                        "topic": where, "task": str(task), "brief": brief_date,
                        "rank": item.get("rank"), "title": item.get("title"),
                        "status": "running", "input": False,
                        "asked_baseline": self.approvals.asked.get((chat_id, thread), 0),
                        "started": datetime.now().astimezone().isoformat(),
                        "finished": None, "summary": ""})
        log(f"tg_bridge: fix of brief {brief_date} item {item.get('rank')} starts in "
            f"thread {thread}, session {session.id}")
        row = Row(update_id=-time.time_ns(), kind="fix", chat_id=chat_id, thread_id=thread,
                  message_id=None, raw={}, state="pending")
        self.lane(chat_id, thread).submit(
            Item(row, text_override=proactive.fix_prompt(item, brief_date, task)))
        return where

    async def _after_fix_turn(self, lane: Lane, session, items: list[Item], text: str) -> None:
        """Apply a fix session's OUTCOME line to its task. Never fails the turn."""
        try:
            fix = self.fixes.running(session.id)
            if not fix:
                return
            asked = self.approvals.asked.get((lane.chat_id, lane.thread_id), 0)
            if not fix["input"] and (any(it.row.kind != "fix" for it in items)
                                     or asked > fix["asked_baseline"]):
                fix["input"] = True
                self.fixes.update(session.id, input=True)
            outcome = proactive.parse_outcome(text)
            if outcome is None:
                return                      # still talking: wait for the final turn
            kind, rest = outcome
            task = Path(fix["task"])
            now = datetime.now().astimezone().isoformat()
            if kind == "done" and not fix["input"]:
                proactive.close_task(task, rest, fix["topic"])
                self.fixes.update(session.id, status="done", finished=now, summary=rest)
                note = f"✅ Done without you. Task closed: {task.stem}"
            elif kind == "done":
                proactive.needs_you_task(task, "Check Claude's fix in its Telegram topic, then "
                                         "close this task", rest, fix["topic"])
                self.fixes.update(session.id, status="needs you", finished=now, summary=rest)
                note = f"Done with your help, so the task stays open for you to close: {task.stem}"
            else:
                proactive.needs_you_task(task, rest, "", fix["topic"])
                self.fixes.update(session.id, status="needs you", finished=now, summary=rest)
                note = f"📌 Task left open, next action: {rest}"
            log(f"tg_bridge: fix {fix['brief']} item {fix['rank']}: {kind}, input={fix['input']}")
            await self.say(lane.chat_id, lane.thread_id, note, html=False)
        except Exception as exc:
            log(f"tg_bridge: fix outcome not applied ({type(exc).__name__}: {exc})")

    async def _fix_failed(self, lane: Lane, session) -> None:
        """A fix turn that died (stall, failure, /stop) leaves its task open, saying so."""
        try:
            fix = self.fixes.running(session.id)
            if not fix:
                return
            proactive.needs_you_task(Path(fix["task"]), "Look at Claude's attempt in its Telegram "
                                     "topic and finish or retry it", "The session did not "
                                     "finish (stopped, stalled or failed).", fix["topic"])
            self.fixes.update(session.id, status="needs you",
                              finished=datetime.now().astimezone().isoformat(),
                              summary="session did not finish")
        except Exception as exc:
            log(f"tg_bridge: failed fix not recorded ({type(exc).__name__}: {exc})")

    async def deliver(self, chat: int, thread: int, session_id: int,
                      result: agent.TurnResult, reply_to: int | None) -> None:
        md = result.text
        if result.notes:
            md = "_(" + " ".join(result.notes) + ")_\n\n" + md
        out = render.plan(md)
        try:
            for i, part in enumerate(out.parts):
                sent = await self.bot.send_rendered(chat, part.html, part.plain,
                                                    thread_id=thread or None,
                                                    reply_to=reply_to if i == 0 else None)
                self.sessions.remember_message(chat, (sent or {}).get("message_id"), session_id)
            if out.document:
                stamp = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M")
                sent = await self.bot.send_document(chat, f"reply-{stamp}.md", out.document,
                                                    caption="Full answer", thread_id=thread or None,
                                                    reply_to=reply_to)
                self.sessions.remember_message(chat, (sent or {}).get("message_id"), session_id)
        except TelegramError as exc:
            await self.report_failure(chat, thread, reply_to, "the answer could not be delivered",
                                      str(exc))

    async def _typing(self, chat: int, thread: int) -> None:
        end = time.monotonic() + TYPING_MAX_S
        try:
            while time.monotonic() < end:
                await self.bot.send_chat_action(chat, "typing", thread_id=thread or None)
                await asyncio.sleep(TYPING_EVERY_S)
        except asyncio.CancelledError:
            pass

    async def say(self, chat: int, thread: int, text: str, *, reply_to: int | None = None,
                  html: bool = True) -> dict | None:
        return await self.bot.send_message(chat, text, parse_mode="HTML" if html else None,
                                           thread_id=thread or None, reply_to=reply_to)

    # ---- failure, which is never allowed to be quiet ---------------------------

    async def report_failure(self, chat: int, thread: int, reply_to: int | None,
                             headline: str, detail: str) -> None:
        log(f"tg_bridge: FAILED, {headline}: {detail[:300]}")
        try:
            await self.say(chat, thread, f"⚠️ {headline}\n\n{detail[:600]}\n\nNothing was replayed. "
                           f"Send it again, or /new for a fresh session.", reply_to=reply_to,
                           html=False)
        except TelegramError as exc:
            log(f"tg_bridge: could not deliver the failure notice ({exc.code})")
        if not self.mail:
            return
        last = self._last_mail.get(headline, 0.0)
        if time.monotonic() - last < MAIL_THROTTLE_S:
            log(f"tg_bridge: mail suppressed, same failure inside {MAIL_THROTTLE_S}s")
            return
        self._last_mail[headline] = time.monotonic()
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
        try:
            to = await asyncio.to_thread(
                notify.send_urgent,
                f"vault-host: Telegram bridge: {headline}",
                f"The Telegram bridge failed at {stamp}.\n\n  problem: {headline}\n\n{detail}\n\n"
                f"A notice was also sent to the chat.\n\n"
                f"    ssh vault-host 'journalctl -u tg-bridge -n 100 --no-pager'\n",
                push_message=f"Telegram bridge: {headline}. Details in mail.")
            log(f"tg_bridge: mailed {to}")
        except notify.MailUnavailable as exc:
            log(f"tg_bridge: mail unavailable ({exc})")
        except Exception as exc:
            log(f"tg_bridge: mail FAILED, {type(exc).__name__}: {exc}")


# --- entry point ----------------------------------------------------------------

def single_instance():
    """flock, the way runner.job and wa_consumer do it. Two pollers on one token
    get 409 Conflict forever and each sees half the messages."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


async def amain(token: str, owner_id: int, cli_path: str,
                providers: list[media.Provider]) -> int:
    bot = Bot(token)
    inbox = Inbox(DB_PATH)
    sessions = SessionStore(DB_PATH)
    approvals = Approvals(bot, owner_id)
    pool = AgentPool(sessions, agent.SdkClient, cwd=config.VAULT, cli_path=cli_path,
                     style_path=REPLY_STYLE, approver=approvals.request,
                     model=env("TG_MODEL") or None)
    bridge = Bridge(bot, inbox, sessions, pool, approvals, media.Transcriber(providers),
                    owner_id=owner_id, vault=config.VAULT,
                    status_line=env("TG_STATUS_LINE", "1") != "0")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    try:
        await bridge.start()
    except TelegramError as exc:
        log(f"tg_bridge: startup failed, {exc}")
        await bot.close()
        return EX_CONFIG if exc.code in (401, 404) else 1

    async def housekeeping() -> None:
        ticks = 0
        while not stop.is_set():
            await asyncio.sleep(30)
            await pool.reap_idle()
            ticks += 1
            if ticks % 2880 == 0:              # daily
                inbox.prune()

    async def token_conflict(seconds: float) -> None:
        log(f"tg_bridge: 409 for {seconds:.0f}s, someone else is polling with this bot token")
        await asyncio.to_thread(
            notify.send_push, "Telegram bot token in use elsewhere",
            f"For {seconds:.0f}s another client has been polling @{bot.username} with its token. "
            "If you did not start one, revoke the token in BotFather (/revoke) and update "
            "TELEGRAM_BOT_TOKEN on vault-host.", priority=1)

    poller = Poller(bot, bridge.next_offset, bridge.on_updates, on_conflict=token_conflict)

    async def poll_watchdog():
        reset_done = False
        while not stop.is_set():
            await asyncio.sleep(30)
            idle = time.monotonic() - poller.last_ok
            action = stall_action(idle, reset_done)
            if action == "exit":
                raise PollStalled(f"no successful poll for {idle:.0f}s")
            if action == "reset":
                log(f"tg_bridge: no successful poll for {idle:.0f}s, reconnecting")
                await bot.reset_transport()
                reset_done = True
            elif idle < 60:
                reset_done = False

    poll_task = asyncio.create_task(poller.run(stop))
    watch_task = asyncio.create_task(poll_watchdog())
    house_task = asyncio.create_task(housekeeping())
    stop_task = asyncio.create_task(stop.wait())
    code = 0
    done, _ = await asyncio.wait({poll_task, watch_task, stop_task},
                                 return_when=asyncio.FIRST_COMPLETED)
    for task in (poll_task, watch_task):
        if task in done and task.exception():
            exc = task.exception()
            log(f"tg_bridge: poller died, {type(exc).__name__}: {exc}")
            code = EX_CONFIG if isinstance(exc, FatalPollError) else 1
    log("tg_bridge: stopping, letting a running turn finish")
    poll_task.cancel()
    watch_task.cancel()
    house_task.cancel()
    stop_task.cancel()
    try:
        await asyncio.wait_for(bridge.settle(), SHUTDOWN_GRACE_S)
    except asyncio.TimeoutError:
        log("tg_bridge: a turn was still running at shutdown; it will be reported on restart")
    await pool.close_all()
    await bot.close()
    return code


def main() -> int:
    config.ensure_dirs()
    token = env("TELEGRAM_BOT_TOKEN")
    owner = env("TELEGRAM_OWNER_ID")
    # Fail closed. No owner means every sender would reach a vault-writing agent.
    missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_OWNER_ID", owner)) if not v]
    if missing:
        log(f"tg_bridge: refusing to start, missing {', '.join(missing)}")
        return EX_CONFIG
    if not owner.isdigit():
        log("tg_bridge: refusing to start, TELEGRAM_OWNER_ID must be the numeric user id")
        return EX_CONFIG
    providers = media.providers_from_env()
    if not providers:
        log("tg_bridge: no GROQ_API_KEY or OPENAI_API_KEY, voice notes will be refused")
    if not REPLY_STYLE.exists():
        log(f"tg_bridge: {REPLY_STYLE} missing, turns run without the reply style")
    try:
        cli_path = agent.claude_binary()
    except agent.AgentError as exc:
        log(f"tg_bridge: refusing to start, {exc}")
        return EX_CONFIG

    lock = single_instance()
    if lock is None:
        log(f"tg_bridge: another bridge holds {LOCK_PATH}, exiting")
        return 1
    # Everything the bridge needs is read; now nothing secret stays in the
    # environment the Claude subprocess (and every Bash it runs) inherits.
    removed = agent.scrub_process_env()
    log(f"tg_bridge: scrubbed {len(removed)} secret variable(s) from the agent's environment")
    try:
        return asyncio.run(amain(token, int(owner), cli_path, providers))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
