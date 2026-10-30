#!/usr/bin/env python3
"""Fixes from the first live run, 2026-10-03.

    python3 -m tests.tg_bridge.fixes_live

  1. **Service messages are not answered.** "Topic created" carries no words or
     media and arrives before the first message of every new topic.
  2. **Read-only connector tools need no tap**, anything that writes or sends
     still does.
"""
import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import Checks
from jobs.tg_bridge.__main__ import has_content
from jobs.tg_bridge.agent import is_read_only_mcp
from jobs.tg_bridge.telegram import (CONFLICT_ALERT_S, STALL_EXIT_S, STALL_RESET_S, Poller,
                                     TelegramError, stall_action)

READ = ["mcp__gws__search_gmail_messages", "mcp__gws__get_gmail_messages_content_batch",
        "mcp__gws__get_gmail_thread_content", "mcp__gws__list_gmail_labels",
        "mcp__claude_ai_Google_Calendar__search_events", "mcp__claude_ai_Google_Calendar__list_events",
        "mcp__claude_ai_Google_Calendar__get_event", "mcp__claude_ai_Google_Calendar__suggest_time"]
WRITE = ["mcp__gws__send_gmail_message", "mcp__gws__draft_gmail_message",
         "mcp__gws__batch_modify_gmail_message_labels", "mcp__gws__modify_gmail_message_labels",
         "mcp__gws__manage_gmail_filter", "mcp__gws__start_google_auth",
         "mcp__claude_ai_Google_Calendar__create_event", "mcp__claude_ai_Google_Calendar__update_event",
         "mcp__claude_ai_Google_Calendar__delete_event", "mcp__claude_ai_Google_Calendar__respond_to_event",
         "Bash", "Read", "WebFetch"]


def run() -> list[str]:
    check = Checks("fixes from the first live run")
    check("a topic-created service message has no content",
          not has_content({"message_id": 4, "forum_topic_created": {"name": "x"}}))
    check("text, captions, photos and voice do", all(has_content(m) for m in (
        {"text": "hi"}, {"caption": "c", "photo": [{}]}, {"photo": [{"file_id": "a"}]},
        {"voice": {"file_id": "v"}})))
    for name in READ:
        check(f"no tap: {name.rsplit('__', 1)[-1]}", is_read_only_mcp(name))
    for name in WRITE:
        check(f"still asks: {name.rsplit('__', 1)[-1]}", not is_read_only_mcp(name))
    check("a poll 40s ago is healthy", stall_action(40, False) == "ok")
    check("past the reset threshold it reconnects", stall_action(STALL_RESET_S + 1, False) == "reset")
    check("but only once per stall", stall_action(STALL_RESET_S + 60, True) == "ok")
    check("past the exit threshold it exits for systemd to restart",
          stall_action(STALL_EXIT_S + 1, True) == "exit")
    run_conflicts(check)
    return check.fails


class _ConflictBot:
    """getUpdates answers 409 `n` times, then stops the poller."""
    def __init__(self, n, stop):
        self.n, self.stop = n, stop
    async def get_updates(self, offset, timeout):
        if self.n <= 0:
            self.stop.set(); return []
        self.n -= 1
        raise TelegramError("getUpdates", 409, "Conflict: terminated by other getUpdates request")
    async def delete_webhook(self): pass
    async def reset_transport(self): pass


async def _conflict(n, *, age):
    stop, alerts = asyncio.Event(), []
    async def alert(seconds): alerts.append(seconds)
    async def no_sleep(_): pass
    poller = Poller(_ConflictBot(n, stop), lambda: None, lambda u: None, sleep=no_sleep,
                    on_conflict=alert)
    if age:
        poller.conflict_since = time.monotonic() - age
    await poller.run(stop)
    return alerts


def run_conflicts(check):
    check("a short 409 at restart does not alert", asyncio.run(_conflict(3, age=0)) == [])
    alerts = asyncio.run(_conflict(5, age=CONFLICT_ALERT_S + 5))
    check("a 409 lasting past the threshold pushes an alert", len(alerts) == 1, alerts)


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
