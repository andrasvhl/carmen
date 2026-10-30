#!/usr/bin/env python3
"""The inbox commit point, dedupe, and what a restart may and may not replay.

    python3 -m tests.tg_bridge.inbox_offsets

  1. **The update is on disk before the offset moves.** A crash between the two
     re-fetches the batch, and dedupe makes that harmless. The reverse order
     loses a message on every crash in that window.
  2. **Dedupe by update_id and by (chat_id, message_id).**
  3. **A row that reached the agent is never replayed after a restart.** A
     half-run tool may already have acted; the owner is told instead.
  4. **The token is never stored**, only its fingerprint beside the bot id.
"""
import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import TOKEN, Checks, make_bridge, update
from jobs.tg_bridge.inbox import ADOPTED, DEAD, DONE, PENDING, Inbox


def run() -> list[str]:
    check = Checks("inbox: commit point, dedupe, no replay after adoption")
    tmp = pathlib.Path(tempfile.mkdtemp())
    inbox = Inbox(tmp / "a.sqlite")
    key = "123456:fingerprint"

    batch = [update("one", uid=1), update("two", uid=2)]
    rows = inbox.store(batch, key)
    check("a batch is stored", len(rows) == 2)
    check("the offset is max(update_id) + 1 after the write", inbox.next_offset(key) == 3,
          inbox.next_offset(key))

    # Crash between write and offset: the rows are there, the offset is not.
    inbox2 = Inbox(tmp / "b.sqlite")

    def boom(*a, **kw):
        raise RuntimeError("killed between write and offset")

    inbox2._advance_offset = boom
    try:
        inbox2.store([update("three", uid=3)], key)
    except RuntimeError:
        pass
    check("after a crash the update is already on disk", inbox2.get(3) is not None)
    check("after a crash the offset did not move", inbox2.next_offset(key) is None)
    inbox3 = Inbox(tmp / "b.sqlite")            # the restarted process
    again = inbox3.store([update("three", uid=3)], key)
    check("the re-fetched batch dedupes by update_id", again == [], again)
    check("and the offset now advances", inbox3.next_offset(key) == 4)

    dup = update("one again", uid=50, mid=batch[0]["message"]["message_id"])
    check("a new update_id carrying a seen (chat, message_id) is dropped",
          inbox.store([dup], key) == [])
    check("the offset still moves past a duplicate-only batch", inbox.next_offset(key) == 51)

    inbox.adopt([1])
    pending, adopted = inbox.recover()
    check("recover splits pending from adopted",
          [r.update_id for r in pending] == [2] and [r.update_id for r in adopted] == [1])
    inbox.set_transcript(2, "secret words")
    inbox.finish([2])
    check("finishing a row erases its transcript (decision 2)", inbox.get(2).transcript is None
          and inbox.get(2).state == DONE)

    check("the token itself is never in the database",
          TOKEN.encode() not in (tmp / "a.sqlite").read_bytes())

    # The bridge on restart: adopted -> notice, pending -> processed.
    async def restart():
        bridge, transport, fake, tmp2 = make_bridge()
        a = update("send the mail to Quinn", uid=901)
        p = update("what is due today", uid=902)
        bridge.inbox.store([a, p], bridge.offset_key)
        bridge.inbox.adopt([901])
        await bridge.recover()
        await bridge.settle()
        return bridge, transport, fake

    bridge, transport, fake = asyncio.run(restart())
    notices = [t for t in transport.texts() if "interrupted by a restart" in t]
    check("an adopted message produces the restart notice", len(notices) == 1, transport.texts())
    check("the notice quotes the message", notices and "send the mail to Quinn" in notices[0])
    check("the adopted message is NOT replayed to the agent",
          not any("send the mail to Quinn" in p for p in fake.prompts), fake.prompts)
    check("the adopted row is dead afterwards", bridge.inbox.get(901).state == DEAD)
    check("a pending message is processed after restart",
          any("what is due today" in p for p in fake.prompts), fake.prompts)
    check("and finished", bridge.inbox.get(902).state == DONE)
    check("offset key is bot id plus fingerprint, not the token",
          bridge.offset_key.startswith("123456:") and "SECRET" not in bridge.offset_key)
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
