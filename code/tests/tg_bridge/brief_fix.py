#!/usr/bin/env python3
"""[Let Claude fix it]: a brief item becomes a task, a topic and a session.

    python3 -m tests.tg_bridge.brief_fix

  1. **The tap creates the task and starts a session in a new topic**, whose
     first turn is the item framed as material, with the stop rules and the
     OUTCOME line it must end on.
  2. **Done without the owner's input closes the task**: status done, checkbox
     ticked, a dated "Done by Claude" section, and one line in the topic.
  3. **Any input keeps it open**: a message from him in the topic, or an
     approval he was asked for, means the task waits for him to close it.
  4. **Needs you leaves it open with what is needed** as the next action, and a
     session that dies says so on the task instead of leaving it "running".
  5. **No topic, no problem**: if Telegram refuses createForumTopic, the fix
     runs as a named session in the brief's own chat.
  6. **The task edits themselves**: close ticks, reopen unticks, frontmatter
     keeps its schema.
"""
import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import OWNER, Checks, callback, make_bridge, update, wait_for
from jobs.tg_bridge import proactive
from jobs.tg_bridge.agent import Event
from jobs.tg_bridge.telegram import TelegramError

BRIEF = """# Maintenance brief

```maintenance-brief-items
{"brief": "2026-10-05", "items": [
 {"id": "mb-2026-10-05-1", "rank": 1, "title": "Routine timer only on the box",
  "what": "automation@routine_push.timer runs but is not in ~/automation/units.",
  "why": "A rebuild would lose the reminders.", "next_action": "Commit the timer unit",
  "effort": "15 min", "kind": "claude"}]}
```
"""

TASK = """---
type: action
captured: 2026-10-05T14:10
status: inbox
priority: 1-none
---

## Next action
- [ ] Next: Commit the timer unit

## Facts (maintenance brief, 2026-10-05)
- **What is wrong:** not in the repo
"""


def answering(*texts):
    """An agent that answers the n-th prompt with texts[n] (the last one repeats)."""
    state = {"n": 0}

    async def behaviour(client, prompt):
        await asyncio.sleep(0.01)
        text = texts[min(state["n"], len(texts) - 1)]
        state["n"] += 1
        yield Event("text", text=text)
        yield Event("result", text=text, data={"is_error": False, "subtype": "success"})
    return behaviour


async def setup(*, topic=True):
    bridge, transport, fake, tmp = make_bridge()
    briefs = bridge.vault / "_system" / "maintenance" / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "2026-10-05.md").write_text(BRIEF)
    inbox = tmp / "Inbox"
    inbox.mkdir()
    made = []

    def create(item, brief_date):
        path = inbox / f"{item['title']}.md"
        path.write_text(TASK)
        made.append(path)
        return path

    bridge.brief_buttons.create = create
    transport.script["createForumTopic"] = [
        {"ok": True, "result": {"message_thread_id": 901, "name": "🔧 x"}} if topic else
        TelegramError("createForumTopic", 400, "Bad Request: the chat is not a forum")]
    return bridge, transport, fake, made


def sent_to(transport, thread):
    return [d.get("text", "") for d in transport.sent() if d.get("message_thread_id") == thread]


def done_without_input(check):
    async def scenario():
        bridge, transport, fake, made = await setup()
        fake.behaviour = answering("Committed units/automation@routine_push.timer.\n"
                                   "OUTCOME: done | committed the timer, heartbeat check passes")
        await bridge.on_updates([callback("mb1:2026-10-05:1:f", message_id=60)])
        await bridge.settle()
        await wait_for(lambda: bridge.fixes.load() and bridge.fixes.load()[-1]["status"] != "running")
        await bridge.settle()
        check("the tap created the task", len(made) == 1, made)
        check("...and opened a topic named after the item",
              any(m == "createForumTopic" and "Routine timer" in d.get("name", "")
                  for m, d, _ in transport.calls))
        prompt = fake.prompts[-1] if fake.prompts else ""
        check("the session's first turn is the item, framed as material",
              "Let Claude fix it" in prompt and "<<<BRIEF_ITEM>>>" in prompt
              and "Commit the timer unit" in prompt, prompt[:200])
        check("...with the hand-over rules and the OUTCOME contract",
              "Go as far as you can" in prompt and "prepare what he needs to decide" in prompt
              and "OUTCOME: done |" in prompt and "OUTCOME: needs you |" in prompt)
        check("...credentials and systemd allowed, gated by the phone",
              "Credentials and systemd are allowed" in prompt and "sudo" in prompt)
        check("...and it ran in the new topic", fake.specs and 901 in [
            d.get("message_thread_id") for d in transport.sent("sendChatAction")], None)
        text = made[0].read_text()
        check("done without input closes the task", "status: done" in text, text)
        check("...ticks the next action", "- [x] Next: Commit the timer unit" in text, text)
        check("...and records what Claude did",
              "## Done by Claude (" in text and "committed the timer" in text, text)
        check("...and says so in the topic",
              any("Done without you" in t for t in sent_to(transport, 901)), sent_to(transport, 901))
        fix = bridge.fixes.load()[-1]
        check("the fix log has it as done, for next week's check",
              fix["status"] == "done" and fix["input"] is False and fix["finished"], fix)
        edits = transport.sent("editMessageText")
        check("the brief message says Claude is on it, buttons gone",
              edits and "Claude is on it" in edits[-1]["text"] and not edits[-1].get("reply_markup"),
              edits[-1:])
    asyncio.run(scenario())


def with_input(check):
    async def scenario():
        bridge, transport, fake, made = await setup()
        fake.behaviour = answering("Which branch should it go on?",
                                   "Done.\nOUTCOME: done | committed on master")
        await bridge.on_updates([callback("mb1:2026-10-05:1:f")])
        await bridge.settle()
        await wait_for(lambda: len(fake.prompts) >= 1)
        await bridge.settle()
        check("a turn without an OUTCOME line leaves the fix running",
              bridge.fixes.load()[-1]["status"] == "running" and "status: inbox" in made[0].read_text())
        await bridge.on_updates([update("master", thread=901)])
        await bridge.settle()
        await wait_for(lambda: bridge.fixes.load()[-1]["status"] != "running")
        text = made[0].read_text()
        fix = bridge.fixes.load()[-1]
        check("after the owner answered, done does NOT close the task",
              "status: inbox" in text and fix["input"] is True, text)
        check("...the next action asks him to check and close it",
              "- [ ] Next: Check Claude's fix" in text, text)

        bridge2, transport2, fake2, made2 = await setup()
        fake2.behaviour = answering("OUTCOME: done | did it")
        bridge2.approvals.asked[(OWNER, 901)] = 0
        orig = bridge2._after_fix_turn

        async def with_approval(lane, session, items, text):
            bridge2.approvals.asked[(lane.chat_id, lane.thread_id)] = 1   # he tapped Allow once
            await orig(lane, session, items, text)
        bridge2._after_fix_turn = with_approval
        await bridge2.on_updates([callback("mb1:2026-10-05:1:f")])
        await bridge2.settle()
        await wait_for(lambda: bridge2.fixes.load()[-1]["status"] != "running")
        check("an approval he was asked for also counts as input",
              "status: inbox" in made2[0].read_text() and bridge2.fixes.load()[-1]["input"])
    asyncio.run(scenario())


def needs_you_and_failure(check):
    async def scenario():
        bridge, transport, fake, made = await setup()
        fake.behaviour = answering("The key name is unclear.\n"
                                   "OUTCOME: needs you | confirm WAHA_API_KEY_SHA512 belongs in ~/.waha.env")
        await bridge.on_updates([callback("mb1:2026-10-05:1:f")])
        await bridge.settle()
        await wait_for(lambda: bridge.fixes.load()[-1]["status"] != "running")
        text = made[0].read_text()
        check("needs you keeps the task open", "status: inbox" in text, text)
        check("...with what is needed as the next action",
              "- [ ] Next: confirm WAHA_API_KEY_SHA512 belongs in ~/.waha.env" in text, text)
        check("...and a Tried by Claude section", "## Tried by Claude (" in text, text)
        check("...and says so in the topic",
              any("Task left open" in t for t in sent_to(transport, 901)))

        bridge2, transport2, fake2, made2 = await setup()

        async def dies(client, prompt):
            yield Event("error", text="boom")
        fake2.behaviour = dies
        await bridge2.on_updates([callback("mb1:2026-10-05:1:f")])
        await bridge2.settle()
        await wait_for(lambda: bridge2.fixes.load()[-1]["status"] != "running")
        text = made2[0].read_text()
        check("a session that dies leaves the task open and says why",
              "status: inbox" in text and "did not finish" in text
              and bridge2.fixes.load()[-1]["status"] == "needs you", text)
    asyncio.run(scenario())


def no_topic(check):
    async def scenario():
        bridge, transport, fake, made = await setup(topic=False)
        fake.behaviour = answering("OUTCOME: done | ok")
        await bridge.on_updates([callback("mb1:2026-10-05:1:f")])
        await bridge.settle()
        await wait_for(lambda: bridge.fixes.load() and bridge.fixes.load()[-1]["status"] != "running")
        fix = bridge.fixes.load()[-1]
        check("no topic: the fix runs as a named session in the brief's chat",
              fix["thread_id"] == 0 and "fix 2026-10-05 1" in fix["topic"], fix)
        check("...and still closes its task", "status: done" in made[0].read_text())
        edits = transport.sent("editMessageText")
        check("...and the brief message names where it runs",
              edits and "fix 2026-10-05 1" in edits[-1]["text"], edits[-1:])
    asyncio.run(scenario())


def task_edits(check):
    path = pathlib.Path(tempfile.mkdtemp()) / "t.md"
    path.write_text(TASK)
    proactive.close_task(path, "did it", "🔧 topic")
    text = path.read_text()
    check("close: status done, checkbox ticked, section added",
          "status: done" in text and "- [x] Next: Commit" in text and "## Done by Claude" in text)
    check("close keeps the rest of the frontmatter",
          "type: action" in text and "priority: 1-none" in text and text.startswith("---\n"))
    proactive.reopen_task(path, "still failing")
    text = path.read_text()
    check("reopen: status inbox, checkbox open again, reason recorded",
          "status: inbox" in text and "- [ ] Next: Commit" in text
          and "## Reopened by the maintenance brief" in text and "still failing" in text, text)
    check("the outcome line is parsed from the last OUTCOME",
          proactive.parse_outcome("x\nOUTCOME: needs you | a\nOUTCOME: done | b") == ("done", "b")
          and proactive.parse_outcome("**OUTCOME:** Done | ok") == ("done", "ok")
          and proactive.parse_outcome("no outcome here") is None)
    check("every item: Let Claude fix it and Create task, Skip on its own row",
          proactive.brief_buttons("2026-10-05", 1) == [
              [("Let Claude fix it", "mb1:2026-10-05:1:f"), ("Create task", "mb1:2026-10-05:1:c")],
              [("Skip", "mb1:2026-10-05:1:s")]])
    check("the fix callback parses", proactive.parse_callback("mb1:2026-10-05:2:f") == ("2026-10-05", 2, "f"))


def run() -> list[str]:
    check = Checks("brief fixes: Let Claude fix it, outcome, task closing")
    task_edits(check)
    done_without_input(check)
    with_input(check)
    needs_you_and_failure(check)
    no_topic(check)
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
