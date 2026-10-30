#!/usr/bin/env python3
"""The session log places days by itself: idempotent, rolls old days, refuses bad answers.

The model only ever returns one day's three sections. Everything about the page
(order, replacement, the 7-day window, history) is Python, and is held here.

    ~/automation/.venv/bin/python -m tests.session_log
"""
import pathlib, sys
from datetime import date, datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from runner.ask import AskFailed
import jobs.session_log as sl

TZ = sl.TZ
GOOD = ("### Worked on\n- **Canvas sync** shipped. (Mac)\n\n"
        "### Tasks and build items\n- [[Real task]]: done, possibly stale: still open\n"
        "- [[Made up task]]: touched\n\n### Open threads\n- none")
fails = []


def refuses(text, why):
    try:
        sl._guard(text, {"Real task"})
    except AskFailed:
        return
    fails.append(f"should have refused: {why}")


# guard: the happy path passes and an unknown link is unlinked, not fatal
out = sl._guard(GOOD, {"Real task"})
if "[[Real task]]" not in out or "[[Made up task]]" in out or "Made up task" not in out:
    fails.append(f"link handling wrong: {out}")
refuses("Here is the day:\n" + GOOD, "preamble")
refuses(GOOD.replace("### Open threads", "### Loose ends"), "missing heading")
refuses("### Open threads\n- x\n### Worked on\n- y\n### Tasks and build items\n- z",
        "headings out of order")
refuses(GOOD + "\n## 2026-10-01 (Thu)\n", "a day heading that would split the page")

# placing: idempotent, newest first, window of 7, old days roll to history verbatim
today = date(2026, 10, 12)
evening = datetime(2026, 10, 12, 21, 0, tzinfo=TZ)
morning_after = datetime(2026, 10, 13, 4, 30, tzinfo=TZ)
days = {(today - timedelta(days=i)).isoformat():
        sl._section(today - timedelta(days=i), GOOD, morning_after) for i in range(8)}
page, hist = sl.place("", "", days, today, evening)
_, kept = sl._split(page)
if len(kept) != 7:
    fails.append(f"window holds {len(kept)} days, not 7")
if sorted(kept, reverse=True) != list(kept):
    fails.append("days not newest first")
oldest = (today - timedelta(days=7)).isoformat()
if oldest in kept or oldest not in hist:
    fails.append("day 8 did not roll into history")
if days[oldest].strip() not in hist:
    fails.append("rolled day was not moved verbatim")

again, hist2 = sl.place(page, hist, {today.isoformat(): days[today.isoformat()]}, today, evening)
if sl._split(again)[1] != kept:
    fails.append("re-placing the same day changed the page")
if hist2 != hist:
    fails.append("re-placing rewrote the history")

replaced = sl._section(today, GOOD.replace("Canvas sync", "Something new"), evening)
p3, _ = sl.place(page, hist, {today.isoformat(): replaced}, today, evening)
if p3.count(f"## {today.isoformat()}") != 1 or "Something new" not in p3:
    fails.append("today was duplicated rather than replaced")

# history keeps everything: a second roll adds, never drops
later = today + timedelta(days=1)
_, hist3 = sl.place(page, hist, {later.isoformat(): sl._section(later, GOOD, morning_after)},
                    later, morning_after)
if oldest not in hist3 or (today - timedelta(days=6)).isoformat() not in hist3:
    fails.append("second roll lost a history day")

# due days: a day written in progress is redone, a final one is not
in_progress = sl._section(today, GOOD, evening)
final_y = sl._section(today - timedelta(days=1), GOOD, evening)
pg, _ = sl.place("", "", {today.isoformat(): in_progress,
                          (today - timedelta(days=1)).isoformat(): final_y}, today, evening)
if sl.due_days(pg, today, 2) != [today]:
    fails.append(f"due_days wrong: {sl.due_days(pg, today, 2)}")
if sl.FINAL in in_progress or sl.FINAL not in final_y:
    fails.append("final marker set on the wrong section")

# a session spanning midnight shows on both days, its ending only on the last
s = {"machine": "mac", "kind": "desktop", "title": "t", "cwd": "", "files": [], "commits": [],
     "actions": [], "final": "ended", "end": "2026-10-12T08:00:00+00:00",
     "prompts": [{"at": "2026-10-12T06:30:00+00:00", "text": "late"},   # 23:30 PT on the 11th
                 {"at": "2026-10-12T07:30:00+00:00", "text": "early"}]}  # 00:30 PT on the 12th
d11, d12 = sl._for_day([s], date(2026, 10, 11)), sl._for_day([s], date(2026, 10, 12))
if [p["text"] for p in d11[0]["prompts"]] != ["late"] or d11[0]["final"]:
    fails.append("first day of a midnight session wrong")
if [p["text"] for p in d12[0]["prompts"]] != ["early"] or d12[0]["final"] != "ended":
    fails.append("second day of a midnight session wrong")

print("\n".join(f"FAIL: {f}" for f in fails) if fails else
      "ok - guard refuses preamble, missing/misordered headings and day headings; placing is "
      "idempotent, newest first, 7 days, rolls verbatim; midnight sessions split")
sys.exit(1 if fails else 0)
