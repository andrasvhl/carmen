"""Step lines of a task note as data (owner decision, Nov 4: "substeps, due dates for substeps, and history
of substeps should be somehow stable ... the purely text-based approach seems to be very
unreliable").

A step is one line in `## Next action`, one action per line, its facts as fields at the end:

    - [ ] Next: Confirm the $3,250 refund landed (10 min) 📅 2026-11-12 ^s4f2a
    - [ ] Then: Reply to the WeTravel refund mail if not 📅 2026-11-12 ^s9c11
    - [x] Then: Zelle Rahim the remaining $475 ✅ 2026-11-04 ^s77b0
    - ~~Then: Ask Parrot for an invoice~~ (discarded, Nov 4) ^s1d0e

  - `(10 min)` / `(1.5 h)`: the estimate, as before.
  - `📅 YYYY-MM-DD`: the step's own deadline (the Tasks-plugin convention; was "(due Nov 12)").
  - `✅ YYYY-MM-DD`: the day it was done.
  - `^sxxxx`: an Obsidian block ID, hidden in reading view: the step's identity, so a step keeps
    its history when its text, date or place changes.

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
# STEPS_DIR points it elsewhere: every test suite sets it, so no test writes the live history.
DIR = Path(os.environ["STEPS_DIR"]) if os.environ.get("STEPS_DIR") else config.STATE_DIR / "steps"
HISTORY = DIR / "history.jsonl"
SNAPS = DIR / "snap"   # one file per task: its steps as last seen

MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}
LINE = re.compile(r"^(\s*)- (\[ \]|\[[xX]\]|~~)\s?(?:(Next|Then|Waiting):\s*)?(.*)$")
BLOCK_ID = re.compile(r"\s+\^([A-Za-z0-9-]+)\s*$")
OUR_ID = re.compile(r"\s+\^(s[0-9a-f]{4,8})(?=\s|$)")   # ours, wherever an older writer left it
DUE_F = re.compile(r"\s*📅\s*(\d{4}-\d{2}-\d{2})")
DONE_F = re.compile(r"\s*✅\s*(\d{4}-\d{2}-\d{2})")
DUE_OLD = re.compile(r"\s*\(due (?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+)?([A-Z][a-z]{2})\w*\.?\s+(\d{1,2})\)")
EST = re.compile(r"\s*\((\d+(?:\.\d+)?)\s*(min|h)\)\s*$")
# Tasks v2 (Nov 6): a step whose length is not known yet carries a timebox instead of an estimate,
# and an appointment step names its calendar event by key (taskids.event_key), hidden on the card.
TIMEBOX = re.compile(r"\s*\((?:timebox|discover,?)\s+(\d+)\s*min\)", re.I)   # "(discover, 30 min)": the Nov 5 draft
EVENT = re.compile(r"\s*\((ev-[0-9a-f]{8})\)")
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
        out = self.text
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


def parse_line(line: str, today: dt.date | None = None) -> Step | None:
    """A step line in any form (old markers or fields), or None for any other line."""
    m = LINE.match(line)
    if not m:
        return None
    today = today or dt.datetime.now(TZ).date()
    ind, box, kind, rest = m.group(1), m.group(2), m.group(3) or "", m.group(4)
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
    ev = EVENT.search(rest)
    event = ""
    if ev:
        event, rest = ev.group(1), rest[:ev.start()] + rest[ev.end():]
    rest = rest.rstrip()
    e = EST.search(rest)
    est = est0
    if e and not est:
        est = f"{e.group(1)} {e.group(2)}"
        rest = rest[:e.start()]
    state = {"[ ]": "open", "[x]": "done", "[X]": "done", "~~": "struck"}[box]
    return Step(state=state, kind=kind, text=rest.strip(), est=est, due=due, done=done, id=sid,
                note=note.strip(), indent=ind, box=None if est else tbox, event=event)


def format_line(s: Step) -> str:
    kind = f"{s.kind}: " if s.kind else ""
    words = s.text + (f" ({s.event})" if s.event else "") + (
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


def normalize(text: str, today: dt.date | None = None, opened: set | None = None) -> str:
    """The note with every step line of its Next action block in the one form: fields for the
    dates, an ID on each line. A tick gets today's ✅ only when the step was open before
    (`opened`: IDs and words, from _open_keys), never an old tick. Lines outside the block, the
    DoD and the Log are never touched. Idempotent."""
    today = today or dt.datetime.now(TZ).date()
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
    for i, s in parsed.items():
        if not s:
            continue
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
    t = re.sub(r"\s*\(the owner[^)]*\)\s*$", "", t or "")
    return re.sub(r"\W+", " ", t.lower()).strip()


def _snap(steps: list[Step]) -> list[dict]:
    return [{"id": s.id, "state": s.state, "kind": s.kind, "text": s.text, "est": s.est, "due": s.due,
             "done": s.done, **({"box": s.box} if s.box else {})} for s in steps if s.kind != "Waiting"]


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
        for k in ("due", "est", "box"):
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
        today = today or dt.datetime.now(TZ).date()
        key = _task_key(after, stem)
        new = _snap(steps_of(after.partition("\n---\n")[2], today))
        with _locked():
            old = (_snap(steps_of(before.partition("\n---\n")[2], today)) if before is not None
                   else (load_snap(key) or {}).get("steps"))
            changes = ([] if old is None else diff(old, new)) + list(extra or [])
            at = dt.datetime.now(TZ).isoformat(timespec="seconds")
            if changes:
                with open(HISTORY, "a", encoding="utf-8") as f:
                    for c in changes:
                        f.write(json.dumps({"at": at, "task_id": key if key.startswith("t-") else "", "task": stem,
                                            "source": source, **({"why": why} if why else {}), **c},
                                           ensure_ascii=False) + "\n")
            if old != new or before is None:
                SNAPS.mkdir(parents=True, exist_ok=True)
                tmp = _snap_path(key).with_suffix(".tmp")
                tmp.write_text(json.dumps({"task": stem, "at": at, "steps": new}, ensure_ascii=False), encoding="utf-8")
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


def write(p: Path, text: str, source: str, before: str | None = None, why: str = "",
          today: dt.date | None = None) -> str:
    """The one way to save a task note with steps: normalized, its step changes logged, written
    atomically. Returns the text as written (for an undo hash)."""
    today = today or dt.datetime.now(TZ).date()
    if before is None:
        try:
            before = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            before = ""
    out = normalize(text, today, _open_keys(steps_of(before.partition("\n---\n")[2], today)) if before else None)
    # The steps as last seen, before anyone else's edit: Tasks' check compares with them, so a
    # chat that edits by hand and then runs `vt step` counts as one change (Tasks v2).
    snap0 = (load_snap(_task_key(before, p.stem)) or {}).get("steps") if before else None
    if before:   # whatever changed since the last snapshot came from someone else
        record(p.stem, None, before, "outside", today)
    prev = snap0 if snap0 is not None else (_snap(steps_of(before.partition("\n---\n")[2], today)) if before else None)
    out, extra = on_write(prev, out, source, today, p.stem)
    tmp = p.with_suffix(".md.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, p)
    record(p.stem, before or None, out, source, today, why, extra)
    return out


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
    today = today or dt.datetime.now(TZ).date()
    text = p.read_text(encoding="utf-8")
    out = normalize(text, today, _open_keys(prev))
    out, extra = on_write(prev, out, source, today, p.stem)
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


def is_template(p) -> bool:
    """A template ("_Inbox Template"), not a task: no writer gives it ids (Nov 6: its status: inbox
    let normalize stamp "^s14e0 ^s9434" into it, ids every task made from it would inherit)."""
    return Path(p).name.startswith("_")


def sweep(paths, today: dt.date | None = None, dry: bool = False) -> dict:
    """Bring open task notes into the one form (the pre-run, daily): lines written by a chat or
    by hand get their fields and IDs, and their changes are logged as "outside" first."""
    today = today or dt.datetime.now(TZ).date()
    n = changed = 0
    for p in paths:
        if is_template(p):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if not re.search(r"^status:\s*(inbox|open|in-progress)\s*$", text.partition("\n---\n")[0], re.M):
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
