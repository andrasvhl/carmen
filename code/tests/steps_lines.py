#!/usr/bin/env python3
"""tools/steps.py, offline: the step line format, normalize, diff and the history log.

    .venv/bin/python -m tests.steps_lines
"""
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile

TMP = pathlib.Path(tempfile.mkdtemp(prefix="steps-lines-"))
os.environ["HOME"] = str(TMP)
os.environ["VAULT_PATH"] = str(TMP / "vault")
os.environ["STEPS_DIR"] = str(TMP / "steps")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tests.tg_bridge._fakes import Checks  # noqa: E402
from tools import steps as S  # noqa: E402

check = Checks("tools/steps.py: step lines")
check("state lives in the scratch home", str(S.DIR).startswith(str(TMP)), S.DIR)
T = dt.date(2026, 11, 4)

# Parsing: old markers and new fields read the same.
a = S.parse_line("- [ ] Next: Confirm the refund landed (due Nov 12) (10 min)", T)
b = S.parse_line("- [ ] Next: Confirm the refund landed (10 min) 📅 2026-11-12 ^s4f2a", T)
check("old and new forms parse to the same step",
      (a.kind, a.text, a.est, a.due) == (b.kind, b.text, b.est, b.due) == ("Next", "Confirm the refund landed", "10 min", "2026-11-12")
      and b.id == "s4f2a", (a, b))
check("display keeps what older readers parse", b.display() == "Confirm the refund landed (due Nov 12) (10 min)", b.display())
c = S.parse_line("- [x] Then: Zelle Rahim $475 ✅ 2026-11-04 ^s77b0", T)
check("a done line", c.state == "done" and c.done == "2026-11-04" and c.text == "Zelle Rahim $475", c)
d = S.parse_line("- ~~Then: Ask Parrot (5 min)~~ (discarded, Nov 4) ^s1d0e", T)
check("a struck line", d.state == "struck" and d.text == "Ask Parrot" and d.est == "5 min" and d.note == "(discarded, Nov 4)"
      and S.format_line(d) == "- ~~Then: Ask Parrot (5 min)~~ (discarded, Nov 4) ^s1d0e", (d, S.format_line(d)))
check("a January date written in November is next year", S.parse_line("- [ ] x (due Jan 5)", T).due == "2027-01-05")
late = S.parse_line("- [ ] Next: Book it 📅 2026-11-12 ^s1a2b (15 min)", T)
check("an estimate appended after the fields still parses", (late.text, late.est, late.due, late.id) == ("Book it", "15 min", "2026-11-12", "s1a2b"), late)
mid = S.parse_line("- [ ] Next: Book it ^s1a2b (due Nov 12)", T)
check("our ID before an old due marker still parses", (mid.text, mid.due, mid.id) == ("Book it", "2026-11-12", "s1a2b"), mid)
check("not a step", S.parse_line("DoD: done well", T) is None and S.parse_line("- plain bullet", T) is None)
check("clean strips every field", S.clean("Next: Book it (due Nov 12) (1.5 h) ^s0001") == "Book it", S.clean("Next: Book it (due Nov 12) (1.5 h) ^s0001"))
for line in ("- [ ] Next: Confirm the refund landed (10 min) 📅 2026-11-12 ^s4f2a", "- [x] Then: Zelle Rahim $475 ✅ 2026-11-04 ^s77b0",
             "- [ ] Waiting: Laura, on a seat (Nov 3) ^s0a0a"):
    check(f"round trip: {line[:30]}", S.format_line(S.parse_line(line, T)) == line, S.format_line(S.parse_line(line, T)))

# normalize: fields and IDs, only inside Next action, idempotent, old ticks never dated.
NOTE = """---
type: action
task_id: t-abc123
status: open
---

## Next action
DoD: Refund in the account (owner decision, Nov 4)
- [ ] Next: Confirm the refund landed (due Nov 12) (10 min)
- [ ] Then: Reply to the WeTravel mail if not
- [x] Then: Zelle Rahim $475

## Notes
- [ ] a checkbox outside the block (due Nov 20)

## Log
- Nov 4 · step done: Zelle Rahim · check-in app
"""
n1 = S.normalize(NOTE, T)
n2 = S.normalize(n1, T)
st = S.steps_of(n1.partition("\n---\n")[2], T)
check("normalize gives every step an ID and the date as a field", all(s.id for s in st) and st[0].due == "2026-11-12"
      and "📅 2026-11-12" in n1 and "(due Nov 12)" not in n1.split("## Notes")[0], n1)
check("normalize is idempotent", n1 == n2)
check("an old tick is never dated today", "✅" not in n1)
check("outside the block nothing changes", "- [ ] a checkbox outside the block (due Nov 20)" in n1 and "DoD: Refund in the account (owner decision, Nov 4)" in n1)
check("IDs are unique", len({s.id for s in st}) == len(st))
dup = n1.replace("- [ ] Then: Reply", f"- [ ] Then: Reply to the WeTravel mail if not ^{st[1].id}\n- [ ] Then: Reply", 1)
check("a copied line gets its own ID", len({s.id for s in S.steps_of(S.normalize(dup, T).partition('\n---\n')[2], T)}) == 4)

# write + history: a tick is dated and logged with its source; an outside edit is logged as outside.
p = TMP / "Refund.md"
p.write_text(n1, encoding="utf-8")
S.observe(p)   # first sight: a snapshot, no events
ticked = n1.replace("- [ ] Next: Confirm", "- [x] Next: Confirm", 1)
out = S.write(p, ticked, "tap:step_done")
hist = [json.loads(l) for l in S.HISTORY.read_text().splitlines()]
check("a tick through write is dated today", f"✅ {dt.datetime.now(S.TZ).date().isoformat()}" in out, out)
check("and logged as done by its source", [(h["event"], h["source"], h["task_id"]) for h in hist] == [("done", "tap:step_done", "t-abc123")], hist)
edited = out.replace("Reply to the WeTravel mail if not", "Reply to the WeTravel refund mail if it has not landed", 1)
edited = edited.replace("- [x] Then: Zelle", "- [ ] Then: Call Parrot about the invoice (due Nov 9)\n- [x] Then: Zelle", 1)
p.write_text(edited, encoding="utf-8")   # Obsidian or a chat: nobody announces it
ev = S.observe(p)
check("an outside edit is seen: one reworded, one added with its date",
      sorted(e["event"] for e in ev) == ["added", "reworded"]
      and next(e for e in ev if e["event"] == "added")["due"] == "2026-11-09", ev)
again = S.observe(p)
check("the same version is not logged twice", again == [], again)
gone = edited.replace("- [ ] Then: Call Parrot about the invoice (due Nov 9)\n", "")
p.write_text(gone, encoding="utf-8")
check("a step deleted from the note is logged", [e["event"] for e in S.observe(p)] == ["deleted"])

# sweep: a chat's plain line gets its ID; an old note is converted without inventing ticks.
q = TMP / "Old.md"
q.write_text(NOTE.replace("t-abc123", "t-def456"), encoding="utf-8")
res = S.sweep([q], T)
check("sweep normalizes open notes", res == {"open": 1, "normalized": 1} and "^s" in q.read_text() and "✅" not in q.read_text(), res)
check("sweep again changes nothing", S.sweep([q], T) == {"open": 1, "normalized": 0})

print("\nall passed" if not check.fails else f"\n{len(check.fails)} FAILED")
sys.exit(1 if check.fails else 0)
