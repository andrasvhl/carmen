#!/usr/bin/env python3
"""The #1 invariant, collect mode, debounce, and /stop jumping the queue.

    python3 -m tests.tg_bridge.serialization

  1. **Never two agent runs on one session**, however the calls arrive: the
     pool's per-session lock, and the lane worker in front of it. Two
     different sessions may run side by side; that is what proves the lock is
     per session rather than one global bottleneck.
  2. **Messages sent mid-turn are collected into the next turn**, marked as
     queued, never run in parallel and never dropped.
  3. **Rapid messages merge** inside the debounce window.
  4. **/stop and the stop words bypass the queue**: interrupt, discard what
     was waiting, no answer delivered for the stopped turn, and the next turn
     is told the last one was interrupted.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import (Checks, interruptible_behaviour, make_bridge, update,
                                    wait_for)
from jobs.tg_bridge.inbox import DEAD


async def pool_lock(check):
    bridge, transport, fake, _ = make_bridge()
    fake.delay = 0.05
    s1 = bridge.sessions.current(1, 0)
    s2 = bridge.sessions.current(2, 0)
    await asyncio.gather(*(bridge.pool.run_turn(s1, f"p{i}", chat_id=1, thread_id=0)
                           for i in range(5)))
    check("five concurrent run_turn calls on one session never overlap", fake.max_active == 1,
          fake.max_active)
    check("all five ran", len(fake.prompts) == 5)
    check("one warm client served them", len(fake.clients) == 1, len(fake.clients))
    fake.max_active = 0
    await asyncio.gather(bridge.pool.run_turn(s1, "a", chat_id=1, thread_id=0),
                         bridge.pool.run_turn(s2, "b", chat_id=2, thread_id=0))
    check("two different sessions can run side by side", fake.max_active == 2, fake.max_active)
    s3 = bridge.sessions.current(3, 0)
    await bridge.pool.run_turn(s3, "c", chat_id=3, thread_id=0)
    check("never more than MAX_WARM (2) warm clients", bridge.pool.warm_count() == 2,
          bridge.pool.warm_count())
    check("the least recently used client was closed", fake.clients[0].closed)


async def collect_mode(check):
    bridge, transport, fake, _ = make_bridge()
    fake.delay = 0.3
    await bridge.on_updates([update("plan my Tuesday")])
    await wait_for(lambda: fake.active == 1)
    await bridge.on_updates([update("oh and I have the dentist at 3")])
    await asyncio.sleep(0.01)
    await bridge.on_updates([update("and gym after")])
    await bridge.settle()
    check("lane turns never overlapped", fake.max_active == 1, fake.max_active)
    check("two turns: the running one, then one collected turn", len(fake.prompts) == 2,
          len(fake.prompts))
    second = fake.prompts[1] if len(fake.prompts) > 1 else ""
    check("the first turn did not see the later messages", "dentist" not in fake.prompts[0])
    check("the collected turn carries both later messages",
          "dentist" in second and "gym after" in second)
    check("the collected turn is marked as queued", "[Queued messages while agent was busy]" in second)
    answers = [d for d in transport.sent() if "answer to" in d.get("text", "")]
    check("each turn was answered", len(answers) == 2, transport.texts())
    check("answers quote-reply the trigger",
          all(d.get("reply_parameters", {}).get("message_id") for d in answers))


async def debounce(check):
    bridge, transport, fake, _ = make_bridge()
    await bridge.on_updates([update("first half")])
    await asyncio.sleep(0.01)
    await bridge.on_updates([update("second half")])
    await bridge.settle()
    check("two messages inside the window become one turn", len(fake.prompts) == 1,
          len(fake.prompts))
    check("with both texts", fake.prompts and "first half" in fake.prompts[0]
          and "second half" in fake.prompts[0])
    check("a merged batch is not marked queued",
          fake.prompts and "[Queued" not in fake.prompts[0])


async def stop_bypass(check):
    bridge, transport, fake, _ = make_bridge()
    fake.behaviour = interruptible_behaviour
    await bridge.on_updates([update("rewrite every note")])
    await wait_for(lambda: fake.active == 1)
    waiting = update("and also this")
    await bridge.on_updates([waiting])
    await bridge.on_updates([update("Stopp!")])
    ok = await wait_for(lambda: any(t.startswith("Stopped.") for t in transport.texts()), 3)
    check("the stop word is answered while the turn is still running", ok, transport.texts())
    check("the running turn was interrupted", fake.interrupts == 1, fake.interrupts)
    await bridge.settle()
    stopped = next((t for t in transport.texts() if t.startswith("Stopped.")), "")
    check("the queued message was discarded and counted", "Discarded 1 queued message" in stopped,
          stopped)
    check("the discarded row is dead", bridge.inbox.get(waiting["update_id"]).state == DEAD)
    check("the discarded message never reached the agent",
          not any("and also this" in p for p in fake.prompts))
    check("no answer was delivered for the stopped turn",
          not any("answer to" in t for t in transport.texts()))
    session = bridge.sessions.current(4242, 0)
    check("the session remembers the run was aborted", session.aborted_last_run)

    fake.behaviour = __import__("tests.tg_bridge._fakes", fromlist=["x"]).answer_behaviour
    await bridge.on_updates([update("ok continue")])
    await bridge.settle()
    check("the next turn is told the previous one was interrupted",
          "previous run in this session did not finish" in fake.prompts[-1], fake.prompts[-1][:200])
    check("and the flag is cleared after it", not bridge.sessions.current(4242, 0).aborted_last_run)
    check("the warm client survived the interrupt (spike 6)", len(fake.clients) == 1,
          len(fake.clients))

    await bridge.on_updates([update("/stop")])
    await bridge.settle()
    check("/stop with nothing running says so", transport.texts()[-1].startswith("Nothing was running"))


async def new_command(check):
    bridge, transport, fake, _ = make_bridge()
    await bridge.on_updates([update("hello")])
    await bridge.settle()
    before = bridge.sessions.current(4242, 0).claude_session_id
    await bridge.on_updates([update("/new start over")])
    await bridge.settle()
    after = bridge.sessions.current(4242, 0).claude_session_id
    check("/new mints a fresh Claude session id", before != after)
    check("/new <text> runs the text as the first prompt", "start over" in fake.prompts[-1])
    check("the new turn opens with session_id=, not resume=",
          fake.specs[-1].claude_id == after and not fake.specs[-1].resume)


def run() -> list[str]:
    check = Checks("serialization: one run per session, collect, debounce, stop")
    for case in (pool_lock, collect_mode, debounce, stop_bypass, new_command):
        asyncio.run(case(check))
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
