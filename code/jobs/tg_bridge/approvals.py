"""can_use_tool -> an inline keyboard on the phone -> a tap, or a deny after 2 minutes.

Decision 1, and OpenClaw's rules, which are good ones:

  * **The full command or nothing.** If it cannot be shown whole within one
    Telegram message, or it carries control or bidi characters that would
    make what is shown differ from what runs, it is denied, never truncated.
  * **Allow once and Deny. No "allow always" for anything.**
  * **Silence is a deny.** No tap within APPROVAL_TIMEOUT_S, a /stop, or any
    error on the way: deny, and say so on the message.
  * **Only the owner's tap counts.** Checked on the callback itself; the
    message being in his private chat is not the check.

Callback data is versioned, `tga1:<id>:<a|d>`, well under Telegram's 64-byte
limit, and looked up server-side: the button carries no decision the bridge
did not issue.

Read-only Bash (`date`, `uname`) never reaches here: the CLI approves it
itself before can_use_tool (spike 1). That is accepted; this module is the
gate for everything that can change state.
"""

import asyncio
import html
import json
import secrets
import unicodedata
from dataclasses import dataclass

from runner.job import log

from .render import utf16_len
from .telegram import Bot, TelegramError

APPROVAL_TIMEOUT_S = 120
CALLBACK_PREFIX = "tga1"
CALLBACK_MAX_BYTES = 64
# The visible text of the request must fit one message with room for the
# header and the verdict line appended afterwards.
MAX_SHOWN_UTF16 = 3500

# Bidi overrides and isolates: text that renders in a different order from
# the order it executes in.
_BIDI = {"‪", "‫", "‬", "‭", "‮",
         "⁦", "⁧", "⁨", "⁩", "‎", "‏", "؜"}


def callback_data(request_id: str, choice: str) -> str:
    data = f"{CALLBACK_PREFIX}:{request_id}:{choice}"
    assert len(data.encode("utf-8")) <= CALLBACK_MAX_BYTES
    return data


def parse_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[2] not in ("a", "d"):
        return None
    return parts[1], parts[2]


def unsafe_characters(text: str) -> bool:
    for ch in text:
        if ch in _BIDI:
            return True
        if unicodedata.category(ch) == "Cc" and ch not in "\n\t":
            return True
    return False


def describe(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """(what is shown verbatim, a one-line context). Bash shows the command
    itself; anything else shows its whole input as JSON."""
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        why = str(tool_input.get("description") or "")
        return command, why
    return json.dumps(tool_input, ensure_ascii=False, indent=1), ""


@dataclass
class _Pending:
    future: asyncio.Future
    chat_id: int
    message_id: int | None
    body: str


class Approvals:
    def __init__(self, bot: Bot, owner_id: int, *, timeout: float = APPROVAL_TIMEOUT_S):
        self.bot = bot
        self.owner_id = owner_id
        self.timeout = timeout
        self._pending: dict[str, _Pending] = {}
        # Requests shown on the phone, per (chat, topic). A fix session that
        # needed one was not finished "without the owner's input" (proactive.py).
        self.asked: dict[tuple[int, int], int] = {}

    def pending_in(self, chat_id: int) -> int:
        return sum(1 for p in self._pending.values() if p.chat_id == chat_id)

    async def request(self, chat_id: int, thread_id: int, tool_name: str,
                      tool_input: dict) -> tuple[bool, str]:
        """Ask on the phone. Returns (allowed, message for Claude when denied)."""
        shown, why = describe(tool_name, tool_input)
        if not shown.strip():
            return False, "Empty request, denied."
        if unsafe_characters(shown):
            await self._notice(chat_id, thread_id, f"Denied a {html.escape(tool_name)} request "
                               "that contained hidden control characters.")
            return False, ("Denied: the request contains control or bidi characters, so the "
                           "full command could not be shown faithfully.")
        if utf16_len(shown) + utf16_len(why) > MAX_SHOWN_UTF16:
            await self._notice(chat_id, thread_id, f"Denied a {html.escape(tool_name)} request "
                               "too long to show in full.")
            return False, ("Denied: the full command is too long to show on the phone. Break it "
                           "into shorter steps, or write a script into the vault and ask to run "
                           "that.")

        request_id = secrets.token_hex(4)
        self.asked[(chat_id, thread_id or 0)] = self.asked.get((chat_id, thread_id or 0), 0) + 1
        verb = "run" if tool_name == "Bash" else f"use {html.escape(tool_name)}"
        lang = ' class="language-bash"' if tool_name == "Bash" else ""
        body = (f"<b>I want to {verb}:</b>\n<pre><code{lang}>{html.escape(shown)}</code></pre>"
                + (f"\n<i>{html.escape(why)}</i>" if why else ""))
        keyboard = {"inline_keyboard": [[
            {"text": "Allow once", "callback_data": callback_data(request_id, "a")},
            {"text": "Deny", "callback_data": callback_data(request_id, "d")},
        ]]}
        future = asyncio.get_running_loop().create_future()
        pending = _Pending(future, chat_id, None, body)
        self._pending[request_id] = pending
        try:
            try:
                sent = await self.bot.send_message(chat_id, body, thread_id=thread_id or None,
                                                   reply_markup=keyboard)
                pending.message_id = (sent or {}).get("message_id")
            except TelegramError as exc:
                log(f"tg_bridge: could not ask for approval ({exc.code}), denying")
                return False, "Denied: the approval request could not be delivered."
            try:
                allowed = await asyncio.wait_for(asyncio.shield(future), self.timeout)
            except asyncio.TimeoutError:
                await self._close(pending, "⌛ No answer in 2 minutes, denied.")
                return False, "Denied: the owner did not answer within 2 minutes."
            if allowed is None:
                await self._close(pending, "⏹ Stopped, denied.")
                return False, "Denied: the run was stopped."
            log(f"tg_bridge: {tool_name} {'allowed' if allowed else 'denied'} by tap")
            return (True, "") if allowed else (False, "Denied by the owner.")
        finally:
            self._pending.pop(request_id, None)

    async def handle_callback(self, callback: dict) -> bool:
        """A button tap. True if it was an approval button (ours to answer)."""
        parsed = parse_callback(callback.get("data") or "")
        if parsed is None:
            return False
        request_id, choice = parsed
        callback_id = callback.get("id", "")
        if (callback.get("from") or {}).get("id") != self.owner_id:
            # Not the owner: acknowledged so the spinner stops, otherwise ignored.
            await self.bot.answer_callback_query(callback_id)
            log("tg_bridge: ignored an approval tap from someone other than the owner")
            return True
        pending = self._pending.get(request_id)
        if pending is None or pending.future.done():
            await self.bot.answer_callback_query(callback_id, "This request has expired.")
            msg = callback.get("message") or {}
            if msg.get("message_id") and (msg.get("chat") or {}).get("id"):
                try:
                    await self.bot.edit_message_reply_markup(msg["chat"]["id"], msg["message_id"])
                except TelegramError:
                    pass
            return True
        allowed = choice == "a"
        pending.future.set_result(allowed)
        await self.bot.answer_callback_query(callback_id, "Allowed once" if allowed else "Denied")
        await self._close(pending, "✅ Allowed once." if allowed else "❌ Denied.")
        return True

    def cancel_chat(self, chat_id: int) -> int:
        """/stop: every open request in this chat resolves as a deny."""
        n = 0
        for pending in list(self._pending.values()):
            if pending.chat_id == chat_id and not pending.future.done():
                pending.future.set_result(None)
                n += 1
        return n

    async def _close(self, pending: _Pending, verdict: str) -> None:
        """The verdict goes onto the message and the buttons come off."""
        if not pending.message_id:
            return
        try:
            await self.bot.edit_message_text(pending.chat_id, pending.message_id,
                                             f"{pending.body}\n\n{verdict}", reply_markup=None)
        except TelegramError as exc:
            log(f"tg_bridge: could not mark the approval message ({exc.code})")
            try:
                await self.bot.edit_message_reply_markup(pending.chat_id, pending.message_id)
            except TelegramError:
                pass

    async def _notice(self, chat_id: int, thread_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text, thread_id=thread_id or None)
        except TelegramError as exc:
            log(f"tg_bridge: approval notice failed ({exc.code})")
