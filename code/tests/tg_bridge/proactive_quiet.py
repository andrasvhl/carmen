#!/usr/bin/env python3
"""Phase 6, the vault speaks first: quiet hours, the channel, and the taps.

    python3 -m tests.tg_bridge.proactive_quiet

  1. **Quiet hours come from the vault**, the Sleep row of working-conditions,
     and a row that stops parsing falls back to fixed defaults rather than
     messaging at 3am or never.
  2. **send_telegram goes silent inside quiet hours** and carries buttons;
     callback_data over 64 bytes is refused before Telegram can.
  3. **A brief button creates exactly one task**, from the item read back out
     of the brief note, only for the owner, then removes the buttons.
  4. **Replying to a proactive message continues it**: the quoted text reaches
     the turn, marked untrusted.
"""
import asyncio
import io
import json
import pathlib
import sys
from datetime import datetime, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import OWNER, STRANGER, Checks, callback, make_bridge, update
from jobs.tg_bridge import proactive
from runner import config, notify, quiet_hours as Q

ROW = ("| Sleep | **Wake 07:30-07:45 weekdays, ~09:00 weekends.** Wind-down from 21:30, "
       "lights off ~23:00 (later when going out). Never place work at 08:00 (2026-09-23) |")


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=Q.TZ)


def quiet(check):
    win = Q.parse(f"| | |\n|---|---|\n{ROW}\n| Weekdays | 09:00 |")
    check("the Sleep row parses", win.source == "vault"
          and (win.lights_off, win.weekday_wake, win.weekend_wake)
          == (time(23, 0), time(7, 45), time(9, 0)), win)
    check("a wake range means awake by its later end", win.weekday_wake == time(7, 45))
    for stamp, want, why in (
            ("2026-10-05 23:30", True, "Monday night after lights off"),
            ("2026-10-06 07:44", True, "Tuesday before the weekday wake"),
            ("2026-10-06 07:46", False, "Tuesday after the weekday wake"),
            ("2026-10-09 22:59", False, "Friday just before lights off"),
            ("2026-10-10 08:30", True, "Saturday morning before the weekend wake"),
            ("2026-10-11 09:01", False, "Sunday after the weekend wake"),
            ("2026-10-12 08:30", False, "Monday 08:30, a weekday again")):
        check(f"quiet hours: {why}", Q.is_quiet(at(stamp), win) is want)
    check("quiet ends at the next wake",
          Q.next_loud(at("2026-10-09 23:30"), win) == at("2026-10-10 09:00"))
    for text, why in (("no table at all", "no Sleep row"),
                      ("| Sleep | whenever I feel like it |", "did not parse")):
        w = Q.parse(text)
        check(f"fallback when there is {why}",
              w.source.startswith("defaults") and why in w.source
              and (w.lights_off, w.weekday_wake) == (time(23, 0), time(7, 45)), w)


def channel(check):
    original_env, original_open, original_quiet = (config.load_env,
                                                   notify.urllib.request.urlopen, Q.is_quiet)
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def ok_open(req, timeout=0):
        seen["data"] = req.data.decode()
        return Resp(json.dumps({"ok": True, "result": {"message_id": 9}}).encode())

    try:
        config.load_env = lambda: {"TELEGRAM_BOT_TOKEN": "1:x", "TELEGRAM_OWNER_ID": "4242"}
        notify.urllib.request.urlopen = ok_open
        Q.is_quiet = lambda *a, **k: True
        notify.send_telegram("night")
        check("inside quiet hours the message is silent", "disable_notification=true" in seen["data"])
        Q.is_quiet = lambda *a, **k: False
        notify.send_telegram("day")
        check("outside quiet hours it is not", "disable_notification" not in seen["data"])
        notify.send_telegram("forced", silent=True)
        check("silent=True overrides the clock", "disable_notification=true" in seen["data"])

        notify.send_telegram("item", buttons=proactive.brief_buttons("2026-10-05", 1))
        from urllib.parse import parse_qs
        markup = json.loads(parse_qs(seen["data"])["reply_markup"][0])
        check("buttons travel as an inline keyboard",
              markup == {"inline_keyboard": [
                  [{"text": "Let Claude fix it", "callback_data": "mb1:2026-10-05:1:f"},
                   {"text": "Create task", "callback_data": "mb1:2026-10-05:1:c"}],
                  [{"text": "Skip", "callback_data": "mb1:2026-10-05:1:s"}]]}, markup)
        try:
            notify.send_telegram("x", buttons=[[("too long", "y" * 65)]])
            refused = False
        except ValueError:
            refused = True
        check("callback_data over 64 bytes is refused before sending", refused)
    finally:
        config.load_env, notify.urllib.request.urlopen, Q.is_quiet = (original_env,
                                                                      original_open,
                                                                      original_quiet)

    check("the callback format round-trips",
          proactive.parse_callback(proactive.brief_callback("2026-10-05", 3, "s"))
          == ("2026-10-05", 3, "s"))
    check("anything else is not a brief button",
          all(proactive.parse_callback(d) is None
              for d in ("tga1:abc:a", "mb1:2026-10-05:1:x", "mb1:bad:1:c", "")))


BRIEF = """# Maintenance brief

## Items, machine-readable

```maintenance-brief-items
{"brief": "2026-10-05", "items": [
 {"id": "mb-2026-10-05-1", "rank": 1, "title": "North Star is out of date",
  "next_action": "Open North Star and re-form the goals", "effort": "1 h",
  "what": "Review was due 2026-09-15.", "why": "Every prioritisation pass reads it."},
 {"id": "mb-2026-10-05-2", "rank": 2, "title": "Routines past their horizon",
  "next_action": "Confirm both routines", "effort": "30 min"}]}
```
"""


def taps(check):
    async def scenario():
        bridge, transport, fake, tmp = make_bridge()
        briefs = bridge.vault / "_system" / "maintenance" / "briefs"
        briefs.mkdir(parents=True)
        (briefs / "2026-10-05.md").write_text(BRIEF)
        created = []

        def fake_create(item, brief_date):
            created.append(item)
            return pathlib.Path(f"/vault/Inbox/{item['title']}.md")

        bridge.brief_buttons.create = fake_create

        def answers():
            return [d.get("text") for d in transport.sent("answerCallbackQuery")]

        await bridge.on_updates([callback("mb1:2026-10-05:1:c", sender=STRANGER)])
        await bridge.settle()
        check("a stranger's tap creates nothing", created == [])

        await bridge.on_updates([callback("mb1:2026-10-05:1:c", message_id=50)])
        await bridge.settle()
        check("the owner's Create tap creates one task", len(created) == 1, created)
        check("...from the item in the brief note, next action included",
              created and created[0]["next_action"] == "Open North Star and re-form the goals")
        edits = transport.sent("editMessageText")
        check("...and the message says so, with the buttons removed",
              edits and "Task created: North Star is out of date" in edits[-1]["text"]
              and not edits[-1].get("reply_markup") and edits[-1]["message_id"] == 50, edits)

        await bridge.on_updates([callback("mb1:2026-10-05:1:c", message_id=50)])
        await bridge.settle()
        check("a second tap on the same item creates nothing more",
              len(created) == 1 and answers()[-1] == "Already handled.", answers())

        await bridge.on_updates([callback("mb1:2026-10-05:2:s", message_id=51)])
        await bridge.settle()
        check("Skip creates nothing and says Skipped",
              len(created) == 1 and answers()[-1] == "Skipped.", answers())

        await bridge.on_updates([callback("mb1:2026-10-05:9:c")])
        await bridge.settle()
        check("an item that is not in the brief is refused",
              len(created) == 1 and answers()[-1] == "That brief item is gone.", answers())

        def broken_create(item, brief_date):
            raise OSError("disk full")

        bridge.brief_buttons.create = broken_create
        (briefs / "2026-10-06.md").write_text(BRIEF.replace("2026-10-05", "2026-10-06"))
        await bridge.on_updates([callback("mb1:2026-10-06:1:c")])
        await bridge.settle()
        check("a failed create says so", answers()[-1] == "Could not create the task.", answers())
        bridge.brief_buttons.create = fake_create
        await bridge.on_updates([callback("mb1:2026-10-06:1:c")])
        await bridge.settle()
        check("...and the item can be tapped again", len(created) == 2, created)

        await bridge.on_updates([update("Snooze the North Star one?", reply_to_message={
            "message_id": 50, "text": "1. North Star is out of date\n\nNext: re-form the goals",
            "from": {"id": 123456, "is_bot": True}})])
        await bridge.settle()
        check("replying to a proactive message carries it into the turn, untrusted",
              fake.prompts and "North Star is out of date" in fake.prompts[-1]
              and "EXTERNAL_UNTRUSTED_CONTENT" in fake.prompts[-1], fake.prompts[-1:])

    asyncio.run(scenario())


def real_task(check):
    """The real write, into a scratch Inbox: schema, order and the reasoning."""
    import tempfile
    from tools import todos
    original = todos.INBOX_DIR
    todos.INBOX_DIR = tempfile.mkdtemp()
    try:
        item = {"rank": 1, "title": "North Star is out of date",
                "what": "Review was due 2026-09-15.", "why": "Every prioritisation pass reads it.",
                "next_action": "Open North Star and re-form the goals", "effort": "1 h"}
        text = proactive.create_task(item, "2026-10-05").read_text()
    finally:
        todos.INBOX_DIR = original
    head, _, body = text.partition("\n---\n")
    check("the task keeps the capture schema", "type: action" in head and "status: inbox" in head,
          head)
    check("the next action comes first, as a checkbox",
          body.lstrip().startswith("## Next action\n- [ ] Next: Open North Star"), body)
    check("the reasoning follows under ## Facts",
          body.index("## Next action") < body.index("## Facts (maintenance brief, 2026-10-05)")
          and "**What is wrong:** Review was due" in body and "**Why it matters:**" in body
          and "**Effort:** 1 h" in body, body)
    check("...with a Source link back to the brief and its item",
          "Source: [[_system/maintenance/briefs/2026-10-05|Maintenance brief 2026-10-05]], item 1"
          in body, body)
    check("no effort property is invented", "effort:" not in head, head)


def run() -> list[str]:
    check = Checks("proactive: quiet hours, the channel, brief buttons, replies")
    quiet(check)
    channel(check)
    taps(check)
    real_task(check)
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
