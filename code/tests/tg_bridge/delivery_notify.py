#!/usr/bin/env python3
"""Where answers land, what the phone shows while waiting, and notify.send_telegram.

    python3 -m tests.tg_bridge.delivery_notify

  1. **Replies stay in their topic**, and each topic is its own session.
  2. **Quote-replying an old answer continues that answer's session**, even
     after switching away with /sessions.
  3. **The status line** is one message, says which tool is running, and is
     deleted when the answer lands. Typing is sent while the turn runs.
  4. **send_telegram** posts to the owner's chat (optionally a topic), fails
     loudly as TelegramUnavailable, and never leaks the token.
"""
import asyncio
import io
import json
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import OWNER, Checks, make_bridge, update
from jobs.tg_bridge.agent import Event
from runner import config, notify


async def topics(check):
    bridge, transport, fake, _ = make_bridge()
    await bridge.on_updates([update("MBA question", thread=11)])
    await bridge.on_updates([update("startup question", thread=22)])
    await bridge.settle()
    answers = [d for d in transport.sent() if "answer to" in d.get("text", "")]
    by_thread = {d["message_thread_id"]: d["text"] for d in answers}
    check("each topic is answered in its own thread",
          "MBA" in by_thread.get(11, "") and "startup" in by_thread.get(22, ""), by_thread)
    s11 = bridge.sessions.current(OWNER, 11)
    s22 = bridge.sessions.current(OWNER, 22)
    check("each topic is its own session", s11.claude_session_id != s22.claude_session_id)
    check("typing was sent into the topic",
          any(d.get("message_thread_id") == 11 for d in transport.sent("sendChatAction")))


async def quote_routing(check):
    bridge, transport, fake, _ = make_bridge()
    await bridge.on_updates([update("first topic of conversation")])
    await bridge.settle()
    main_answer = next(d for d in transport.sent() if "answer to" in d.get("text", ""))
    main_answer_id = transport.next_id      # the fake's id for that send
    await bridge.on_updates([update("/sessions oslo")])
    await bridge.on_updates([update("plan the trip")])
    await bridge.settle()
    check("/sessions <name> switches", bridge.sessions.list(OWNER, 0)[0] == "oslo")
    oslo = bridge.sessions.current(OWNER, 0)
    await bridge.on_updates([update("follow-up on that", reply_to_message={
        "message_id": main_answer_id, "text": main_answer["text"]})])
    await bridge.settle()
    current = bridge.sessions.current(OWNER, 0)
    check("a quote-reply to an old answer routes back to its session",
          current.name == "main" and current.id != oslo.id, current.name)
    check("the quoted text reaches the agent as untrusted material",
          "In reply to:" in fake.prompts[-1] and "EXTERNAL_UNTRUSTED_CONTENT" in fake.prompts[-1])


async def status_line(check):
    bridge, transport, fake, _ = make_bridge()

    async def busy(client, prompt):
        yield Event("tool", name="Read", id="r1", data={"file_path": "/home/<user>/vault/Wiki/index.md"})
        await asyncio.sleep(0.05)
        yield Event("tool_result", id="r1")
        yield Event("tool", name="Bash", id="b1",
                    data={"command": "/home/<user>/automation/bin/vt todos --due"})
        await asyncio.sleep(1.2)
        yield Event("tool_result", id="b1")
        yield Event("result", text="Nothing due.", data={"is_error": False})

    fake.behaviour = busy
    await bridge.on_updates([update("anything due?")])
    await bridge.settle()
    statuses = [d for d in transport.sent() if d.get("text", "").startswith("⏳")]
    check("one status message was sent", len(statuses) == 1, [d.get("text") for d in statuses])
    check("silently", statuses and statuses[0].get("disable_notification") is True)
    edits = [d["text"] for d in transport.sent("editMessageText")]
    check("it was edited with the running tool", any("Running vt todos" in e for e in edits), edits)
    check("and deleted when the answer landed", len(transport.sent("deleteMessage")) == 1)
    order = [m for m, _, _ in transport.calls if m in ("deleteMessage", "sendMessage")]
    check("the status was removed before the answer was sent",
          order.index("deleteMessage") < len(order) - 1 and order[-1] == "sendMessage", order)


def notify_telegram(check):
    original_env, original_open = config.load_env, notify.urllib.request.urlopen
    try:
        config.load_env = lambda: {}
        try:
            notify.send_telegram("hello")
            raised = False
        except notify.TelegramUnavailable:
            raised = True
        check("unconfigured Telegram raises TelegramUnavailable", raised)

        token = "999:SECRETSECRET"
        config.load_env = lambda: {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_OWNER_ID": "4242"}
        seen = {}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def ok_open(req, timeout=0):
            seen["url"], seen["data"] = req.full_url, req.data.decode()
            return Resp(json.dumps({"ok": True, "result": {"message_id": 77}}).encode())

        notify.urllib.request.urlopen = ok_open
        check("send_telegram returns the message id", notify.send_telegram("Due today: x",
                                                                         thread_id=11) == 77)
        check("to the owner's chat and topic",
              "chat_id=4242" in seen["data"] and "message_thread_id=11" in seen["data"], seen)
        check("as plain text (no parse_mode)", "parse_mode" not in seen["data"])

        def refusing(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, io.BytesIO(
                b'{"ok":false,"description":"Bad Request: chat not found"}'))

        notify.urllib.request.urlopen = refusing
        try:
            notify.send_telegram("x")
        except notify.TelegramUnavailable as exc:
            check("a refusal is loud and names the reason", "chat not found" in str(exc))
            check("the token is not in the error", token not in str(exc))

        def unreachable(req, timeout=0):
            raise urllib.error.URLError(f"failed for {req.full_url}")

        notify.urllib.request.urlopen = unreachable
        try:
            notify.send_telegram("x")
        except notify.TelegramUnavailable as exc:
            check("unreachable is loud and redacted", "unreachable" in str(exc)
                  and token not in str(exc), str(exc))
    finally:
        config.load_env, notify.urllib.request.urlopen = original_env, original_open


def run() -> list[str]:
    check = Checks("delivery: topics, quote routing, status line; notify.send_telegram")
    for case in (topics, quote_routing, status_line):
        asyncio.run(case(check))
    notify_telegram(check)
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
