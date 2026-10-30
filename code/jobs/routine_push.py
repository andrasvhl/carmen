"""Timed: push the morning and evening routine reminders to his phone.

The text is never written here. Each reminder is a `[!push]` callout inside
Areas/Personal/Routines/Morning Routine.md or Evening Routine.md, so the owner edits what
he gets in Obsidian and the next push carries it. Built 2026-09-27 at his
request; component doc: _system/routine-push.md.

    > [!push] morning · 07:15 · silent
    > **Morning Routine**
    > 1. Straight onto the mat ...

Header, split on `·`: slot id, time (informational, checked against SLOTS),
priority word (silent / normal / strong), optional `device <name>`. The first
body line in **bold** is the notification title; the rest is the message.

One timer (automation@routine_push.timer) starts this service at 07:40
Mon-Fri, 09:00 Sat-Sun, and 21:30, 22:00 and 22:30 daily, Pacific. The job
works out which slot it is from the clock and weekday, so one unit serves all
five, and it refuses to send a reminder more
than WINDOW_MIN late: a 07:15 routine arriving at 11:00 after a reboot is
worse than none, which is also why the timers are not Persistent.

    ssh vault-host '~/automation/bin/run-job routine_push --dry-run --slot morning'
"""

import re
import sys
from urllib.parse import quote
from datetime import datetime
from zoneinfo import ZoneInfo

from runner import config, notify
from runner.job import Job, log, IMPORTANT, YOU

TZ = ZoneInfo("America/Los_Angeles")
MORNING = "Areas/Personal/Routines/Morning Routine.md"  # moved from Projects/<PROJECT> 2026-09-27
EVENING = "Areas/Personal/Routines/Evening Routine.md"
DAILY, WEEKDAYS, WEEKEND = range(7), range(5), (5, 6)
# slot -> (time the timer fires, note that holds its block, weekdays it sends on)
SLOTS = {
    "morning": ("07:40", MORNING, WEEKDAYS),
    "weekend": ("09:00", MORNING, WEEKEND),
    "stopwork": ("21:30", EVENING, DAILY),
    "bedready": ("22:00", EVENING, DAILY),
    "lightsout": ("22:30", EVENING, DAILY),
}
# Slots that send another slot's text. owner decision, 2026-10-05: the weekend morning
# is the same routine two hours later, so it reuses the `morning` callout and
# he edits one text, not two. The header time is then the owner's, not checked.
BORROWS = {"weekend": "morning"}
PRIORITY = {"silent": -1, "normal": 0, "strong": 1, "critical": 2}
# `critical` = Pushover emergency: plays through the mute switch and Focus on
# iOS. owner decision, 2026-09-27: rings, then repeats twice, two minutes apart
# (t = 0, 2, 4 min), and stops at once when acknowledged.
CRITICAL_RETRY_S = 120
CRITICAL_EXPIRE_S = 245
# Per-slot override of (retry, expire). owner decision, 2026-10-06: the 21:30 stop-work
# push rang twice and was missed, so it repeats every 5 min for up to 1 h,
# until he acknowledges it. Every other critical slot keeps the default.
CRITICAL_LIMITS = {"stopwork": (300, 3600)}
WINDOW_MIN = 20
# The name Obsidian knows the vault by on the phone; the folder on vault-host is `vault`.
OBSIDIAN_VAULT = "<VAULT_NAME>"

HEADER = re.compile(r"^>\s*\[!push\][+-]?\s*(.+)$")


def parse_blocks(text: str) -> dict[str, dict]:
    """Every `[!push]` callout in a note, keyed by slot id."""
    blocks, cur = {}, None
    for line in text.splitlines():
        m = HEADER.match(line)
        if m:
            parts = [p.strip() for p in m.group(1).split("·")]
            cur = {"slot": parts[0].lower(), "time": "", "priority": "normal",
                   "device": None, "lines": []}
            for p in parts[1:]:
                if re.fullmatch(r"\d{1,2}:\d{2}", p):
                    cur["time"] = p.zfill(5)
                elif p.lower() in PRIORITY:
                    cur["priority"] = p.lower()
                elif p.lower().startswith("device "):
                    cur["device"] = p.split(None, 1)[1].strip()
            blocks[cur["slot"]] = cur
            continue
        if cur is not None and line.startswith(">"):
            cur["lines"].append(re.sub(r"^>\s?", "", line))
        else:
            cur = None
    return blocks


def plain(line: str) -> str:
    """Obsidian markdown to phone text: links to their label, no emphasis marks."""
    line = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", line)
    line = re.sub(r"\[\[([^\]]+)\]\]", r"\1", line)
    return re.sub(r"(\*\*|__|\*|`)", "", line).rstrip()


LINK = re.compile(r"^\s*Link:\s*\[\[([^\]|]+)(?:\|([^\]]+))?\]\]\s*$", re.I)


def obsidian_uri(note: str) -> str:
    return ("obsidian://open?vault=" + quote(OBSIDIAN_VAULT)
            + "&file=" + quote(note.removesuffix(".md"), safe=""))


def render(block: dict) -> tuple[str, str, tuple[str, str] | None]:
    """Title, body, and an optional (url, label) from a `Link: [[Note|Label]]` line.

    The Link line is taken out of the text and sent as Pushover's tappable URL,
    opening the note in Obsidian (added 2026-09-28 for GSB Outfits)."""
    lines, link = [], None
    for l in block["lines"]:
        m = LINK.match(l)
        if m and link is None:
            link = (obsidian_uri(m.group(1)), m.group(2) or m.group(1).rsplit("/", 1)[-1])
        else:
            lines.append(l)
    while lines and not lines[0].strip():
        lines.pop(0)
    title = "Routine"
    if lines and re.fullmatch(r"\*\*.+\*\*", lines[0].strip()):
        title = plain(lines.pop(0).strip())
    body = "\n".join(plain(l) for l in lines).strip()
    return title, body, link


def pick_slot(now: datetime) -> str | None:
    """The slot whose time is within WINDOW_MIN minutes before now."""
    for slot, (hhmm, _, days) in SLOTS.items():
        if now.weekday() not in days:
            continue
        h, m = map(int, hhmm.split(":"))
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if 0 <= (now - due).total_seconds() <= WINDOW_MIN * 60:
            return slot
    return None


def main() -> int:
    forced = None
    if "--slot" in sys.argv:
        forced = sys.argv[sys.argv.index("--slot") + 1]
    with Job("routine_push", description="morning and evening routine reminders",
             watches=("routine-push",)) as job:
        now = datetime.now(TZ)
        slot = forced or pick_slot(now)
        if slot is None:
            log(f"routine_push: no slot due at {now:%H:%M} Pacific, sending nothing")
            return 0
        if slot not in SLOTS:
            job.check("routine slot known", ok=False, tier=IMPORTANT, owner=YOU,
                      covers="routine-push", detail=f"unknown slot {slot!r}")
            return 0
        hhmm, rel, _ = SLOTS[slot]
        block_id = BORROWS.get(slot, slot)
        path = config.VAULT / rel
        try:
            block = parse_blocks(path.read_text(encoding="utf-8")).get(block_id)
        except OSError as exc:
            block = None
            log(f"routine_push: cannot read {rel}: {exc}")
        if not block or not render(block)[1]:
            job.check(f"[!push] {slot} block present", ok=False, tier=IMPORTANT, owner=YOU,
                      covers="routine-push",
                      detail=f"no `> [!push] {block_id} · ...` callout with text in {rel}, "
                             f"so the {hhmm} reminder was not sent")
            return 0
        if block_id == slot and block["time"] and block["time"] != hhmm:
            job.check(f"{slot} header time matches the timer", ok=False, tier=IMPORTANT,
                      owner=YOU, covers="routine-push",
                      detail=f"{rel} says {block['time']}, the timer fires at {hhmm}. "
                             f"Sent at {hhmm}; changing the time needs the timer changed too")
        title, body, link = render(block)
        prio = PRIORITY[block["priority"]]
        limits = CRITICAL_LIMITS.get(slot, (CRITICAL_RETRY_S, CRITICAL_EXPIRE_S))
        job.record("slot", slot)
        job.record("chars", len(body))
        if job.dry_run:
            log(f"routine_push [dry run] {slot} prio={prio} device={block['device']}"
                + (f" retry={limits[0]}s expire={limits[1]}s" if prio == 2 else "") + "\n"
                f"--- {title}\n{body}" + (f"\n[link] {link[1]} -> {link[0]}" if link else ""))
            return 0
        try:
            notify.send_push(title, body, priority=prio, device=block["device"],
                             url=link[0] if link else None,
                             url_title=link[1] if link else None,
                             **(dict(zip(("retry", "expire"), limits))
                                if prio == 2 else {}))
            ok, detail = True, f"sent {slot} ({block['priority']}, {len(body)} chars)"
        except notify.PushUnavailable as exc:
            ok, detail = False, f"{slot} not sent: {exc}"
        job.check(f"{slot} reminder sent", ok=ok, tier=IMPORTANT, owner=YOU,
                  covers="routine-push", detail=detail)
        log(f"routine_push: {detail}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
