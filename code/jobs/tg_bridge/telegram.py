"""Raw Telegram Bot API over httpx. Every call the bridge makes goes through Bot.call().

No framework: OpenClaw itself dropped grammY's runner for a hand-written loop,
and ours is about the same size. Four rules from openclaw-research-3 shape it:

  * **One owner for 429s.** A single per-token limiter honours `retry_after`.
    Final replies wait it out; typing and status edits are dropped instead of
    queued, so a rate limit never delays the answer behind cosmetics.
  * **Retry a send only when it provably never reached Telegram.** A connect
    error is safe to retry. A read timeout is not: the message may already be
    on the phone, and resending it is how one answer becomes two.
  * **Classify by error_code**, never by guessing at prose. The one exception
    is "can't parse entities", which only the description distinguishes, and
    which is exactly the case the plain-text fallback exists for.
  * **Never log the token.** httpx puts the URL, and so `bot<token>`, into its
    exception text. Everything that leaves this module is redacted first.
"""

import asyncio
import hashlib
import json
import time
from typing import Any, Awaitable, Callable, Protocol

from runner.job import log

API_BASE = "https://api.telegram.org"

# Long poll: Telegram holds the request up to POLL_TIMEOUT, the client gives up
# well after that, so a healthy idle poll never reads as a network failure.
POLL_TIMEOUT = 30
POLL_CLIENT_TIMEOUT = 45
ALLOWED_UPDATES = ["message", "callback_query"]

SEND_TIMEOUT = 30
# Attempts for a request that failed before it reached Telegram, or was 429'd.
MAX_ATTEMPTS = 4
CONNECT_BACKOFF_S = (1, 2, 4)
# Bot API cloud limit for getFile. Larger files are a permanent "file is too big".
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

_PARSE_ERRORS = ("can't parse entities", "find end of the entity", "unsupported start tag",
                 "can't find end tag", "unclosed start tag")


class TelegramError(Exception):
    """The Bot API answered, and the answer was no."""

    def __init__(self, method: str, code: int | None, description: str,
                 retry_after: float | None = None):
        self.method = method
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after
        super().__init__(f"{method} -> {code}: {self.description[:300]}")


class NotSent(TelegramError):
    """The request never reached Telegram (connect error, DNS). Safe to retry."""


class MaybeSent(TelegramError):
    """The request left, the answer did not come back. Never blindly retried."""


class Transport(Protocol):
    """What Bot needs from the wire. Tests replace it with a recorded fake."""

    async def post(self, method: str, data: dict, files: dict | None,
                   timeout: float) -> dict: ...

    async def fetch(self, file_path: str, max_bytes: int, timeout: float) -> bytes: ...

    async def close(self) -> None: ...


def redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "<token>")
    return text


class HttpxTransport:
    """The real wire. httpx is imported here, not at module level, so the
    offline tests run on a bare interpreter."""

    def __init__(self, token: str, *, base: str = API_BASE):
        import httpx
        self._httpx = httpx
        self._token = token
        self._base = base
        # HTTP/1.1 (httpx's default) and IPv4 only: research-3 item 17, a flaky
        # IPv6 route is a classic cause of a poller that silently stalls.
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
            timeout=httpx.Timeout(SEND_TIMEOUT, connect=10))

    async def post(self, method: str, data: dict, files: dict | None,
                   timeout: float) -> dict:
        httpx = self._httpx
        url = f"{self._base}/bot{self._token}/{method}"
        try:
            if files:
                # Multipart wants flat strings; nested values travel as JSON.
                form = {k: v if isinstance(v, str) else json.dumps(v)
                        for k, v in data.items() if v is not None}
                resp = await self._client.post(url, data=form, files=files, timeout=timeout)
            else:
                resp = await self._client.post(url, json=data, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise NotSent(method, None, redact(f"{type(exc).__name__}: {exc}", self._token))
        except httpx.TransportError as exc:
            # ReadTimeout, WriteTimeout, RemoteProtocolError: the bytes may
            # have arrived. Treated as possibly delivered.
            raise MaybeSent(method, None, redact(f"{type(exc).__name__}: {exc}", self._token))
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "error_code": resp.status_code,
                    "description": f"non-JSON response, HTTP {resp.status_code}"}

    async def fetch(self, file_path: str, max_bytes: int, timeout: float) -> bytes:
        httpx = self._httpx
        url = f"{self._base}/file/bot{self._token}/{file_path}"
        try:
            async with self._client.stream("GET", url, timeout=timeout) as resp:
                if resp.status_code != 200:
                    raise TelegramError("download", resp.status_code, "file download failed")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise TelegramError("download", 400, "file is too big")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TransportError as exc:
            raise NotSent("download", None, redact(f"{type(exc).__name__}: {exc}", self._token))

    async def close(self) -> None:
        await self._client.aclose()


def token_fingerprint(token: str) -> str:
    """Identifies a token without being one. Stored beside the offset, so a
    rotated token starts clean and the database never holds the secret."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class Bot:
    def __init__(self, token: str, transport: Transport | None = None, *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 transport_factory: Callable[[], Transport] | None = None):
        self.token = token
        self.bot_id = token.split(":", 1)[0]
        self.fingerprint = token_fingerprint(token)
        self._factory = transport_factory or (lambda: HttpxTransport(token))
        self._transport = transport or self._factory()
        self._sleep = sleep
        self._clock = clock
        self._blocked_until = 0.0
        self.dropped = 0          # droppable calls skipped under a rate limit
        self.username = ""

    async def reset_transport(self) -> None:
        """A fresh socket after a 409 or a stalled poll (research-3 items 2, 17)."""
        try:
            await self._transport.close()
        except Exception as exc:
            log(f"tg_bridge: closing the old transport failed ({type(exc).__name__})")
        self._transport = self._factory()

    async def close(self) -> None:
        await self._transport.close()

    # ---- the one place requests happen -----------------------------------

    async def call(self, method: str, payload: dict | None = None, *,
                   files: dict | None = None, droppable: bool = False,
                   timeout: float = SEND_TIMEOUT) -> Any:
        """One Bot API call. Returns `result`, or None when a droppable call
        was skipped. Raises TelegramError (or NotSent / MaybeSent)."""
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        for attempt in range(MAX_ATTEMPTS):
            wait = self._blocked_until - self._clock()
            if wait > 0:
                if droppable:
                    self.dropped += 1
                    return None
                await self._sleep(wait)
            try:
                body = await self._transport.post(method, payload, files, timeout)
            except NotSent as exc:
                if attempt + 1 >= MAX_ATTEMPTS:
                    raise
                if droppable:
                    self.dropped += 1
                    return None
                log(f"tg_bridge: {method} did not reach Telegram ({exc.description[:120]}), "
                    f"retrying")
                await self._sleep(CONNECT_BACKOFF_S[min(attempt, len(CONNECT_BACKOFF_S) - 1)])
                continue
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code")
            desc = redact(str(body.get("description") or ""), self.token)
            params = body.get("parameters") or {}
            if code == 429:
                # Telegram refused it, so it provably was not sent: a retry
                # cannot duplicate anything.
                retry_after = float(params.get("retry_after") or 1)
                self._blocked_until = max(self._blocked_until, self._clock() + retry_after)
                log(f"tg_bridge: 429 on {method}, blocked for {retry_after:.0f}s")
                if droppable:
                    self.dropped += 1
                    return None
                continue
            raise TelegramError(method, code, desc, params.get("retry_after"))
        raise TelegramError(method, 429, "still rate limited after retries")

    # ---- methods ---------------------------------------------------------

    async def get_me(self) -> dict:
        me = await self.call("getMe")
        self.username = (me or {}).get("username", "")
        return me

    async def delete_webhook(self) -> None:
        # drop_pending_updates stays false: pending updates are messages.
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        return await self.call(
            "getUpdates", {"offset": offset, "timeout": timeout, "limit": 100,
                           "allowed_updates": ALLOWED_UPDATES},
            timeout=timeout + (POLL_CLIENT_TIMEOUT - POLL_TIMEOUT)) or []

    async def send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML",
                           thread_id: int | None = None, reply_to: int | None = None,
                           reply_markup: dict | None = None) -> dict:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
                   "message_thread_id": thread_id or None, "reply_markup": reply_markup,
                   "link_preview_options": {"is_disabled": True}}
        if reply_to:
            # Quote-reply, but never fail the answer because the question was
            # deleted in the meantime.
            payload["reply_parameters"] = {"message_id": reply_to,
                                           "allow_sending_without_reply": True}
        return await self.call("sendMessage", payload)

    async def send_rendered(self, chat_id: int, html: str, plain: str, **kw) -> dict | None:
        """HTML first, the same text as plain on a parse error.

        This one wrapper prevents most "bot went silent" failures: Telegram
        rejects the whole message for one unbalanced tag, and the answer is
        worth more than its formatting.
        """
        try:
            return await self.send_message(chat_id, html, parse_mode="HTML", **kw)
        except TelegramError as exc:
            if exc.code != 400:
                raise
            desc = exc.description.lower()
            if "message text is empty" in desc:
                return None
            if not any(p in desc for p in _PARSE_ERRORS):
                raise
            log(f"tg_bridge: HTML rejected ({exc.description[:80]}), sending plain")
            return await self.send_message(chat_id, plain, parse_mode=None, **kw)

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, *,
                                parse_mode: str | None = "HTML",
                                reply_markup: dict | None = None,
                                droppable: bool = False) -> None:
        try:
            await self.call("editMessageText",
                            {"chat_id": chat_id, "message_id": message_id, "text": text,
                             "parse_mode": parse_mode, "reply_markup": reply_markup,
                             "link_preview_options": {"is_disabled": True}},
                            droppable=droppable)
        except TelegramError as exc:
            if "message is not modified" not in exc.description:
                raise

    async def edit_message_reply_markup(self, chat_id: int, message_id: int,
                                        reply_markup: dict | None = None) -> None:
        try:
            await self.call("editMessageReplyMarkup",
                            {"chat_id": chat_id, "message_id": message_id,
                             "reply_markup": reply_markup or {"inline_keyboard": []}})
        except TelegramError as exc:
            if "message is not modified" not in exc.description:
                raise

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError as exc:
            log(f"tg_bridge: deleteMessage failed, leaving it ({exc.code})")

    async def send_chat_action(self, chat_id: int, action: str = "typing", *,
                               thread_id: int | None = None) -> None:
        try:
            await self.call("sendChatAction", {"chat_id": chat_id, "action": action,
                                               "message_thread_id": thread_id or None},
                            droppable=True)
        except TelegramError as exc:
            log(f"tg_bridge: sendChatAction failed ({exc.code})")

    async def create_forum_topic(self, chat_id: int, name: str) -> int | None:
        """A new topic, its message_thread_id; None if Telegram refuses (e.g. topics are
        off in this chat). Names are capped at 128 characters by the Bot API."""
        try:
            result = await self.call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})
        except TelegramError as exc:
            log(f"tg_bridge: createForumTopic refused ({exc.code}: {exc.description[:120]})")
            return None
        thread = (result or {}).get("message_thread_id") if isinstance(result, dict) else None
        return int(thread) if thread else None

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        try:
            await self.call("answerCallbackQuery",
                            {"callback_query_id": callback_id, "text": text or None})
        except TelegramError as exc:
            # A late answer is "query is too old"; the tap still counts.
            log(f"tg_bridge: answerCallbackQuery failed ({exc.code})")

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", {"file_id": file_id})

    async def download(self, file_path: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES,
                       timeout: float = 60) -> bytes:
        return await self._transport.fetch(file_path, max_bytes, timeout)

    async def send_document(self, chat_id: int, filename: str, content: bytes, *,
                            caption: str = "", thread_id: int | None = None,
                            reply_to: int | None = None) -> dict:
        payload = {"chat_id": str(chat_id), "caption": caption or None,
                   "parse_mode": "HTML" if caption else None,
                   "message_thread_id": str(thread_id) if thread_id else None}
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to,
                                           "allow_sending_without_reply": True}
        return await self.call("sendDocument", payload,
                               files={"document": (filename, content, "text/markdown")},
                               timeout=60)

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands", {"commands": [
            {"command": c, "description": d} for c, d in commands]})


# --- the poller -------------------------------------------------------------

# Watchdog over the poller's last success. A healthy loop succeeds at least
# every POLL_TIMEOUT seconds even when nobody writes, so minutes of silence
# mean it is stuck: reconnect first, then exit for systemd to restart us.
STALL_RESET_S = 180

# A 409 at startup is normal: the previous process's last poll is still open.
# One that persists means someone else polls with this token, i.e. it leaked.
CONFLICT_ALERT_S = 120
STALL_EXIT_S = 600


def stall_action(idle_s: float, reset_done: bool) -> str:
    """'ok', 'reset' (once per stall) or 'exit', for `idle_s` since the last good poll."""
    if idle_s > STALL_EXIT_S:
        return "exit"
    if idle_s > STALL_RESET_S and not reset_done:
        return "reset"
    return "ok"


class PollStalled(Exception):
    """No successful getUpdates for STALL_EXIT_S. Exit 1, systemd restarts."""


class FatalPollError(Exception):
    """The token is revoked or wrong. Restarting will not help; a human must."""


class Poller:
    """getUpdates forever. The first call uses timeout 0 to validate quickly.

    `on_batch` must persist the updates and advance the offset before it
    returns; the poller only ever asks `next_offset()` where to continue.
    """

    def __init__(self, bot: Bot, next_offset: Callable[[], int | None],
                 on_batch: Callable[[list[dict]], Awaitable[None]], *,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_conflict: Callable[[float], Awaitable[None]] | None = None):
        self.bot = bot
        self.on_conflict = on_conflict
        self.conflict_since: float | None = None
        self.conflict_alerted = False
        self.next_offset = next_offset
        self.on_batch = on_batch
        self._sleep = sleep
        self.last_ok = time.monotonic()

    async def run(self, stop: asyncio.Event) -> None:
        first, backoff = True, 1.0
        while not stop.is_set():
            try:
                # A hard ceiling above the client timeout: an await that never
                # returns (a hung socket, a stuck DNS lookup) must still end.
                updates = await asyncio.wait_for(
                    self.bot.get_updates(self.next_offset(), 0 if first else POLL_TIMEOUT),
                    POLL_CLIENT_TIMEOUT + 15)
            except asyncio.TimeoutError:
                log(f"tg_bridge: getUpdates hung past {POLL_CLIENT_TIMEOUT + 15}s, "
                    f"reconnecting and retrying in {backoff:.0f}s")
                await self.bot.reset_transport()
                await self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            except TelegramError as exc:
                if exc.code in (401, 404):
                    raise FatalPollError(f"Telegram rejected the token ({exc.code})")
                if exc.code == 409:
                    now = time.monotonic()
                    self.conflict_since = self.conflict_since or now
                    if (now - self.conflict_since > CONFLICT_ALERT_S and not self.conflict_alerted
                            and self.on_conflict):
                        self.conflict_alerted = True
                        try:
                            await self.on_conflict(now - self.conflict_since)
                        except Exception as alert_exc:
                            log(f"tg_bridge: conflict alert failed ({alert_exc})")
                    # Another poller, or a webhook, owns this token. Take it back.
                    log("tg_bridge: 409, another poller or a webhook is using this token; "
                        "deleting the webhook and backing off")
                    try:
                        await self.bot.delete_webhook()
                    except TelegramError as again:
                        log(f"tg_bridge: deleteWebhook failed ({again.code})")
                    await self.bot.reset_transport()
                else:
                    log(f"tg_bridge: getUpdates failed ({exc.code}: {exc.description[:120]}), "
                        f"retrying in {backoff:.0f}s")
                await self._sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            first, backoff = False, 1.0
            self.last_ok = time.monotonic()
            self.conflict_since, self.conflict_alerted = None, False
            if updates:
                await self.on_batch(updates)
