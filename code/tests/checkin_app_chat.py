#!/usr/bin/env python3
"""The check-in app's chat (jobs/checkin_app/chat.py), offline: a fake Claude client, a scratch
state folder, no network. Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_chat
"""
import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobs.checkin_app import chat
from jobs.tg_bridge.agent import Event
from tests.tg_bridge._fakes import FAST_LIMITS, Checks, FakeAgent

check = Checks("check-in app chat")
tmp = pathlib.Path(tempfile.mkdtemp())
chat.DIR, chat.DB = tmp / "chat", tmp / "chat.sqlite"
import jobs.checkin_app as app
app.LOG = tmp / "taps.jsonl"


async def wait_idle(hub, key, secs=5):
    for _ in range(int(secs * 100)):
        if not hub.state(key)["busy"]:
            return
        await asyncio.sleep(0.01)


def make(fake):
    return chat.Hub(fake, cli_path="/usr/bin/false", projects_dir=tmp / "projects", limits=FAST_LIMITS)


async def main():
    fake = FakeAgent()
    hub = make(fake)

    # 1. A message gets an answer, and the history survives a new hub (a restart).
    st = hub.send("task:Call Quinn", "Call Quinn", "what is left here?", context="Next: call her", test=False)
    check("sending shows thinking at once", st["busy"] and st["messages"][-1]["role"] == "you")
    await wait_idle(hub, "task:Call Quinn")
    st = hub.state("task:Call Quinn")
    check("the answer lands in the history", st["messages"][-1]["role"] == "claude"
          and "what is left here?" in st["messages"][-1]["text"], st["messages"])
    check("the task arrives as untrusted context", "EXTERNAL_UNTRUSTED_CONTENT" in fake.prompts[-1]
          and "Next: call her" in fake.prompts[-1])
    check("live mode is said in the metadata", '"mode": "live"' in fake.prompts[-1])
    live_spec = fake.specs[-1]
    check("live runs with the bridge's tools and mode", "Write" in live_spec.allowed
          and live_spec.permission_mode == "bypassPermissions")
    check("the app's channel, not Telegram's", "check-in app's chat" in live_spec.append
          and "Telegram bot" not in live_spec.append)
    again = make(FakeAgent())
    check("history is there after reopening", len(again.state("task:Call Quinn")["messages"]) == 2)
    check("one Claude session per conversation", hub.store.switch(1, 0, "task:Call Quinn")[0].id
          == hub.store.switch(1, 0, "task:Call Quinn")[0].id)

    # 2. Test mode: the read-only pool, and anything else is refused without asking.
    async def tries_write(client, prompt):
        ok, msg = await client.spec.can_use_tool("Write", {"file_path": "x", "content": "y"})
        yield Event("text", text=f"write allowed={ok}")
        yield Event("result", text=f"write allowed={ok}: {msg[:30]}", data={"is_error": False, "subtype": "success"})
    fake.behaviour = tries_write
    hub.send("task:Call Quinn", "Call Quinn", "just do it", test=True)
    await wait_idle(hub, "task:Call Quinn")
    spec = fake.specs[-1]
    check("test mode opens the read-only pool", spec.allowed == chat.READ_ONLY and spec.permission_mode == "default"
          and "Test mode" in spec.append)
    check("test mode refuses a write", "allowed=False" in hub.state("task:Call Quinn")["messages"][-1]["text"])
    check("the live client was closed on the switch", fake.clients[0].closed)

    # 3. Live approvals: a card in the sheet, Allow once or Deny, never silently allowed.
    async def wants_send(client, prompt):
        ok, msg = await client.spec.can_use_tool("Bash", {"command": "beeper send 123", "description": "Reply to Quinn"})
        yield Event("result", text=f"send allowed={ok}", data={"is_error": False, "subtype": "success"})
    fake.behaviour = wants_send
    for allow in (True, False):
        hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
        for _ in range(300):
            ap = hub.state("task:Call Quinn")["approval"]
            if ap:
                break
            await asyncio.sleep(0.01)
        check(f"an approval card shows the full command ({allow})", ap and ap["shown"] == "beeper send 123"
              and ap["why"] == "Reply to Quinn")
        check("a made-up request id does nothing", not hub.answer("nope", True))
        hub.answer(ap["id"], allow)
        await wait_idle(hub, "task:Call Quinn")
        msgs = hub.state("task:Call Quinn")["messages"]
        check(f"the tap decides ({allow})", f"send allowed={allow}" in msgs[-1]["text"], msgs[-2:])
        check("the verdict stays in the history", msgs[-2]["role"] == "note"
              and msgs[-2]["text"].startswith("Allowed once" if allow else "Denied"))
    orig = chat.APPROVAL_TIMEOUT_S
    chat.APPROVAL_TIMEOUT_S = 0.2
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    await wait_idle(hub, "task:Call Quinn")
    check("silence is a deny", "send allowed=False" in hub.state("task:Call Quinn")["messages"][-1]["text"])
    chat.APPROVAL_TIMEOUT_S = orig

    # 4. One turn at a time, and Stop.
    from tests.tg_bridge._fakes import interruptible_behaviour
    fake.behaviour = interruptible_behaviour
    hub.send("plan:2026-10-31", "Plan Oct 31", "plan it", test=False, kind="plan")
    st = hub.send("plan:2026-10-31", "Plan Oct 31", "again", test=False, kind="plan")
    check("a message while busy is queued, not refused (Nov 1)",
          st["busy"] and st["messages"][-1]["text"] == "again" and st["messages"][-1].get("queued"))
    await asyncio.sleep(0.1)
    check("the status line shows the tool", "vt" in hub.state("plan:2026-10-31")["status"])
    await hub.stop("plan:2026-10-31")
    await wait_idle(hub, "plan:2026-10-31")
    check("Stop ends the turn", hub.state("plan:2026-10-31")["messages"][-1]["text"] == "Stopped.")
    check("Stop drops the queued message, no new turn",
          not hub.state("plan:2026-10-31")["busy"]
          and any(m.get("discarded") for m in hub.state("plan:2026-10-31")["messages"]))
    from tests.tg_bridge._fakes import answer_behaviour
    fake.behaviour, old_delay = answer_behaviour, getattr(fake, "delay", 0)
    fake.delay = 0.3
    hub.send("task:Queue", "Queue", "first", test=False)
    hub.send("task:Queue", "Queue", "second", test=False)
    await wait_idle(hub, "task:Queue")
    replies = [m["text"] for m in hub.state("task:Queue")["messages"] if m["role"] == "claude"]
    check("a queued message runs right after the reply, in order",
          replies == ["answer to: first", "answer to: second"]
          and not any(m.get("queued") for m in hub.state("task:Queue")["messages"]))
    fake.delay = old_delay

    # 5. A turn cut by a restart is said once, never replayed.
    c = chat.load("task:Other")
    c.update(title="Other", busy=True, messages=[{"role": "you", "text": "hello?", "at": ""}])
    chat.save(c)
    make(FakeAgent())
    msgs = chat.load("task:Other")["messages"]
    check("restart: the cut turn is noted", not chat.load("task:Other")["busy"] and "hello?" in msgs[-1]["text"])
    make(FakeAgent())
    check("restart: noted only once", len(chat.load("task:Other")["messages"]) == 2)
    check("summaries list conversations", set(chat.summaries()) >= {"task:Call Quinn", "plan:2026-10-31"})
    logged = [json.loads(l) for l in app.LOG.read_text().splitlines()]
    turns = [e for e in logged if e["kind"] == "chat"]
    check("every turn is logged", len(turns) >= 6)
    check("a turn log carries the reply and the task", any(e.get("reply", "").startswith("answer to") and e["id"] == "Call Quinn"
                                                          for e in turns))
    sig = [e.get("signal") for e in logged if e["kind"] == "signal"]
    check("approvals are signals", sig.count("chat_approved") == 1 and sig.count("chat_denied") == 1
          and "chat_approval_timeout" in sig)
    await hub.close()


asyncio.run(main())
print("\nFAILED:\n  " + "\n  ".join(check.fails) if check.fails else "\nall passed")
sys.exit(1 if check.fails else 0)
