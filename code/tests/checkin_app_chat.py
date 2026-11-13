#!/usr/bin/env python3
"""The check-in app's chat (jobs/checkin_app/chat.py), offline: a fake Claude client, a scratch
state folder, no network. Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_chat
"""
import asyncio
import time
import json
import pathlib
import sys
import os
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# A scratch HOME and vault before the app is imported (Nov 6): live turns run log_chat_delta, which
# scans the vault and writes do.json; without this they touched the real vault and state.
_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="carmen-chat-"))
for _d in ("home", "vault/Inbox", "vault/_system"):
    (_SCRATCH / _d).mkdir(parents=True)
os.environ["HOME"], os.environ["VAULT_PATH"] = str(_SCRATCH / "home"), str(_SCRATCH / "vault")

from jobs.checkin_app import chat
import jobs.checkin_app.adapters.taplog as _taplog
from runner import config as _config
assert str(_config.STATE_DIR).startswith(str(_SCRATCH)) and str(_config.VAULT).startswith(str(_SCRATCH)), \
    "chat suite is not isolated: refusing to touch live state"
from jobs.tg_bridge.agent import Event
from tests.tg_bridge._fakes import FAST_LIMITS, Checks, FakeAgent

check = Checks("check-in app chat")
tmp = pathlib.Path(tempfile.mkdtemp())
chat.DIR, chat.DB = tmp / "chat", tmp / "chat.sqlite"
import jobs.checkin_app as app
_taplog.LOG = tmp / "taps.jsonl"
# Every card is a task note (Nov 4): what Carmen creates and drops stays in the scratch folder.
from jobs.checkin_app.tasks import cardnotes as _cardnotes  # noqa: E402
_cardnotes.MADE = tmp / "made.json"
os.environ["PT_DROPPED"] = str(tmp / "dropped.json")
os.environ["STEPS_DIR"] = str(tmp / "steps")


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
    # Screenshots (Nov 2): the message keeps its images, Claude gets the saved path.
    st = hub.send("task:Pics", "Pics", "see this", test=True, prompt_text="see this\n\n[the owner attached a screenshot, saved at Attachments/Carmen/x.png]",
                  images=["obsidian://open?vault=<VAULT_NAME>&file=Attachments%2FCarmen%2Fx.png"])
    await wait_idle(hub, "task:Pics")
    sent = [m for m in hub.state("task:Pics")["messages"] if m["role"] == "you"][-1]
    check("a message keeps its images", sent.get("images") and sent["text"] == "see this", sent)
    check("Claude is told where the screenshot is", "Attachments/Carmen/x.png" in fake.prompts[-1])
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
    # Nov 8: a waiting approval is listed for the banner on every tab, and pushed if unanswered.
    pushes = []
    hub.push = lambda *a: pushes.append(a)
    orig_push = chat.APPROVAL_PUSH_S
    chat.APPROVAL_PUSH_S = 0.05
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    for _ in range(300):
        ap = hub.state("task:Call Quinn")["approval"]
        if ap:
            break
        await asyncio.sleep(0.01)
    pend = hub.pending()
    check("the waiting approval is listed for the banner", len(pend) == 1 and pend[0]["id"] == ap["id"]
          and pend[0]["shown"] == "beeper send 123" and pend[0]["title"] == "Call Quinn", pend)
    await asyncio.sleep(0.3)
    check("unanswered, it is pushed once", len(pushes) == 1 and pushes[0][0] == "Call Quinn"
          and "beeper send 123" in pushes[0][2], pushes)
    hub.answer(ap["id"], False)
    await wait_idle(hub, "task:Call Quinn")
    check("answered, the list is empty", hub.pending() == [])
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    for _ in range(300):
        ap = hub.state("task:Call Quinn")["approval"]
        if ap:
            break
        await asyncio.sleep(0.01)
    hub.answer(ap["id"], True)
    await wait_idle(hub, "task:Call Quinn")
    await asyncio.sleep(0.2)
    check("answered in time, no push", len(pushes) == 1, pushes)
    chat.APPROVAL_PUSH_S = orig_push
    orig = chat.APPROVAL_TIMEOUT_S
    chat.APPROVAL_TIMEOUT_S = 0.2
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    await wait_idle(hub, "task:Call Quinn")
    check("silence is a deny", "send allowed=False" in hub.state("task:Call Quinn")["messages"][-1]["text"])
    chat.APPROVAL_TIMEOUT_S = orig

    # Nov 9: a QA run (CARMEN_QA_APPROVALS=deny) never waits for a person and never allows.
    import os as _os
    _os.environ["CARMEN_QA_APPROVALS"] = "deny"
    orig_qa = chat.QA_DENY_S
    chat.QA_DENY_S = 0.2
    t0 = time.monotonic()
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    await wait_idle(hub, "task:Call Quinn")
    msgs = hub.state("task:Call Quinn")["messages"]
    check("QA run: the card is denied on its own, fast", "send allowed=False" in msgs[-1]["text"]
          and time.monotonic() - t0 < 5, msgs[-2:])
    check("QA run: the note says why", msgs[-2]["role"] == "note" and msgs[-2]["text"].startswith("QA run:"))
    hub.send("task:Call Quinn", "Call Quinn", "send it", test=False)
    for _ in range(300):
        ap = hub.state("task:Call Quinn")["approval"]
        if ap:
            break
        await asyncio.sleep(0.01)
    hub.answer(ap["id"], True)
    await wait_idle(hub, "task:Call Quinn")
    check("QA run: even a tap on Allow denies",
          "send allowed=False" in hub.state("task:Call Quinn")["messages"][-1]["text"])
    async def wants_block(client, prompt):
        ok, msg = await client.spec.can_use_tool("mcp__claude_ai_Google_Calendar__create_event",
                                                 {"summary": "Focus", "startTime": "x", "endTime": "y"})
        yield Event("result", text=f"block allowed={ok}", data={"is_error": False, "subtype": "success"})
    keep = fake.behaviour
    fake.behaviour = wants_block
    hub.send("task:Call Quinn", "Call Quinn", "block it", test=False)
    await wait_idle(hub, "task:Call Quinn")
    check("QA run: no tap-free calendar block either",
          "block allowed=False" in hub.state("task:Call Quinn")["messages"][-1]["text"])
    fake.behaviour = keep
    del _os.environ["CARMEN_QA_APPROVALS"]
    chat.QA_DENY_S = orig_qa
    check("live: the switch is off without the variable", not chat.qa_deny())

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
    # Turn ids (Nov 11, Talk spoke one answer four times): on his message and on the answer, the
    # running turn in the state, and the Talk hook for a turn queued behind another.
    hooked = []
    hub.before_queued = lambda key, turn: (hooked.append((key, turn)), "[QUEUED NOTE] ")[1]
    s1 = hub.send("task:Turns", "Turns", "first", test=False)
    s2 = hub.send("task:Turns", "Turns", "second", test=False)
    check("turns: each send says its turn, the running one is in the state",
          s1.get("sent_turn") == 1 and s2.get("sent_turn") == 2 and s2.get("turn") == 1, (s1.get("sent_turn"), s2.get("sent_turn"), s2.get("turn")))
    await wait_idle(hub, "task:Turns")
    ms = hub.state("task:Turns")["messages"]
    check("turns: his messages and the answers carry the turn id",
          [m.get("turn") for m in ms if m["role"] == "you"] == [1, 2]
          and [m.get("turn") for m in ms if m["role"] == "claude"] == [1, 2], [(m["role"], m.get("turn")) for m in ms])
    check("turns: the queued turn's prompt gets the Talk note, with the finished turn",
          hooked == [("task:Turns", 1)] and any(p_.startswith("[QUEUED NOTE]") or "[QUEUED NOTE]" in p_ for p_ in fake.prompts),
          (hooked, [p_[-120:] for p_ in fake.prompts[-2:]]))
    check("turns: an idle conversation has no running turn", hub.state("task:Turns").get("turn") is None)
    hub.before_queued = None
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
    logged = [json.loads(l) for l in _taplog.LOG.read_text().splitlines()]
    turns = [e for e in logged if e["kind"] == "chat"]
    check("every turn is logged", len(turns) >= 6)
    check("a turn log carries the reply and the task", any(e.get("reply", "").startswith("answer to") and e["id"] == "Call Quinn"
                                                          for e in turns))
    sig = [e.get("signal") for e in logged if e["kind"] == "signal"]
    # Two of each (plus the QA run's Allow tap, logged as a deny): section 3's pair plus the banner checks' pair (Nov 8).
    check("approvals are signals", sig.count("chat_approved") == 2 and sig.count("chat_denied") == 3
          and "chat_approval_timeout" in sig and "chat_qa_denied" in sig)

    # 6. Plan chats (Nov 10, Tier 1.2 and 1.3): only the Book tap books, Book only books, and its
    #    outcome is read from the calendar into the chat state.
    import datetime as _dt
    from jobs.checkin_app.adapters import calendar as _cal
    from jobs.checkin_app.planning import read as _pr
    pday = _dt.date(2026, 11, 10)
    pkey = "plan:" + pday.isoformat()
    block = {"date": pday.isoformat(), "s": 840, "f": 900, "title": "Trail race prep"}

    def books(extra_edit=False):
        async def run(client, prompt):
            yield Event("tool", name="mcp__claude_ai_Google_Calendar__create_event", id="t1", data={"summary": "Trail race prep"})
            ok, _ = await client.spec.can_use_tool("mcp__claude_ai_Google_Calendar__create_event", {"summary": "Trail race prep"})
            # A Book turn's update is refused by the gate; outside Book it would wait for an Allow card.
            ok2 = (await client.spec.can_use_tool("mcp__claude_ai_Google_Calendar__update_event", {"eventId": "x"}))[0] \
                if "[Book] the owner tapped Book" in prompt else False
            yield Event("tool_result", id="t1")
            if extra_edit:
                yield Event("tool", name="Edit", id="t2", data={"file_path": "/v/Inbox/Finalise Iceland trek prep.md"})
                yield Event("tool_result", id="t2")
            yield Event("result", text=f"create={ok} update={ok2}", data={"is_error": False, "subtype": "success"})
        return run
    fake.behaviour = books()
    hub.send(pkey, "Plan Nov 10", "Yes, add the e-bike", kind="plan", test=False)
    await wait_idle(hub, pkey)
    st = hub.state(pkey)
    check("plan: a chat message never books", "create=False" in st["messages"][-1]["text"] and "book" not in st, st["messages"][-2:])
    check("plan: the prompt says the chat plans its own day and only Book books",
          "this chat plans Tuesday 2026-11-10 only" in fake.prompts[-1] and "[Book]" in fake.prompts[-1])
    check("plan: the prompt names the day by its weekday, the app's durations and a changed-blocks line (UI r3)",
          "the planned day is" in fake.prompts[-1] and "Tuesday, Nov 10" in fake.prompts[-1] and '"3h 40"' in fake.prompts[-1]
          and "Changed:" in fake.prompts[-1])
    real_events = _cal.day_events
    _cal.day_events = lambda day, fresh=False: [{"title": "Trail race prep", "s": 840, "f": 900, "task_ids": ["t-00c001"]}]
    try:
        st = hub.send(pkey, "Plan Nov 10", "Book 1 block for Tuesday", kind="plan", test=False,
                      prompt_text=_pr.book_text(pday, [block]), book={"day": pday, "blocks": [block]})
        check("plan: Book shows booking at once", st["book"]["status"] == "booking" and st["messages"][-1].get("book"), st.get("book"))
        await wait_idle(hub, pkey)
        st = hub.state(pkey)
        check("plan: Book creates the blocks and changes no other event", "create=True update=False" in st["messages"][-1]["text"],
              st["messages"][-2:])
        check("plan: Book's outcome comes from the calendar", st.get("book", {}).get("status") == "booked" and st["book"]["booked"] == 1, st.get("book"))
        fake.behaviour = books(extra_edit=True)
        hub.send(pkey, "Plan Nov 10", "Book 1 block for Tuesday", kind="plan", test=False,
                 prompt_text=_pr.book_text(pday, [block]), book={"day": pday, "blocks": [block]})
        await wait_idle(hub, pkey)
        st = hub.state(pkey)
        check("plan: a Book that also edited a task says what else changed", st["book"]["status"] == "changed"
              and st["book"]["changes"] == ["Edited Finalise Iceland trek prep"], st.get("book"))
        _cal.day_events = lambda day, fresh=False: []
        hub.send(pkey, "Plan Nov 10", "Book 1 block for Tuesday", kind="plan", test=False,
                 prompt_text=_pr.book_text(pday, [block]), book={"day": pday, "blocks": [block]})
        await wait_idle(hub, pkey)
        check("plan: a Book that left the calendar empty reads failed", hub.state(pkey)["book"]["status"] == "failed",
              hub.state(pkey).get("book"))

        async def proposes(client, prompt):
            yield Event("result", text="New plan\n```blocks\n2026-11-10 15:00-16:00 Run\n```", data={"is_error": False, "subtype": "success"})
        fake.behaviour = proposes
        hub.send(pkey, "Plan Nov 10", "Move it later", kind="plan", test=False)
        await wait_idle(hub, pkey)
        check("plan: a new proposal clears the last Book's outcome", "book" not in hub.state(pkey))
        check("plan: the latest proposal is stored for the sheet and Do's banner (copy r3)",
              [(x["date"], x["s"], x["title"]) for x in hub.state(pkey).get("plan", {}).get("blocks", [])] == [("2026-11-10", 900, "Run")],
              hub.state(pkey).get("plan"))
        check("plan: the prompt asks for titles only, en-dash ranges and a question mark on a choice",
              "never their IDs" in fake.prompts[-1] and "14:00–14:45" in fake.prompts[-1] and "question mark" in fake.prompts[-1])
    finally:
        _cal.day_events = real_events
    await hub.close()


asyncio.run(main())

# The step trail (Nov 9): what a chat turn did to the steps, by id.
_b = [{"id": "s1", "state": "open", "text": "Look up the bidding dates (15 min)"},
      {"id": "s2", "state": "open", "text": "Email Sam"}, {"id": "s3", "state": "open", "text": "Book"},
      {"id": "s4", "state": "open", "text": "Gone"}]
_a = [{"id": "s1", "state": "open", "text": "Email Academic Operations (15 min)"},
      {"id": "s2", "state": "done", "text": "Email Sam"}, {"id": "s3", "state": "struck", "text": "Book"},
      {"id": "", "state": "open", "text": "New step"}]
_ch = chat.step_changes(_b, _a)
check("step trail: reworded, ticked, discarded, added, removed",
      sorted(x["kind"] for x in _ch) == ["added", "discarded", "removed", "reworded", "ticked"]
      and next(x for x in _ch if x["kind"] == "reworded")["old"].startswith("Look up"), _ch)
check("step trail: nothing changed is nothing", chat.step_changes(_b, _b) == [])
print("\nFAILED:\n  " + "\n  ".join(check.fails) if check.fails else "\nall passed")
sys.exit(1 if check.fails else 0)
