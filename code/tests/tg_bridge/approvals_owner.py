#!/usr/bin/env python3
"""Approval buttons, and the owner-only filter.

    python3 -m tests.tg_bridge.approvals_owner

  1. **Silence is a deny.** No tap inside the timeout denies, says so on the
     message, and removes the buttons.
  2. **Only the owner's tap counts.** A stranger's tap is acknowledged and
     otherwise ignored.
  3. **The full command or a deny.** Too long to show, or hidden control or
     bidi characters: denied without ever offering the buttons.
  4. **Callback data stays under 64 bytes** and is versioned.
  5. **Waiting for a tap is not a stall**: the watchdog does not kill a turn
     that is waiting on the owner.
  6. **Anyone but the owner, in his private chat, gets silence.** Nothing is
     sent, nothing reaches the agent.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import (OWNER, STRANGER, Checks, callback, make_bridge, update,
                                    wait_for)
from jobs.tg_bridge.agent import Event
from jobs.tg_bridge.approvals import (CALLBACK_MAX_BYTES, callback_data, parse_callback)
from jobs.tg_bridge.commands import SESSION_CALLBACK
from jobs.tg_bridge.inbox import DEAD


async def timeout_denies(check):
    bridge, transport, fake, _ = make_bridge(approval_timeout=0.1)
    allowed, why = await bridge.approvals.request(OWNER, 0, "Bash", {"command": "rm -rf /tmp/x"})
    check("no tap within the timeout denies", allowed is False, why)
    check("and tells Claude why", "2 minutes" in why)
    asked = transport.sent()[0]
    check("the request showed the full command", "rm -rf /tmp/x" in asked["text"])
    check("with Allow once / Deny buttons only",
          [b["text"] for b in asked["reply_markup"]["inline_keyboard"][0]] == ["Allow once", "Deny"])
    edits = transport.sent("editMessageText")
    check("the message was edited to say it expired", edits and "No answer" in edits[-1]["text"])
    check("and the buttons were removed", edits and "reply_markup" not in edits[-1])


async def owner_tap(check):
    bridge, transport, fake, _ = make_bridge(approval_timeout=2)
    task = asyncio.create_task(bridge.approvals.request(OWNER, 0, "Bash", {"command": "ls /etc"}))
    await wait_for(lambda: transport.sent())
    markup = transport.sent()[0]["reply_markup"]["inline_keyboard"][0]
    allow_data = markup[0]["callback_data"]
    msg_id = 5001
    await bridge.on_updates([callback(allow_data, sender=STRANGER, message_id=msg_id)])
    await asyncio.sleep(0.02)
    check("a stranger's tap decides nothing", not task.done())
    await bridge.on_updates([callback(allow_data, sender=OWNER, message_id=msg_id)])
    allowed, _ = await asyncio.wait_for(task, 1)
    check("the owner's tap allows once", allowed is True)
    answers = transport.sent("answerCallbackQuery")
    check("every tap is acknowledged (spinner stops)", len(answers) == 2, len(answers))
    edits = transport.sent("editMessageText")
    check("the verdict is written onto the message", edits and "Allowed once" in edits[-1]["text"])
    await bridge.on_updates([callback(allow_data, sender=OWNER, message_id=msg_id)])
    check("a second tap on a decided request is told it expired",
          "expired" in (transport.sent("answerCallbackQuery")[-1].get("text") or ""))


async def refuses_unshowable(check):
    bridge, transport, fake, _ = make_bridge()
    allowed, why = await bridge.approvals.request(OWNER, 0, "Bash", {"command": "echo " + "a" * 5000})
    check("a command too long to show is denied", allowed is False and "too long" in why)
    check("without offering buttons", not any("reply_markup" in d for d in transport.sent()))
    allowed, why = await bridge.approvals.request(OWNER, 0, "Bash",
                                                  {"command": "echo safe ‮ lmth.exe"})
    check("a bidi override is denied", allowed is False and "bidi" in why)
    check("callback data is versioned and under 64 bytes",
          len(callback_data("deadbeef", "a").encode()) <= CALLBACK_MAX_BYTES
          and callback_data("deadbeef", "a").startswith("tga1:"))
    check("callback data round-trips", parse_callback(callback_data("deadbeef", "d")) == ("deadbeef", "d"))
    check("foreign callback data is not ours", parse_callback("tga0:x:a") is None)
    check("session callbacks are under 64 bytes too",
          len(f"{SESSION_CALLBACK}:{10**12}".encode()) <= CALLBACK_MAX_BYTES)


async def watchdog_waits_for_tap(check):
    # The watchdog's silence limit (0.15 s) is far shorter than the wait for the tap.
    bridge, transport, fake, _ = make_bridge(
        approval_timeout=0.5, limits={"fresh": 0.15, "between": 0.15, "tool": 0.15})

    async def asks(client, prompt):
        yield Event("tool", name="Bash", id="b1", data={"command": "touch /tmp/marker"})
        allowed, why = await client.spec.can_use_tool("Bash", {"command": "touch /tmp/marker"})
        yield Event("tool_result", id="b1")
        yield Event("result", text=f"allowed={allowed}", data={"is_error": False})

    fake.behaviour = asks
    await bridge.on_updates([update("make the marker")])
    await bridge.settle()
    check("a turn waiting 0.5 s for a tap is not killed by a 0.15 s watchdog",
          any("allowed=False" in t for t in transport.texts()), transport.texts())

    async def schedules(client, prompt):
        allowed, why = await client.spec.can_use_tool("CronCreate", {"cron": "* * * * *"})
        yield Event("result", text=f"cron={allowed}", data={"is_error": False})

    fake.behaviour = schedules
    n = len(transport.sent())
    await bridge.on_updates([update("remind me hourly")])
    await bridge.settle()
    check("scheduling tools are refused without asking",
          any("cron=False" in t for t in transport.texts())
          and not any("reply_markup" in d for d in transport.sent()[n:]))


async def owner_only(check):
    bridge, transport, fake, _ = make_bridge()
    stranger = update("hi bot", sender=STRANGER, chat=STRANGER)
    group = update("hi from a group", chat=-1001, chat_type="supergroup")
    await bridge.on_updates([stranger, group])
    await bridge.on_updates([callback(f"{SESSION_CALLBACK}:1", sender=STRANGER, chat=STRANGER)])
    await bridge.settle()
    check("a stranger gets silence", transport.sent() == [], transport.texts())
    check("nothing reached the agent", fake.prompts == [])
    check("the stranger's row is dead", bridge.inbox.get(stranger["update_id"]).state == DEAD)
    check("the owner writing from a group is dropped too",
          bridge.inbox.get(group["update_id"]).state == DEAD)
    await bridge.on_updates([update("hi, it's me")])
    await bridge.settle()
    check("the owner is answered", any("answer to" in t for t in transport.texts()))


def run() -> list[str]:
    check = Checks("approvals and the owner filter")
    for case in (timeout_denies, owner_tap, refuses_unshowable, watchdog_waits_for_tap, owner_only):
        asyncio.run(case(check))
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
