#!/usr/bin/env python3
"""The account's claude.ai connectors never act without the approver.

    python3 -m tests.tg_bridge.connector_rules

  1. **Every claude.ai connector tool is an ask rule**, so under bypassPermissions each call
     reaches can_use_tool (Nov 9: a QA sandbox booked 3 events through the Calendar connector
     because nothing covered it).
  2. **Reads still run without a tap**, writes do not.
  3. **Carmen's no-guest calendar block stays tap-free**; one with guests asks.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import Checks
from jobs.tg_bridge import agent


def run() -> list[str]:
    check = Checks("claude.ai connector rules")
    ask = json.loads(agent.settings_json())["permissions"]["ask"]
    check("mcp__claude_ai_* is in the settings the CLI gets", "mcp__claude_ai_*" in ask)
    check("the gws mail send still asks", "mcp__gws__send_gmail_message" in ask)
    for name in ("mcp__claude_ai_Gmail__search_threads", "mcp__claude_ai_Gmail__get_thread",
                 "mcp__claude_ai_Google_Calendar__list_events"):
        check(f"read runs without a tap: {name}", agent.is_read_only_mcp(name))
    for name in ("mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Gmail__send_message",
                 "mcp__claude_ai_Google_Calendar__create_event",
                 "mcp__claude_ai_Google_Calendar__update_event",
                 "mcp__claude_ai_Google_Calendar__respond_to_event",
                 "mcp__claude_ai_Google_Drive__create_file"):
        check(f"write asks: {name}", not agent.is_read_only_mcp(name))
    try:
        from jobs.checkin_app.chat import auto_allowed
    except Exception as exc:  # the chat module needs the app's deps; skip, don't fail
        print(f"  skip  auto_allowed ({type(exc).__name__})")
    else:
        ev = "mcp__claude_ai_Google_Calendar__create_event"
        check("a no-guest block is tap-free", auto_allowed(ev, {"summary": "Focus"}))
        check("guests ask", not auto_allowed(ev, {"attendees": [{"email": "x@example.com"}]}))
        check("deprecated guest field asks", not auto_allowed(ev, {"attendeeEmails": ["x@example.com"]}))
        check("moving an event asks", not auto_allowed(
            "mcp__claude_ai_Google_Calendar__update_event", {"eventId": "1"}))
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
