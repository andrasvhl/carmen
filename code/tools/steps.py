"""Step lines of a task note as data (owner decision, Nov 4: "substeps, due dates for substeps, and history
of substeps should be somehow stable ... the purely text-based approach seems to be very
unreliable").

A step is one line in `## Next action`, one action per line, its facts as fields at the end:

    - [ ] Next: Confirm the $3,250 refund landed (10 min) 📅 2026-11-12 ^s4f2a
    - [ ] Then: Reply to the WeTravel refund mail if not 📅 2026-11-12 ^s9c11
    - [x] Then: Zelle Ben the remaining $475 ✅ 2026-11-04 ^s77b0
    - ~~Then: Ask Parrot for an invoice~~ (discarded, Nov 4) ^s1d0e

  - `(10 min)` / `(1.5 h)`: the estimate, as before.
  - `📅 YYYY-MM-DD`: the step's own deadline (the Tasks-plugin convention; was "(due Nov 12)").
  - `✅ YYYY-MM-DD`: the day it was done.
  - `^sxxxx`: an Obsidian block ID, hidden in reading view: the step's identity, so a step keeps
    its history when its text, date or place changes.
  - `(after: ^s2765)` or `(after: ^s2765, ^s70e2)`: the step needs those steps or `Waiting:` lines
    done first (Nov 10, waits per step). It sits after the words, before the estimate. The caret ids
    in brackets keep it apart from the older backward links: a Waiting line's `· for: <step>` (the
    step that became the wait) and `(label, after: <step>)` (the step he just did), which name a
    step in words, never by id. A step is blocked while any target is open; a step without the tag
    is never blocked. A task is waiting only when a Waiting line is open and no unblocked step of
    his is (`task_waiting`).
  - `(optional)`: a nice-to-have (Nov 11, the owner: "i would prefer a word though"). It sits after
    the words and any `(after: ...)`, before the estimate. An optional step never becomes the Next
    on its own (`_promote` in the app skips it), planning counts it only as filler for spare
    minutes, and a task that closes strikes it "(not needed, <Mon D>)", never ticks it.

`normalize` turns any line into that form (old "(due Nov 13)" markers, missing IDs, a tick without
its date), so every writer may keep writing plain lines. `diff` compares two versions by ID (by
text for lines without one) and `record` appends what changed to the step history,
`state/steps/history.jsonl`, with who changed it: what the review reads to get better at guessing
steps (owner decision, Nov 4: "keep logging also where steps changed etc so we get better at guessing
substeps"). `observe` does the same for edits nobody announced (Obsidian, a chat, prep), against
the last snapshot of each note.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import fcntl
import json
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from pathlib import Path
from zoneinfo import ZoneInfo

from runner import config

TZ = ZoneInfo("America/Los_Angeles")


def _today() -> dt.date:
    """His day, the Pacific date, read through runner.clock so a frozen AUTOMATION_NOW holds here too
    (Nov 12: a close's checkmark date came from the wall clock, not the test's frozen day)."""
    from runner import clock
    return clock.today()
# STEPS_DIR points it elsewhere: every test suite sets it, so no test writes the live history.
DIR = Path(os.environ["STEPS_DIR"]) if os.environ.get("STEPS_DIR") else config.STATE_DIR / "steps"
HISTORY = DIR / "history.jsonl"
SNAPS = DIR / "snap"   # one file per task: its steps as last seen

# --- the one place for every fact about a step line (Nov 11, the owner: "Why do we double the rules here
# should there not be a single rule will these not drift?"). Every writer, reader, the task hook, the
# contract and Carmen build on these; tests/checkin_tasks_v2 fails if a copy appears elsewhere.
MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
MONTHS_RX = "|".join(MON)                       # "Jan|Feb|...": match with \w* or [a-z]* after it
WEEKDAYS_RX = "Mon|Tue|Wed|Thu|Fri|Sat|Sun"
WDAY_OPT = rf"(?:(?:{WEEKDAYS_RX})\w*,?\s+)?"   # an optional weekday before a date
DATE_RX = rf"({MONTHS_RX})\w*\.?\s+(\d{{1,2}})\b"   # "Nov 13": (month, day)
# Words that make the date after them a deadline. START_WORDS: a date the step must begin on.
DUE_WORDS_RX = r"by|before|until|due|no later than|deadline:?"
START_WORDS_RX = r"(?:must|has to|needs to) (?:start|begin)(?: on)?"
# An estimate as anyone writes it: "(30 min)", "(2 h)", "(1,5 hours)", "(timebox 20 min)". page.html's
# NOMIN and EST_ANY are the same pattern (tests/checkin_tasks_v2 holds them equal, audit A2).
EST_RX = r"\((?:timebox )?(\d+(?:[.,]\d+)?)\s*(min|h|hours?|hrs?)\)"
EST_IN = re.compile(r"\s*" + EST_RX, re.I)       # anywhere in a line or text
# His attribution, anywhere in the words ("(owner decision, Nov 11)"): the same as page.html's clean() (audit A3).
HIS_TAG = re.compile(r"\s*\(the owner\b[^)]*\)")
# Any estimate-looking bracket, the forms a hand writes ("(~30m)", "(2 hrs)"): the form check only.
EST_LOOSE = re.compile(r"\(\s*~?\d+(?:[.,]\d+)?\s*(?:m|mins?|minutes?|h|hrs?|hours?)\s*\)\s*$", re.I)
EST_STRICT = re.compile(r" \(\d+(?:\.\d+)? (?:min|h)\)\s*$")
FIELDS_TAIL = re.compile(r"(?:\s+(?:📅|✅)\s*\d{4}-\d{2}-\d{2}|\s+\^[A-Za-z0-9-]+)+\s*$")
LINE = re.compile(r"^(\s*)- (\[ \]|\[[xX]\]|~~)\s?(?:(Next|Then|Waiting):\s*)?(.*)$")
BLOCK_ID = re.compile(r"\s+\^([A-Za-z0-9-]+)\s*$")
OUR_ID = re.compile(r"\s+\^(s[0-9a-f]{4,8})(?=\s|$)")   # ours, wherever an older writer left it
DUE_F = re.compile(r"\s*📅\s*(\d{4}-\d{2}-\d{2})")
DONE_F = re.compile(r"\s*✅\s*(\d{4}-\d{2}-\d{2})")
# "(due Nov 13)": the old marker in a note, and the marker Step.display() writes for the app.
DUE_OLD = re.compile(rf"\s*\(due {WDAY_OPT}([A-Z][a-z]{{2}})\w*\.?\s+(\d{{1,2}})\)")
# A deadline named only in the words ("by Wed Nov 11, 12:00", "before Nov 18"), Nov 10: a session
# wrote three and Carmen showed none. An open step gets it as its 📅 field ("before" = the day
# before); the words stay. Not a condition ("if nothing by Nov 6, open a dispute"). The task hook
# (check_tasks.STEP_DATED_PROSE) blocks the same thing in Claude's writes.
# Nov 11: "must start by Nov 18" was already "by"; "no later than", "deadline" and "must start Nov 18"
# (a date the step has to begin on) count too.
DUE_PROSE = re.compile(rf"\b({DUE_WORDS_RX}|{START_WORDS_RX})\s+{WDAY_OPT}{DATE_RX}", re.I)
COND_BEFORE = re.compile(r"\bif\b[^,;]*$", re.I)
# A date gate (Nov 11): "Fill in the e-Arrival card from Nov 17" cannot happen before that day. Not a
# deadline: the step is not free until then (`Step.gate`, read from the words, never written). A
# range ("from Nov 20 to Nov 21") is not a gate.
GATE_PROSE = re.compile(rf"\b(?:from|not before|no earlier than|opens(?: on)?|starting|after)\s+{WDAY_OPT}"
                        rf"{DATE_RX}(?!\s*(?:to|until|till|through|-|–)\s)", re.I)
# Any date in a step's words, with an optional weekday ("Fri Nov 13", "Nov 13").
DATE_WORDS = re.compile(rf"\b((?:{WEEKDAYS_RX})\w*,?\s+)?{DATE_RX}")
# Deadline words right before a date ("by noon Fri Nov 13", "due Nov 13").
DUE_WORD_BEFORE = re.compile(rf"\b(?:{DUE_WORDS_RX})"
                             r"(?:\s+(?:noon|midnight|eod|end of day|\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?,?\s+$", re.I)
# Words that point at something to open: "link in their mail", "the reminder email", "the form".
POINTS_AT = re.compile(r"\b(?:link|links|the \w+ (?:e-?mail|mail)|(?:their|her|his|the) (?:e-?mail|mail)|"
                       r"(?:e-?mail|mail) from|the form|the portal)\b(?!\s+(?:drafted|below|above))", re.I)
HAS_LINK = re.compile(r"https?://|\[\[")
# A wikilink, its note optional: [[Note]], [[Note#Section|label]], and [[#Section|label]], a section of
# the note it is written in (Nov 11: a Carmen chat wrote one under an HR step and the card showed text).
WIKILINK = re.compile(r"\[\[([^\]|#]*)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")


def wikilink(item: str, note: str) -> tuple[str, str, str] | None:
    """(note, section, label) of a workspace line that is one wikilink; a link with no note names
    `note`, the one it is written in. None for anything else (a URL, a [label](url))."""
    m = WIKILINK.fullmatch((item or "").strip())
    if not m or not (m.group(1).strip() or m.group(2)):
        return None
    name, sec, label = m.group(1).strip() or note, (m.group(2) or "").strip(), (m.group(3) or "").strip()
    return name, sec, label or sec or name


# Tags of notes whose open boxes are a form, not steps. The build brief (jobs/build_brief.py) is a
# review sheet: "process build brief" reads which boxes he ticked in each section and the answer line
# under each; as Then steps its 5 to 21 items would flood Carmen. The one exemption (Nov 11).
FORM_NOTE_TAGS = ("build-brief",)


# D-17
def stray_boxes(text: str) -> list[str]:
    """Open checkboxes outside `## Next action` in a task note (Nov 11: the HR note's `## Steps`
    checklist showed on Carmen half-parsed). Steps live in the block only (SYSTEM.md: "never a new
    checklist further down"). [] for a note that is not an open task or is a form (FORM_NOTE_TAGS)."""
    head, sep, body = (text or "").partition("\n---\n")
    if not sep or not is_task(text) or not re.search(r"(?m)^status:\s*(inbox|open|in-progress)\s*$", head):
        return []
    if any(re.search(rf"(?m)^\s*-\s*{re.escape(t)}\s*$|^tags:.*\b{re.escape(t)}\b", head) for t in FORM_NOTE_TAGS):
        return []
    lines = body.split("\n")
    r = block_range(lines)
    return [l.strip() for i, l in enumerate(lines)
            if re.match(r"^\s*- \[ \]", l) and not (r and r[0] < i < r[1])]


def month_day(mon: str, day, today: dt.date) -> str:
    """"Nov", 13 as YYYY-MM-DD, the year that makes sense from today (a date 60+ days back is next
    year's), or ""."""
    m = MON.get(str(mon)[:3].title())
    return _year(m, int(day), today) if m else ""


def strip_marks(text: str, est: bool = True, due: bool = True, ids: bool = True, optional: bool = False) -> str:
    """A step's display text without its estimate "(30 min)" / "(2 h)", its "(due Nov 13)" marker,
    its ^id, and with optional=True its "(optional)": what every reader that shows or compares
    step words takes off."""
    t = text or ""
    if due:
        t = DUE_OLD.sub("", t)
    if est:
        t = EST_IN.sub("", t)
    if ids:
        t = re.sub(r"\s*\^s[0-9a-f]{4,8}\b", "", t)
    if optional:
        t = OPTIONAL.sub("", t)
    return t.strip()


def est_min(text: str) -> int | None:
    """The minutes of the estimate a step's text ends with ("(30 min)", "(1.5 h)", a timebox,
    a "(due ...)" marker after it allowed), or None."""
    m = re.search(EST_RX + r"\s*$", DUE_OLD.sub("", text or "").rstrip(), re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    return int(round(n * 60)) if m.group(2).lower().startswith("h") else int(round(n))


# The form rules (Nov 11): what a step line must not do. One code each; check_tasks reports them
# for Claude's writes, steps.write records them for the lines a write added or changed, and the
# contract shows them on Train. {code: (title, why)}.
FORM = {
    "est_form": ("Estimate in the wrong form", "An estimate reads `(N min)` or `(N h)` right after the step, before any 📅 or ^id."),
    "due_old": ("Old deadline marker", "A deadline is the `📅 YYYY-MM-DD` field after the estimate, not `(due …)`."),
    "due_words": ("Deadline only in the words", "The words name a deadline but the line has no `📅` field, so Carmen and planning never see the date. Add it after the estimate; the time of day may stay in the words."),
    "date_twice": ("Date twice", "The step repeats its `📅` date in its words, so the card shows it twice. Take the date out of the words; a time of day such as \"by noon\" can stay."),
    "two_est": ("Two estimates", "This step carries two estimates; keep one `(N min)` or `(N h)` at the end."),
    "no_link": ("Points at a link it lacks", "The step points at a link, a mail or a form but carries none. Put the link on an indented line under it (the mail itself, or the form), SYSTEM.md § Capture note schema."),
}
OPEN_ONLY = ("due_words", "date_twice", "two_est", "no_link")
# Note-level, not one line's form (earlier_needed): asked on Train, proposed by the pre-run.
EARLIER = ("Depends on an earlier step?", "It takes the result of an earlier step but has no `(after: ^id)`, "
           "so planning can put it next to the step it waits on. Link it, or say it is free.")


# D-10
def form_problems(line: str, workspace_lines=(), today: dt.date | None = None, open_checks: bool = True) -> list[dict]:
    """What is wrong with one step line's form: [{code, title, why}], in FORM's order. Built on
    parse_line and this module's patterns only. workspace_lines: the indented lines under it (a
    link there counts). open_checks=False (a closed task, a ticked line) keeps the field-form
    checks only. Never raises."""
    try:
        today = today or _today()
        st = parse_line(line, today)
        if not st:
            return []
        out = []
        core = FIELDS_TAIL.sub("", line)
        if st.state == "open" and EST_LOOSE.search(core) and not EST_STRICT.search(core):
            out.append("est_form")
        if st.state != "struck" and DUE_OLD.search(line):
            out.append("due_old")
        if open_checks and st.state == "open" and st.kind in ("Next", "Then"):
            words_all = re.sub(r"^\s*- \[ \] (?:Next|Then):\s*", "", core)
            if "📅" not in line and prose_due(words_all, today):
                out.append("due_words")
            due = DUE_F.search(line)
            if due:
                d = dt.date.fromisoformat(due.group(1))
                # The step's own words only: brackets are details ("(owner decision, Nov 11)", a reason),
                # "from Nov 17" is a gate, "after: Jan 4" a condition. A date counts when it says
                # when the step is due ("by Fri Nov 13") or sits in the action before a colon.
                words = re.sub(r"\([^()]*\)", "", words_all)
                head = words.split(": ", 1)[0]
                for m in DATE_WORDS.finditer(words):
                    before = words[:m.start()]
                    if (MON[m.group(2).title()[:3]], int(m.group(3))) == (d.month, d.day) \
                            and not re.search(r"\b(?:from|after:?)\s+$", before, re.I) \
                            and (m.end() <= len(head) or DUE_WORD_BEFORE.search(before)):
                        out.append("date_twice")
                        break
            if len(re.findall(EST_RX, core)) > 1:
                out.append("two_est")
            if POINTS_AT.search(core) and not HAS_LINK.search(line) \
                    and not any(HAS_LINK.search(w) for w in workspace_lines or ()):
                out.append("no_link")
        return [{"code": c, "title": FORM[c][0], "why": FORM[c][1]} for c in FORM if c in out]
    except Exception:   # noqa: BLE001 - a check never breaks a write
        return []


def workspace_of(lines: list[str], i: int) -> list[str]:
    """The indented lines right under line i (a step's workspace)."""
    out = []
    for nxt in lines[i + 1:]:
        if not re.match(r"^\s{2,}- ", nxt):
            break
        out.append(nxt)
    return out


def note_problems(body: str, today: dt.date | None = None, ids=None, open_checks: bool = True) -> dict:
    """{line index: (line, problems)} for the step lines of a body's Next action block (only those
    whose id or words are in `ids` when given)."""
    lines = body.split("\n")
    r = block_range(lines)
    out = {}
    if not r:
        return out
    for i in range(r[0] + 1, r[1]):
        st = parse_line(lines[i], today)
        if not st or (ids is not None and st.id not in ids and _key(st.text) not in ids):
            continue
        pr = form_problems(lines[i], workspace_of(lines, i), today, open_checks)
        if pr:
            out[i] = (lines[i], pr)
    return out


def prose_gate(text: str, today: dt.date) -> str:
    """The first day a step can be done, named in its words ("from Nov 17"), as YYYY-MM-DD, or ""
    when there is none or it has passed."""
    m = GATE_PROSE.search(text or "")
    if not m or COND_BEFORE.search(text[:m.start()]):
        return ""
    iso = _year(MON[m.group(1).title()], int(m.group(2)), today)
    return iso if iso and iso > today.isoformat() else ""


def prose_due(text: str, today: dt.date) -> str:
    """The deadline a step names in its words, as YYYY-MM-DD, or ""."""
    m = DUE_PROSE.search(text)
    if not m or COND_BEFORE.search(text[:m.start()]):
        return ""
    iso = _year(MON[m.group(2).title()], int(m.group(3)), today)
    if iso and m.group(1).lower() == "before":
        iso = (dt.date.fromisoformat(iso) - dt.timedelta(days=1)).isoformat()
    # A date already past is stale words, or one he cleared on purpose (Nov 10: "by Nov 1" turned
    # into 📅 brought a task he had un-dated on Nov 1 back as overdue): never a new deadline.
    return iso if iso and iso >= today.isoformat() else ""


EST = re.compile(r"\s*\((\d+(?:\.\d+)?)\s*(min|h)\)\s*$")
# Tasks v2 (Nov 6): a step whose length is not known yet carries a timebox instead of an estimate,
# and an appointment step names its calendar event by key (taskids.event_key), hidden on the card.
TIMEBOX = re.compile(r"\s*\((?:timebox|discover,?)\s+(\d+)\s*min\)", re.I)   # "(discover, 30 min)": the Nov 5 draft
EVENT = re.compile(r"\s*\((ev-[0-9a-f]{8})\)")
# D-6
OPTIONAL = re.compile(r"\s+\(optional\)(?=\s|$)", re.I)   # never "`(optional)`" quoted in prose
# D-35
# How long a wait takes at most, on its Waiting line ("Lakeside lab, on the lab result (up to 7 d)").
SPAN = re.compile(r"\s*\(up to (\d+(?:\.\d+)?)\s*d\)")
AFTER = re.compile(r"\s*\(after:\s*(\^s[0-9a-f]{4,8}(?:\s*,\s*\^s[0-9a-f]{4,8})*)\s*\)")
STRUCK = re.compile(r"^(.*?)~~\s*(\(.*\))?\s*$")


@dataclass
class Step:
    state: str            # open | done | struck
    kind: str = ""        # Next | Then | Waiting | "" (a plain checkbox)
    text: str = ""        # the step in words, nothing else
    est: str = ""         # "10 min", "1.5 h", ""
    due: str = ""         # YYYY-MM-DD
    done: str = ""        # YYYY-MM-DD
    id: str = ""
    note: str = ""        # a struck line's "(discarded, Nov 4)"
    indent: str = ""
    box: int | None = None   # a timebox in minutes: not known yet, "up to 30 min" (Tasks v2)
    event: str = ""       # ev-xxxxxxxx: an appointment step, linked to its calendar event
    after: list = field(default_factory=list)   # ids it waits on (Nov 10): "(after: ^s2765)"
    optional: bool = False   # "(optional)" (Nov 11): a nice-to-have, filler for spare minutes
    gate: str = ""        # YYYY-MM-DD: not doable before (Nov 11, from the words only; never written)
    span: float | None = None   # a Waiting line's "(up to N d)": days at most (Nov 12, D-35)
    extra: dict = field(default_factory=dict)

    @property
    def minutes(self) -> int | None:
        m = re.match(r"(\d+(?:\.\d+)?) (min|h)$", self.est)
        return None if not m else int(float(m.group(1)) * (60 if m.group(2) == "h" else 1))

    @property
    def planned(self) -> int | None:
        """The minutes planning counts: the estimate, else the timebox."""
        return self.minutes if self.minutes is not None else self.box

    def display(self) -> str:
        """The step as the app and older readers know it: text, "(due Nov 12)", "(10 min)"."""
        out = self.text + (" (optional)" if self.optional else "")
        if self.due:
            d = dt.date.fromisoformat(self.due)
            out += f" (due {d.strftime('%b')} {d.day})"
        if self.est:
            out += f" ({self.est})"
        elif self.box:
            out += f" (timebox {self.box} min)"
        return out


def _year(mon: int, day: int, today: dt.date) -> str:
    try:
        d = dt.date(today.year, mon, day)
    except ValueError:
        return ""
    if d < today - dt.timedelta(days=60):   # "(due Jan 5)" written in November is next year
        d = d.replace(year=today.year + 1)
    return d.isoformat()


# D-10
def parse_line(line: str, today: dt.date | None = None) -> Step | None:
    """A step line in any form (old markers or fields), or None for any other line."""
    m = LINE.match(line)
    if not m:
        return None
    today = today or _today()
    ind, box, kind, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4)
    after = []
    a = AFTER.search(rest)
    if a:
        after = [x.strip().lstrip("^") for x in a.group(1).split(",") if x.strip()]
        rest = rest[:a.start()] + rest[a.end():]
    sid = ""
    e0 = EST.search(rest)   # an estimate an older writer appended after the fields
    est0 = f"{e0.group(1)} {e0.group(2)}" if e0 and ("📅" in rest[:e0.start()] or OUR_ID.search(rest[:e0.start()])) else ""
    if est0:
        rest = rest[:e0.start()]
    b = BLOCK_ID.search(rest) or OUR_ID.search(rest)
    if b:
        sid, rest = b.group(1), (rest[:b.start()] + rest[b.end():]).rstrip()
    note = ""
    if box == "~~":
        s = STRUCK.match(rest)
        if not s:
            return None
        rest, note = s.group(1), (s.group(2) or "")
        k = re.match(r"^(Next|Then|Waiting):\s*", rest)
        if k:
            kind, rest = k.group(1), rest[k.end():]
        if not sid:   # a writer that struck the whole line, ID and dates included
            b = BLOCK_ID.search(rest)
            if b:
                sid, rest = b.group(1), rest[:b.start()]
    due = done = ""
    for rx, key in ((DONE_F, "done"), (DUE_F, "due")):
        f = rx.search(rest)
        if f:
            rest = rest[:f.start()] + rest[f.end():]
            if key == "done":
                done = f.group(1)
            else:
                due = f.group(1)
    o = DUE_OLD.search(rest)
    if o and o.group(1) in MON:
        due = due or _year(MON[o.group(1)], int(o.group(2)), today)
        rest = rest[:o.start()] + rest[o.end():]
    tbox = None
    t = TIMEBOX.search(rest)
    if t:
        tbox, rest = int(t.group(1)), rest[:t.start()] + rest[t.end():]
    span = None
    sp = SPAN.search(rest)
    if sp and kind == "Waiting":
        span, rest = float(sp.group(1)), rest[:sp.start()] + rest[sp.end():]
    ev = EVENT.search(rest)
    event = ""
    if ev:
        event, rest = ev.group(1), rest[:ev.start()] + rest[ev.end():]
    rest = rest.rstrip()
    opt = False
    o2 = OPTIONAL.search(rest)
    if o2:
        opt, rest = True, (rest[:o2.start()] + rest[o2.end():]).rstrip()
    e = EST.search(rest)
    est = est0
    if e and not est:
        est = f"{e.group(1)} {e.group(2)}"
        rest = rest[:e.start()]
    while est and EST.search(rest):   # "(2 h) (2 h)": one estimate is the field, the repeat goes (Nov 11)
        rest = rest[:EST.search(rest).start()]
    state = {"[ ]": "open", "[x]": "done", "[X]": "done", "~~": "struck"}[box]
    if not due and state == "open" and kind in ("Next", "Then"):
        due = prose_due(rest, today)
    gate = prose_gate(rest, today) if state == "open" and kind in ("Next", "Then", "") else ""
    return Step(state=state, kind=kind, text=rest.strip(), est=est, due=due, done=done, id=sid,
                note=note.strip(), indent=ind, box=None if est else tbox, event=event, after=after,
                optional=opt, gate=gate, span=span)


def format_line(s: Step) -> str:
    kind = f"{s.kind}: " if s.kind else ""
    words = s.text + (" (after: " + ", ".join("^" + x for x in s.after) + ")" if s.after else "") \
        + (" (optional)" if s.optional else "") + (f" ({s.event})" if s.event else "") \
        + (f" (up to {s.span:g} d)" if s.span and s.kind == "Waiting" else "") + (
        f" ({s.est})" if s.est else f" (timebox {s.box} min)" if s.box else "")
    if s.state == "struck":
        out = f"{s.indent}- ~~{kind}{words}~~" + (f" {s.note}" if s.note else "")
    else:
        out = f"{s.indent}- {'[x]' if s.state == 'done' else '[ ]'} {kind}{words}"
    if s.due:
        out += f" 📅 {s.due}"
    if s.done and s.state == "done":
        out += f" ✅ {s.done}"
    if s.id:
        out += f" ^{s.id}"
    return out


def blocked_ids(steps: list[Step]) -> dict:
    """{step id: [the open targets it waits on]} for each open step whose `after:` names an open
    step or Waiting line (Nov 10). A target that is done, struck or gone no longer blocks."""
    open_ids = {x.id for x in steps if x.state == "open" and x.id}
    return {x.id: [t for t in x.after if t in open_ids] for x in steps
            if x.state == "open" and x.id and any(t in open_ids for t in x.after)}


# D-9
def task_waiting(steps: list[Step]) -> bool:
    """The one rule (Nov 10, Plan and Do alike): waiting only when a Waiting line is open and no open
    step of his is free. A step with no `after:` link is free (no link, not blocked)."""
    b = blocked_ids(steps)
    # A Waiting line after one of his open steps is pending (D-35 row) (the result of a step not done
    # yet): it does not make the task waiting.
    if not any(x.state == "open" and x.kind == "Waiting" and x.id not in b for x in steps):
        return False
    return not any(x.state == "open" and x.kind != "Waiting" and x.text and x.id not in b
                   and not (not x.id and x.after) for x in steps)


OUTREACH = re.compile(r"^(?:reply|respond|answer|send|thank|offer|chase|call|ask|email|e-mail|mail|message|text|ping|nudge|"
                      r"remind|tell|write|check|open|look up|look|search|see whether|see if)\b", re.I)
_MON3 = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def needs_wait(text: str, who: str = "", what: str = "", today: dt.date | None = None, outreach_ok: bool = False) -> bool:
    """Whether a step's words say it uses the answer it waits for (Nov 10; the rule the backfill, the
    writers and the prompts share). Linked: a step that says it comes after the answer ("once/when/
    after <who> sends or replies", "needs the reply") or one that uses it and names the person (pick,
    book, attend, add ... Lakeside). Never linked: his own reach-out or check toward that person (reply,
    send, thank, offer, chase, call, ask, email, message, check, open, look up), unless it says it
    comes after the answer; and never a step whose own condition is a date that has passed
    ("if nothing by Nov 6"). outreach_ok: the wait is a step that checks for the reply, so what he
    sends after it can depend on it (HR: "Send Lakeside the complete clearance file")."""
    t = (text or "").lower()
    if re.search(r"\bif (there is |there's |there isn't |you get )?no (reply|answer|response)\b|\b(nudge|chase|follow[- ]up|remind)\b", t):
        return False
    today = today or _today()
    for m in re.finditer(r"\bif\b[^.;]*?\b(?:by|until|before)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})\b", t):
        try:
            if dt.date(today.year, _MON3[m.group(1)], int(m.group(2))) < today:
                return False   # its own condition is a date that has passed
        except ValueError:
            pass
    # "once he picks a date", "when she decides", "after they agree" (r7) count like "once she sends".
    if re.search(r"\b(once|when|after|as soon as)\b[^.;]*\b(send|sends|sent|repl|confirm|answer|arriv|come back|get back|"
                 r"pick|picks|picked|choose|chooses|chose|decide|decides|decided|agree|agrees|agreed|book|books|booked)", t):
        return True
    if re.search(r"\b(needs?|wait for|with) (the |their |her |his )?(reply|answer|confirmation|result)\b", t):
        return True
    if OUTREACH.match(t) and not (outreach_ok and not re.match(r"^(?:check|open|look|search|see|call|ask|chase|nudge|remind)\b", t)):
        return False
    names = [w for w in re.split(r"[\s,()/]+", re.split(r"\s*(?: · for: |,|\()", who or "")[0]) if len(w) >= 4 and w[:1].isupper()]
    return any(re.search(rf"\b{re.escape(n.lower())}\b", t) for n in names)


# A step that takes the result of an earlier step (Nov 11, prescription: "Pick up at Walgreens" sat
# in today's chunk beside the visit that prescribes it, with no after:). Verbs that always take a
# result, and verbs that do only with a pronoun or a named result.
RESULT_STEP = re.compile(r"^(?:pick up|collect|receive|fetch)\b|^(?:wear|use|pack|bring)\s+(?:it|them|this|these)\b|"
                         r"^(?:send|upload|submit|forward|attach)\s+(?:the\s+)?(?:result|results|certificate|prescription|"
                         r"letter|record|records|receipt|confirmation)s?\b", re.I)


# "the e-bike online classroom, part 2 of 3": parts of one piece of work follow each other (Nov 11, M1:
# part 3 ranked before part 1 because it was shorter).
PART_OF = re.compile(r"^(.*?)\bpart\s+(\d+)\s+of\s+(\d+)\b", re.I)


def part_of(text: str) -> tuple[str, int, int] | None:
    """(the series' words before "part", N, M) for "<words> part N of M ...", else None."""
    m = PART_OF.match(clean(HIS_TAG.sub("", text or "")))
    return (_key(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def part_order(steps: list[Step]) -> dict:
    """{step id: the id of the open part right before it in its series} for open "part N of M"
    steps with an earlier open part (part N-1, else the nearest lower one)."""
    series = {}
    for x in steps:
        p = part_of(x.text) if x.state == "open" and x.id else None
        if p:
            series.setdefault((p[0], p[2]), []).append((p[1], x.id))
    out = {}
    for parts in series.values():
        parts.sort()
        for (n0, a), (n1, b) in zip(parts, parts[1:]):
            out[b] = a
    return out


# D-18
def earlier_needed(steps: list[Step]) -> dict:
    """{step id: the earlier open step it likely needs} for open steps whose words take an earlier
    step's result (RESULT_STEP) and that carry no `after:`: a question, never a link by itself
    (the contract asks it on Train, the pre-run proposes the link)."""
    out = {}
    prev = None
    parts = part_order(steps)
    for x in steps:
        if x.state != "open" or x.kind == "Waiting":
            continue
        if x.id and not x.after and x.id in parts:   # part N waits on part N-1
            out[x.id] = parts[x.id]
        elif prev is not None and x.id and not x.after and RESULT_STEP.match(clean(HIS_TAG.sub("", x.text))):
            out[x.id] = prev.id
        if x.id:
            prev = x
    return out


_REPLY_CHECK = re.compile(r"\bfor ([A-Z][\w-]+)(?:'s|’s)\s+(?:reply|answer|response)")


# D-9
def waits_on_someone(step: Step) -> str:
    """Who a step waits on, when it is a wait on someone else (Nov 11, M1: part 2 after his own
    part 1 showed "Waiting on the step before"): a `Waiting:` line (its who), or a step that checks
    for someone's reply ("Check the portal for Lakeside's reply": Lakeside). "" for his own steps: a step
    after one of those is simply "after" it, never a wait."""
    if step.kind == "Waiting":
        return re.split(r"\s*(?: · for: |,|\(| to reply)", step.text)[0].strip()
    m = _REPLY_CHECK.search(step.text or "")
    return m.group(1) if m else ""


def after_label(steps: list[Step], sid: str) -> str:
    """How a step that waits only on his own open steps reads: "after part 1" for a part, else
    "after <the step's first words>". "" when it waits on someone (that is a wait row) or on nothing."""
    by = {x.id: x for x in steps if x.id}
    me = by.get(sid)
    targets = [by[t] for t in (me.after if me else []) if t in by and by[t].state == "open"]
    if not targets or any(waits_on_someone(t) for t in targets):
        return ""
    t = targets[0]
    p, q = part_of(t.text), part_of(me.text)
    if p and q and (p[0], p[2]) == (q[0], q[2]):
        return f"after part {p[1]}"
    words = clean(HIS_TAG.sub("", t.text)).split()
    return "after " + " ".join(words[:5]) + ("…" if len(words) > 5 else "")


# D-18
def relink_waits(snap: dict, text: str, today: dt.date | None = None) -> tuple[str, list[dict]]:
    """A write that ticks a step and adds a Waiting line moves the steps that waited on the ticked
    step to the new wait (Nov 11, HR: a chat ticked "Check the portal for Lakeside's reply" and added
    "Waiting: Lakeside nurse, on the lab order"; the lab draw and the letter kept `(after:)` on the
    ticked step and showed as free, the draw "Due today"). snap: the note as last seen (load_snap,
    with its open "waits"); without one nothing moves. A line carrying "(the owner" is not rewritten:
    a "relink_proposed" event goes to the history and the pre-run proposes it. Returns the text
    and the history events."""
    if not snap or "waits" not in snap or not snap.get("steps"):
        return text, []
    head, sep, body = text.partition("\n---\n")
    lines = body.split("\n")
    r = block_range(lines)
    if not sep or not r:
        return text, []
    was_open = {x.get("id") for x in snap["steps"] if x.get("state") == "open" and x.get("id")}
    now = [(i, parse_line(lines[i], today)) for i in range(r[0] + 1, r[1])]
    now = [(i, s) for i, s in now if s]
    ticked = {s.id for _, s in now if s.state == "done" and s.id in was_open}
    new_waits = [s.id for _, s in now if s.kind == "Waiting" and s.state == "open" and s.id and s.id not in snap["waits"]]
    if not ticked or not new_waits:
        return text, []
    wid, events = new_waits[0], []
    for i, s in now:
        hit = [t for t in s.after if t in ticked]
        if s.state != "open" or not hit:
            continue
        if HIS_TAG.search(lines[i]):
            events.append({"step": s.id, "text": s.text, "event": "relink_proposed", "from": hit, "to": wid})
            continue
        s.after = list(dict.fromkeys([wid if t in ticked else t for t in s.after]))
        lines[i] = format_line(s)
        events.append({"step": s.id, "text": s.text, "event": "relinked", "from": hit, "to": wid})
    return head + sep + "\n".join(lines), events


# --- step commands: the one way a writer changes structure (Nov 11, item 15) -------------------------
# owner decision, Nov 11: structure (order, waits, dates, links) lived in free text that six writers edited by
# hand, so each had to keep it right and didn't (the HR wait not relinked, the prescription pick-up
# unlinked, M1's parts unchained, a checklist outside the block, a same-note link). These functions
# change one step and keep the rest right; `vt step` exposes them, the hook refuses a raw edit of the
# block, and steps.write stays the save underneath. Each returns the note's new text or raises Refused.

# D-11
class Refused(ValueError):
    """A step command refused, with the reason in words for the writer."""


def _block_lines(text: str):
    head, sep, body = text.partition("\n---\n")
    if not sep:
        raise Refused("no frontmatter")
    lines = body.split("\n")
    r = block_range(lines)
    if not r:
        raise Refused("no `## Next action` block")
    return head, sep, lines, r


def _find(lines: list[str], r, sid: str, states=("open",)) -> tuple[int, Step]:
    sid = (sid or "").lstrip("^")
    for i in range(r[0] + 1, r[1]):
        st = parse_line(lines[i])
        if st and st.id == sid and st.state in states:
            return i, st
    raise Refused(f"no {'/'.join(states)} step ^{sid} in this note's `## Next action`; `vt step list` shows them")


def _span(lines: list[str], i: int, end: int) -> int:
    """The line after step i and its workspace (indented lines right under it)."""
    j = i + 1
    while j < end and re.match(r"^\s{2,}\S", lines[j]):
        j += 1
    return j


def _new_problems(old_line: str, new_line: str, ws=(), today=None) -> list[str]:
    before = {x["code"] for x in form_problems(old_line, ws, today)} if old_line else set()
    return [x["why"] for x in form_problems(new_line, ws, today) if x["code"] not in before]


def _ids(text: str) -> set:
    return {x.id for x in steps_of(text.partition("\n---\n")[2]) if x.id}


def op_link(text: str, sid: str, after: list[str], today=None) -> str:
    """`(after: ^ids)` on step sid: each id an open step or Waiting line here, never itself, no loop."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid)
    by = {x.id: x for x in steps_of("\n".join(lines)) if x.id}
    ids = [x.lstrip("^") for x in after if x.strip()]
    for t in ids:
        if t == st.id:
            raise Refused("a step cannot wait on itself")
        if t not in by or by[t].state != "open":
            raise Refused(f"^{t} is not an open step or Waiting line here")
        seen, todo = set(), [t]   # no loop: t must not (transitively) wait on sid
        while todo:
            x = todo.pop()
            if x == st.id:
                raise Refused(f"^{t} already waits on ^{st.id}: that would be a loop")
            if x in seen or x not in by:
                continue
            seen.add(x)
            todo += by[x].after
    st.after = list(dict.fromkeys(ids))
    lines[i] = format_line(st)
    return head + sep + "\n".join(lines)


def op_date(text: str, sid: str, due: str, today=None) -> str:
    """The step's 📅 set (YYYY-MM-DD) or cleared ("none"); refused when the words then repeat it."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid)
    old = lines[i]
    if due.lower() == "none":
        named = prose_due(st.text, today or _today())
        if named:
            raise Refused(f"the words still name a deadline ({named}); reword the step first")
        st.due = ""
    else:
        try:
            dt.date.fromisoformat(due)
        except ValueError:
            raise Refused(f"{due!r} is not YYYY-MM-DD or none")
        st.due = due
    lines[i] = format_line(st)
    bad = _new_problems(old, lines[i], workspace_of(lines, i), today)
    if bad:
        raise Refused(bad[0])
    return head + sep + "\n".join(lines)


def op_reword(text: str, sid: str, words: str, his: str = "", today=None) -> str:
    """New words for step sid, its id and fields kept; `his` ("Nov 11") adds his attribution, for a
    line he dictated. Refused when the new line breaks a form rule the old one did not."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid)
    words = " ".join((words or "").split())
    if not words:
        raise Refused("give the step's new words")
    if LINE.match(words) or "📅" in words or re.search(r"\^s[0-9a-f]{4}", words) or EST_IN.search(words):
        raise Refused("give the words only; date, estimate and links have their own commands")
    old = lines[i]
    st.text = HIS_TAG.sub("", words).strip() + (f" (the owner, {his})" if his else "")
    st.due = st.due or ""
    lines[i] = format_line(st)
    bad = _new_problems(old, lines[i], workspace_of(lines, i), today)
    if bad:
        raise Refused(bad[0])
    return head + sep + "\n".join(lines)


def op_est(text: str, sid: str, est: str) -> str:
    """Step sid's estimate set: "N min" or "N h" (Nov 11: the hook refuses hand edits, and a chat had
    no way to set one). It is the writer's: the estimator never overwrites it."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid)
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(min|m|minutes?|h|hours?|hrs?)\s*", est or "", re.I)
    if not m:
        raise Refused(f"{est!r}: give the estimate as N min or N h")
    n, unit = m.group(1).replace(",", "."), "h" if m.group(2).lower().startswith("h") else "min"
    st.est, st.box = f"{n.rstrip('0').rstrip('.') if '.' in n else n} {unit}", None
    lines[i] = format_line(st)
    return head + sep + "\n".join(lines)


def op_ws(text: str, sid: str, link: str, label: str, note: str) -> str:
    """A workspace line under step sid: a URL as [label](url), a wikilink as [[Note#Section|label]];
    a same-note [[#Section]] gets this note's name. At most 3."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid)
    link = (link or "").strip()
    w = wikilink(link if link.startswith("[[") else f"[[{link}]]", note) if not re.match(r"https?://", link) else None
    if re.match(r"https?://", link):
        item = f"[{label or link}]({link})"
    elif w:
        name, sec, lab = w
        item = f"[[{name}" + (f"#{sec}" if sec else "") + f"|{label or lab}]]"
    else:
        raise Refused("a link is a URL or a [[Note#Section]] wikilink")
    end = _span(lines, i, r[1])
    if end - i - 1 >= 3:
        raise Refused("a step carries at most 3 links (SYSTEM.md § Capture note schema)")
    ind = re.match(r"^(\s*)", lines[i]).group(1)
    lines.insert(end, f"{ind}    - {item}")
    return head + sep + "\n".join(lines)


def op_move(text: str, sid: str, target: str, where: str = "after") -> str:
    """Step sid (with its workspace) placed before or after step target; Next stays first."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, sid, ("open", "done", "struck"))
    j0, tg = _find(lines, r, target, ("open", "done", "struck"))
    if i == j0:
        raise Refused("a step cannot move next to itself")
    chunk = lines[i:_span(lines, i, r[1])]
    del lines[i:i + len(chunk)]
    r2 = block_range(lines)
    j, _ = _find(lines, r2, target, ("open", "done", "struck"))
    at = j if where == "before" else _span(lines, j, r2[1])
    lines[at:at] = chunk
    return head + sep + "\n".join(lines)


# D-35
def op_result(text: str, words: str, after_sid: str, span: float, moves: list[str], today=None) -> str:
    """A result someone else produces over time after one of his steps (the lab result after the
    draw): "- [ ] Waiting: <words> (after: ^after_sid) (up to N d)" under that step, pending until
    it is ticked, and the steps in `moves` (those that need the result) now wait on it instead."""
    head, sep, lines, r = _block_lines(text)
    after_sid = after_sid.lstrip("^")
    i, st = _find(lines, r, after_sid, states=("open", "done"))
    words = " ".join((words or "").split())
    if not words or span <= 0:
        raise Refused("result needs the words (who, on what) and --span N days")
    wid = _new_id(_ids(text))
    ind = re.match(r"^(\s*)", lines[i]).group(1)
    end = _span(lines, i, r[1])
    lines.insert(end, f"{ind}- [ ] Waiting: {words} (after: ^{after_sid}) (up to {span:g} d) ^{wid}")
    want = {m.lstrip("^") for m in moves}
    for k in range(r[0] + 1, min(r[1] + 2, len(lines))):
        x = parse_line(lines[k]) if k < len(lines) else None
        if x and x.id in want and x.state == "open":
            x.after = list(dict.fromkeys(wid if t == after_sid else t for t in x.after)) if after_sid in x.after else x.after + [wid]
            lines[k] = format_line(x)
    return head + sep + "\n".join(lines)


# D-38
CHASE = re.compile(r"^Chase\s+(.+?)\s+about\s+", re.I)


def chase_words(wait: Step) -> str:
    """The chase step for a wait, in words: "Chase Lakeside nurse about the lab order"."""
    who = waits_on_someone(wait) or "them"
    what = re.split(r"\s*\(", re.sub(r"^.*?\bon\s+", "", wait.text, count=1))[0].strip()
    what = re.sub(r"\s+after\s+the owner'?s?\b.*$", "", what).strip() or "it"
    return f"Chase {who} about {what}"


def chase_of(steps: list[Step], wait: Step, states=("open",)) -> Step | None:
    """The chase step of a wait (D-38): a step of his, in the given states, that reads "Chase
    <who> about ...", the wait's who. Found by its words, so no state file can drift from the note."""
    who = (waits_on_someone(wait) or "").lower()
    for x in steps:
        m = CHASE.match(x.text or "")
        if x.state in states and x.kind != "Waiting" and m and who and m.group(1).lower() == who:
            return x
    return None


def op_chase(text: str, wait_id: str, today=None) -> tuple[str, str]:
    """A chase of an open wait as an ordinary step (Nov 12, the owner: "These should still be
    substeps not a new card"): "- [ ] Then: Chase <who> about <what> (15 min) 📅 <today>" right
    above the Waiting line. No-op ("", "") when one is already open. Returns (text, new id)."""
    today = today or _today()
    head, sep, lines, r = _block_lines(text)
    i, w = _find(lines, r, wait_id)
    if w.kind != "Waiting":
        raise Refused(f"^{wait_id} is not a Waiting line")
    allsteps = [parse_line(l, today) for l in lines[r[0] + 1:r[1] + 1]]
    if chase_of([x for x in allsteps if x], w):
        return text, ""
    sid = _new_id(_ids(text))
    ind = re.match(r"^(\s*)", lines[i]).group(1)
    lines.insert(i, f"{ind}- [ ] Then: {chase_words(w)} (15 min) 📅 {today.isoformat()} ^{sid}")
    return head + sep + "\n".join(lines), sid


def op_chase_over(text: str, today=None) -> tuple[str, list[str]]:
    """Strike every open chase step whose wait is no longer open (D-38): the answer came, or the
    wait was struck. Returns (text, the struck ids)."""
    today = today or _today()
    head, sep, lines, r = _block_lines(text)
    st = [(k, parse_line(lines[k], today)) for k in range(r[0] + 1, min(r[1] + 1, len(lines)))]
    st = [(k, x) for k, x in st if x]
    waits = [x for _, x in st if x.kind == "Waiting"]
    gone = []
    for k, x in st:
        m = CHASE.match(x.text or "") if x.state == "open" and x.kind != "Waiting" else None
        if not m:
            continue
        mine = [w for w in waits if (waits_on_someone(w) or "").lower() == m.group(1).lower()]
        if mine and not any(w.state == "open" for w in mine):
            x.state, x.note = "struck", f"(wait over, {today:%b} {today.day})"
            lines[k] = format_line(x)
            gone.append(x.id)
    return head + sep + "\n".join(lines), gone


def op_wait(text: str, who: str, for_sid: str, label: str, today=None) -> str:
    """He is now waiting on `who` for what step for_sid set off: the step ticked, a Waiting line under
    it, and every open step that waited on the step moved to the wait (item 14's rule)."""
    head, sep, lines, r = _block_lines(text)
    i, st = _find(lines, r, for_sid)
    who = " ".join((who or "").split())
    if not who:
        raise Refused("name whom you wait on")
    taken = _ids(text)
    wid = _new_id(taken)
    st.state, st.done = "done", (today or _today()).isoformat()
    kind = "Next" if st.kind == "Next" else st.kind
    lines[i] = format_line(st)
    ind = re.match(r"^(\s*)", lines[i]).group(1)
    end = _span(lines, i, r[1])
    lines.insert(end, f"{ind}- [ ] Waiting: {who} · for: {st.text} ^{wid}")
    for k in range(r[0] + 1, r[1] + 1):
        x = parse_line(lines[k]) if k < len(lines) else None
        if x and x.state == "open" and st.id in x.after:
            x.after = list(dict.fromkeys(wid if t == st.id else t for t in x.after))
            lines[k] = format_line(x)
    out = head + sep + "\n".join(lines)
    if kind == "Next":   # the next free step of his comes up as Next
        out = _promote_next(out)
    return out


def _promote_next(text: str) -> str:
    """With no open Next, the first open Then (not optional, not blocked) becomes Next."""
    head, sep, lines, r = _block_lines(text)
    steps = [(i, parse_line(lines[i])) for i in range(r[0] + 1, r[1])]
    steps = [(i, s) for i, s in steps if s]
    if any(s.state == "open" and s.kind == "Next" for _, s in steps):
        return text
    blk = blocked_ids([s for _, s in steps])
    for i, s in steps:
        if s.state == "open" and s.kind == "Then" and not s.optional and s.id not in blk:
            s.kind = "Next"
            lines[i] = format_line(s)
            break
    return head + sep + "\n".join(lines)


def wait_holds(steps: list[Step], wid: str) -> bool:
    """Whether the Waiting line `wid` still holds a step back: the task waits (task_waiting), or an
    open step names it in `after:`. Such a wait is ended by evidence and his Submit, never struck
    by a writer that read a message (the pre-run's guard, Nov 11 audit A1)."""
    return task_waiting(steps) or any(x.state == "open" and wid in x.after for x in steps if wid)


def waiting_cache(text: str, today: dt.date | None = None) -> str:
    """`waiting:` in the frontmatter as a cache of task_waiting, recomputed on every write (Nov 10).
    A note without a Next action block keeps its frontmatter as it is."""
    head, sep, body = text.partition("\n---\n")
    if not sep or not block_range(body.split("\n")):
        return text
    want = task_waiting(steps_of(body, today))
    has = re.search(r"^waiting:\s*true\s*$", head, re.M) is not None
    if want == has:
        return text
    lines = [l for l in head.split("\n") if not l.startswith("waiting:")]
    if want:
        lines.append("waiting: true")
    return "\n".join(lines) + sep + body


def clean(text: str) -> str:
    """A step's words without estimate, dates or ID, from a line or a step's text."""
    s = parse_line(f"- [ ] {text}") if not LINE.match(text or "") else parse_line(text)
    return s.text if s else (text or "").strip()


def block_range(lines: list[str]) -> tuple[int, int] | None:
    for i, l in enumerate(lines):
        if l.strip() == "## Next action":
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return i, end
    return None


def steps_of(body: str, today: dt.date | None = None) -> list[Step]:
    """Every step line of the Next action block, in order."""
    lines = body.split("\n")
    r = block_range(lines)
    if not r:
        return []
    return [s for s in (parse_line(lines[i], today) for i in range(r[0] + 1, r[1])) if s]


def _new_id(taken: set[str]) -> str:
    while True:
        i = "s" + secrets.token_hex(2)
        if i not in taken:
            return i


def _open_keys(steps: list) -> set:
    """IDs and words of the steps open in a version (Step objects or _snap dicts)."""
    out = set()
    for s in steps or []:
        st, sid, t = (s.state, s.id, s.text) if isinstance(s, Step) else (s.get("state"), s.get("id"), s.get("text"))
        if st == "open":
            out |= {x for x in (sid, _key(t)) if x}
    return out


# D-14 D-6
def normalize(text: str, today: dt.date | None = None, opened: set | None = None) -> str:
    """The note with every step line of its Next action block in the one form: fields for the
    dates, an ID on each line. A tick gets today's ✅ only when the step was open before
    (`opened`: IDs and words, from _open_keys), never an old tick. Lines outside the block, the
    DoD and the Log are never touched. Idempotent."""
    today = today or _today()
    head, sep, body = text.partition("\n---\n")
    if not sep:
        return text
    lines = body.split("\n")
    r = block_range(lines)
    if not r:
        return text
    parsed = {i: parse_line(lines[i], today) for i in range(r[0] + 1, r[1])}
    taken = {s.id for s in parsed.values() if s and s.id}
    seen = set()
    # A closed task closes its open steps: struck, not ticked (owner decision, Nov 10: "closing a task
    # should simply mark substeps with due dates as closed but not done"). Otherwise a dropped
    # task keeps open dated steps that bring it back. Every closer writes through here.
    closed = bool(re.search(r"(?m)^status:\s*(done|cancelled)\s*$", head))
    for i, s in parsed.items():
        if not s:
            continue
        if closed and s.state == "open" and s.kind in ("Next", "Then", "Waiting"):
            s.state, s.note = "struck", (f"(not needed, {today:%b} {today.day})" if s.optional
                                         else f"(closed with the task, {today:%b} {today.day})")
        elif not closed and s.state == "struck" and (s.note.startswith("(closed with the task")
                                                     or (s.optional and s.note.startswith("(not needed"))):
            s.state, s.note = "open", ""   # reopened: what the close struck comes back, his discards stay
        if not s.id or s.id in seen:   # a copied line keeps its text, never its twin's identity
            s.id = _new_id(taken | seen)
            taken.add(s.id)
        seen.add(s.id)
        if s.state == "done" and not s.done and opened and (s.id in opened or _key(s.text) in opened):
            s.done = today.isoformat()
        new = format_line(s)
        if new != lines[i]:
            lines[i] = new
    return head + sep + "\n".join(lines)


# --- history ------------------------------------------------------------------------------------

def _key(t: str) -> str:
    t = HIS_TAG.sub("", t or "")
    return re.sub(r"\W+", " ", t.lower()).strip()


def _snap(steps: list[Step]) -> list[dict]:
    return [{"id": s.id, "state": s.state, "kind": s.kind, "text": s.text, "est": s.est, "due": s.due,
             "done": s.done, **({"box": s.box} if s.box else {}), **({"optional": True} if s.optional else {})}
            for s in steps if s.kind != "Waiting"]


def diff(before: list[dict], after: list[dict]) -> list[dict]:
    """What changed between two snapshots of a note's steps (lists of _snap dicts): steps are
    paired by ID, else by the same words, else by close words in the same place (an edit)."""
    out = []
    b_left = list(before)
    pairs = []
    for a in after:
        hit = next((b for b in b_left if a["id"] and b["id"] == a["id"]), None) \
            or next((b for b in b_left if _key(b["text"]) == _key(a["text"])), None)
        if hit:
            b_left.remove(hit)
        pairs.append((hit, a))
    for n, (b, a) in enumerate(pairs):   # lines changed beyond recognition at the same place
        if b is None:
            close = max(b_left, key=lambda x: difflib.SequenceMatcher(None, _key(x["text"]), _key(a["text"])).ratio(),
                        default=None)
            if close and difflib.SequenceMatcher(None, _key(close["text"]), _key(a["text"])).ratio() >= 0.6:
                b_left.remove(close)
                pairs[n] = (close, a)
    for pos, (b, a) in enumerate(pairs):
        base = {"step": a["id"], "text": a["text"], "pos": pos}
        if b is None:
            out.append({**base, "event": "added", "kind": a["kind"], "due": a["due"], "est": a["est"],
                        **({"state": a["state"]} if a["state"] != "open" else {})})
            continue
        if _key(b["text"]) != _key(a["text"]):
            out.append({**base, "event": "reworded", "old": b["text"], "new": a["text"]})
        if b["state"] != a["state"]:
            out.append({**base, "event": {"done": "done", "struck": "discarded", "open": "reopened"}[a["state"]],
                        "was": b["state"]})
        for k in ("due", "est", "box", "optional"):
            if b.get(k) != a.get(k):
                out.append({**base, "event": f"{k}_changed", "old": b.get(k) or "", "new": a.get(k) or ""})
        if b["kind"] != a["kind"] and {b["kind"], a["kind"]} != {"Next", "Then"}:
            out.append({**base, "event": "kind_changed", "old": b["kind"], "new": a["kind"]})
    for b in b_left:   # gone without a trace: deleted from the note, not struck
        out.append({"step": b["id"], "text": b["text"], "event": "deleted", "was": b["state"]})
    return out


@contextlib.contextmanager
def _locked():
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / "lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def _snap_path(key: str) -> Path:
    return SNAPS / (re.sub(r"[^\w.-]+", "_", key)[:120] + ".json")


def load_snap(key: str) -> dict | None:
    try:
        d = json.loads(_snap_path(key).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _task_key(text: str, stem: str) -> str:
    m = re.search(r"^task_id:\s*(t-[0-9a-f]{6})\s*$", text.partition("\n---\n")[0], re.M)
    return m.group(1) if m else stem


def record(stem: str, before: str | None, after: str, source: str, today: dt.date | None = None,
           why: str = "", extra: list[dict] | None = None) -> list[dict]:
    """Log what a write changed in the note's steps (before None: compare with the snapshot) and
    keep the new snapshot. source: who wrote it ("tap:step_done", "chat", "pre-run", "prep",
    "outside", "migration"). Never raises: the history is a layer on top."""
    try:
        today = today or _today()
        key = _task_key(after, stem)
        _all_after = steps_of(after.partition("\n---\n")[2], today)
        new = _snap(_all_after)
        waits = sorted(x.id for x in _all_after if x.kind == "Waiting" and x.state == "open" and x.id)
        with _locked():
            old = (_snap(steps_of(before.partition("\n---\n")[2], today)) if before is not None
                   else (load_snap(key) or {}).get("steps"))
            changes = ([] if old is None else diff(old, new)) + list(extra or [])
            # A wait's life (D-39 row), so response times can be learned (Nov 12: the history held no
            # wait event at all). Opened and ended, with who and how (done, struck, gone).
            # D-39
            if old is not None:
                if before is not None:
                    w0 = {x.id: x for x in steps_of(before.partition("\n---\n")[2], today)
                          if x.kind == "Waiting" and x.state == "open" and x.id}
                else:
                    w0 = {i: None for i in (load_snap(key) or {}).get("waits") or []}
                w1 = {x.id: x for x in _all_after if x.kind == "Waiting" and x.state == "open" and x.id}
                now_by = {x.id: x for x in _all_after if x.id}
                for i in sorted(set(w1) - set(w0)):
                    changes.append({"step": i, "text": w1[i].text, "event": "wait_opened",
                                    **({"span": w1[i].span} if w1[i].span else {})})
                for i in sorted(set(w0) - set(w1)):
                    x = now_by.get(i)
                    changes.append({"step": i, "text": (x or w0[i]).text if (x or w0[i]) else "",
                                    "event": "wait_ended", "how": x.state if x else "gone"})
            at = dt.datetime.now(TZ).isoformat(timespec="seconds")
            if changes:
                with open(HISTORY, "a", encoding="utf-8") as f:
                    for c in changes:
                        f.write(json.dumps({"at": at, "task_id": key if key.startswith("t-") else "", "task": stem,
                                            "source": source, **({"why": why} if why else {}), **c},
                                           ensure_ascii=False) + "\n")
            if old != new or before is None or (load_snap(key) or {}).get("waits") != waits:
                SNAPS.mkdir(parents=True, exist_ok=True)
                tmp = _snap_path(key).with_suffix(".tmp")
                # waits: the open Waiting lines (Nov 11), so the next write tells a new wait from an old one
                tmp.write_text(json.dumps({"task": stem, "at": at, "steps": new, "waits": waits}, ensure_ascii=False),
                               encoding="utf-8")
                os.replace(tmp, _snap_path(key))
        return changes
    except Exception:   # noqa: BLE001
        return []


def observe(p: Path, source: str = "outside") -> list[dict]:
    """A note read after someone else changed it: log its step changes against the snapshot."""
    try:
        return record(p.stem, None, p.read_text(encoding="utf-8", errors="replace"), source)
    except OSError:
        return []


class NoteChanged(RuntimeError):
    """The note changed between the writer's read and its write, and no redo could follow it."""


def _read_now(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def replace_if(p: Path, base: str, new: str) -> bool:
    """Write new only while the note still reads base (compare and swap, atomic replace)."""
    if _read_now(p) != base:
        return False
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, p)
    return True


# D-30
def write(p: Path, text: str, source: str, before: str | None = None, why: str = "",
          today: dt.date | None = None, redo=None) -> str:
    """The one way to save a task note with steps: normalized, its step changes logged, written
    atomically. Returns the text as written (for an undo hash).

    Compare and swap (Nov 12, the owner: "Yes, all of it"): `before` is the text the change was
    computed from (read here when not given). The note is written only while it still reads
    `before`; if it changed underneath (his Obsidian edit, another writer), the change is computed
    again from the note as it now is with redo(current) and tried once more, else NoteChanged is
    raised and nothing is written. A writer without redo is refused rather than overwriting."""
    today = today or _today()
    if before is None:
        before = _read_now(p)
    for attempt in range(3):
        out = normalize(text, today, _open_keys(steps_of(before.partition("\n---\n")[2], today)) if before else None)
        # The steps as last seen, before anyone else's edit: Tasks' check compares with them, so a
        # chat that edits by hand and then runs `vt step` counts as one change (Tasks v2).
        snapd = (load_snap(_task_key(before, p.stem)) or {}) if before else {}
        snap0 = snapd.get("steps") if before else None
        out, relinked = relink_waits(snapd, out, today)
        out = waiting_cache(out, today)
        if before:   # whatever changed since the last snapshot came from someone else
            record(p.stem, None, before, "outside", today)
        prev = snap0 if snap0 is not None else (_snap(steps_of(before.partition("\n---\n")[2], today)) if before else None)
        out, extra = on_write(prev, out, source, today, p.stem)
        extra = extra + form_events(prev, out, today) + relinked
        cur = _read_now(p)
        if cur == before:
            break
        if redo is None or attempt == 2:
            raise NoteChanged(f"{p.name} changed while {source} was writing it; nothing written")
        text, before = redo(cur), cur
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, p)
    record(p.stem, before or None, out, source, today, why, extra + _blocks_on_status(before, out))
    return out


_CLOSED = re.compile(r"(?m)^status:\s*(done|cancelled)\s*$")


# D-21
def _blocks_on_status(before: str | None, out: str) -> list[dict]:
    """A task that closed in this write loses its future calendar blocks; one that reopened gets them
    back (D-21, Nov 11). Carmen's tasks/blocks does it; without the app, nothing. Never raises."""
    if not before:
        return []
    was, now = bool(_CLOSED.search(before.partition("\n---\n")[0])), bool(_CLOSED.search(out.partition("\n---\n")[0]))
    if was == now or not is_task(out):
        return []
    m = re.search(r"^task_id:\s*(t-[0-9a-f]{6})\s*$", out.partition("\n---\n")[0], re.M)
    try:
        from jobs.checkin_app.tasks import blocks
        ev = blocks.on_close(m.group(1) if m else "") if now else blocks.on_reopen(m.group(1) if m else "")
    except Exception:   # noqa: BLE001 - the calendar is a layer on top of the save
        return []
    return [ev] if ev else []


def form_events(prev: list[dict] | None, text: str, today: dt.date | None = None) -> list[dict]:
    """A "form" history event for each step line this write added or changed (never an old line)
    that breaks a form rule (Nov 11). Recorded, never blocking: Train shows them through the
    contract. prev: the steps as last seen (_snap dicts), None for a new note."""
    try:
        body = text.partition("\n---\n")[2]
        old = {(x.get("id"), _key(x.get("text", "")), x.get("due") or "", x.get("est") or "") for x in prev or []}
        changed = {st.id for st in steps_of(body, today)
                   if st.state == "open" and st.id and (st.id, _key(st.text), st.due, st.est) not in old}
        if not changed:
            return []
        out = []
        for _, (line, pr) in note_problems(body, today, ids=changed).items():
            st = parse_line(line, today)
            out.append({"step": st.id, "text": st.text, "event": "form", "codes": [x["code"] for x in pr]})
        return out
    except Exception:   # noqa: BLE001
        return []


def on_write(prev: list[dict] | None, text: str, source: str, today: dt.date | None = None,
             stem: str = "") -> tuple[str, list[dict]]:
    """Tasks' check on every write (Tasks v2, Nov 6): the step contract and the estimator, in the
    Tasks module (jobs/checkin_app/tasks/contract.py). Text unchanged and no events when the
    app's code is not importable or its flags are off."""
    try:
        from jobs.checkin_app.tasks import contract
    except Exception:   # noqa: BLE001 - tools/steps works without the app
        return text, []
    return contract.on_write(prev, text, source, today, stem)


def append(key: str, stem: str, source: str, event: dict) -> None:
    """One step event no note write carries (Tasks v2: a step started, his answer to the actual
    check), into the same history. Never raises."""
    try:
        with _locked():
            with open(HISTORY, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": dt.datetime.now(TZ).isoformat(timespec="seconds"),
                                    "task_id": key if key.startswith("t-") else "", "task": stem,
                                    "source": source, **event}, ensure_ascii=False) + "\n")
    except Exception:   # noqa: BLE001
        pass


# --- found_by: steps a timeboxed finding-out step found (Tasks v2) -------------------------------
FOUND = DIR / "found.json"   # {task key: {step id: the timeboxed step that found it}}


def found_of(key: str) -> dict:
    try:
        d = json.loads(FOUND.read_text(encoding="utf-8"))
        v = d.get(key) if isinstance(d, dict) else None
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def mark_found(key: str, found: dict) -> None:
    """Remember which steps a finding-out step found. Kept for open tasks; never raises."""
    try:
        with _locked():
            try:
                d = json.loads(FOUND.read_text(encoding="utf-8"))
                d = d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                d = {}
            d.setdefault(key, {}).update(found)
            tmp = FOUND.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, FOUND)
    except Exception:   # noqa: BLE001
        pass


def settle(p: Path, source: str, prev: list[dict] | None, today: dt.date | None = None) -> list[dict]:
    """A note someone else just edited (a chat turn): normalized, and Tasks' check run against the
    steps as last seen before that edit (`prev`, the snapshot), so the estimator sees what the edit
    changed. Written only when something changed. Returns the extra events."""
    today = today or _today()
    text = p.read_text(encoding="utf-8")
    out = normalize(text, today, _open_keys(prev))
    out, extra = on_write(prev, out, source, today, p.stem)
    extra = extra + form_events(prev, out, today)   # a chat turn's own lines (Nov 11)
    if out != text:
        if p.read_text(encoding="utf-8") != text:   # compare and swap: written meanwhile, leave it
            return []
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, p)
        record(p.stem, text, out, source, today, extra=extra)
    elif extra:
        record(p.stem, text, out, source, today, extra=extra)
    return extra


# --- closing and reopening a task: one form for every closer (Nov 11, audit A5) ------------------
# Five closers (Carmen's taps and steps, auto-close, the card-note clean-up, Telegram) each wrote
# their own; Telegram's left no completedDate and put its section after ## Log, so Carmen's Done
# fold never showed those tasks and Log readers took the section for the Log.

def set_fm(head: str, key: str, val: str | None) -> str:
    """The frontmatter (without its --- lines) with `key` set in place, appended when missing,
    removed when val is None."""
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if val is None:
        return "\n".join(l for l in head.split("\n") if not re.match(rf"{re.escape(key)}:", l))
    return pat.sub(lambda _: f"{key}: {val}", head, count=1) if pat.search(head) else f"{head}\n{key}: {val}"


# D-14
def close_head(head: str, today: dt.date, status: str = "done") -> str:
    """Closed: `status` done or cancelled, `completedDate` today, `waiting` off. Its open steps are
    struck by normalize on the write; a step the closer ticked gets its ✅ there too."""
    return set_fm(set_fm(set_fm(head, "status", status), "completedDate", today.isoformat()), "waiting", None)


def reopen_head(head: str, status: str = "open") -> str:
    """Open again: `status`, `completedDate` cleared (normalize brings back what the close struck)."""
    return set_fm(set_fm(head, "status", status), "completedDate", None)


def close(text: str, today: dt.date, status: str = "done") -> str:
    head, sep, body = text.partition("\n---\n")
    return close_head(head, today, status) + sep + body if sep else text


def reopen(text: str, status: str = "open") -> str:
    head, sep, body = text.partition("\n---\n")
    return reopen_head(head, status) + sep + body if sep else text


def add_section(text: str, section: str) -> str:
    """A `## ...` section inserted right above `## Log`, so the Log stays the last section; at the
    end when the note has no Log."""
    section = section.strip()
    m = re.search(r"(?m)^## Log\s*$", text)
    if not m:
        return text.rstrip() + "\n\n" + section + "\n"
    return text[:m.start()].rstrip() + "\n\n" + section + "\n\n" + text[m.start():]


def is_task(text: str) -> bool:
    """A task note: `type: action` in its frontmatter (Nov 6: learnings and ideas carry
    status: inbox too, so status alone took 65 of them for tasks)."""
    return bool(re.search(r"^type:\s*action\s*$", text.partition("\n---\n")[0], re.M))


def is_template(p) -> bool:
    """A template ("_Inbox Template"), not a task: no writer gives it ids (Nov 6: its status: inbox
    let normalize stamp "^s14e0 ^s9434" into it, ids every task made from it would inherit)."""
    return Path(p).name.startswith("_")


def sweep(paths, today: dt.date | None = None, dry: bool = False) -> dict:
    """Bring open task notes into the one form (the pre-run, daily): lines written by a chat or
    by hand get their fields and IDs, and their changes are logged as "outside" first."""
    today = today or _today()
    n = changed = 0
    for p in paths:
        if is_template(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if not is_task(text) or not re.search(r"^status:\s*(inbox|open|in-progress)\s*$", text.partition("\n---\n")[0], re.M):
            continue
        n += 1
        prev = (load_snap(_task_key(text, p.stem)) or {}).get("steps")
        observe(p, "outside")
        out = normalize(text, today, _open_keys(prev))
        out, extra = on_write(prev, out, "pre-run", today, p.stem)
        if out != text:
            changed += 1
            if not dry:
                if p.read_text(encoding="utf-8") != text:   # written meanwhile: leave it for the next sweep
                    continue
                tmp = p.with_suffix(".md.tmp")
                tmp.write_text(out, encoding="utf-8")
                os.replace(tmp, p)
                record(p.stem, text, out, "normalize", today, extra=extra)
        elif extra and not dry:
            record(p.stem, text, out, "pre-run", today, extra=extra)
    return {"open": n, "normalized": changed}
