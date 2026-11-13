"""Daily, 07:40 Pacific: do the task notes agree with what Carmen will show today?

Why (Nov 11): a task's structure (order, waits, dates, links) lives in free text that many writers
edit: Carmen's taps, chats, the pre-run, skills and the owner in Obsidian. That day's visible bugs were
all one shape, a note and Carmen's view disagreeing: a wait not relinked (HR), stale dates in the
words, unchained parts (M1), a checklist outside the block, a same-note link, and Do marking a task
Finished while a booked block still held one of its steps. `vt step` makes the steps change through
one door; this job is the net for whatever still slips through, his own Obsidian edits included.

What it checks, for every open task in `Inbox/` (`type: action`), with Carmen's own functions
(tools/steps, tasks.read, planning.do) and no copy of a date, estimate or wait pattern:

  stale_link    a step shown free whose `after:` points only at done steps while a newer open
                Waiting line exists (HR Nov 11: the lab draw sat free after the reply check)
  past_words    an open step whose words schedule it for a date that has passed ("for Nov 10 or
                11", "Oct 15: …", "(Mon Nov 9)"); a past event named as context is not stale
  past_due      an open step whose 📅 has passed
  do_finished   yesterday's Do said Finished while a block booked that day still held one of its
                open steps (M1 Nov 11); do.json is read, never written
  stray_box     an open checkbox outside `## Next action` (steps.stray_boxes)
  bad_link      a workspace link or a wikilink in a step that does not resolve: no such note, or
                no such section (a same-note `[[#…]]` included)
  form          steps.form_problems on an open line
  earlier       a step that takes an earlier step's result without `after:` (steps.earlier_needed)
  part_*        parts of a series ("part 2 of 3") unchained, out of order in the note or in
                Carmen's computed order, chained backwards, or numbered twice

Repairs (Nov 11, the owner: "contradictions i wont check manually myself this has to be automated"):
what is decidable is fixed in the note through the shared step operations (tools/steps op_*, as
`vt step`), one at a time, each with a Log line `consistency check: … · automatic`: relink to the
newer wait, chain and order the parts, move a stray box into the block, give same-note links the
note's name, link a step to the producer right before it, take a passed date out of the words when
the 📅 holds the real one, drop a passed 📅 on a step that now waits. A line carrying "(the owner" is
never rewritten: its links are proposed by the 08:00 pre-run (`_relink_props`, `_earlier_props`).
What only he can decide (a step whose scheduled date has passed, a free step past its 📅) is asked
the same way, one tap on the task's row in the morning panel (`panel_asks`, D-29); nothing is a list
he reads. `_system/maintenance/consistency-today.md` is the record only (overwritten every run).
No push for findings; a failed or missing run reaches him through the job's checks and heartbeat.
do.json and the calendar are never written.

    ssh vault-host '~/automation/bin/run-job consistency_check --dry-run'
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from runner import config
from runner.job import Job, log, IMPORTANT, AUTOMATIC
from tools import steps as S

COVERS = "task-consistency"
PAGE_REL = "_system/maintenance/consistency-today.md"
SLOT = "07:40"          # Pacific; the timer's OnCalendar must say the same (tests.consistency_check)

# Words before a date in a step that make it history, not a target: "checked Oct 26:", "the trek ran
# Oct 8". A gate ("from Nov 17") that has passed is not stale either: the step is simply doable now.
_HISTORY_BEFORE = re.compile(r"\b(?:asked|sent|booked|submitted|since|proof|received|replied|confirmed|ordered|paid|"
                             r"wrote|written|got|done|checked|signed|ran|was|were|bought|downloaded|approved|sold out|"
                             r"from|after:?|starting|opens(?: on)?|not before|no earlier than)\s+(?:on\s+)?$", re.I)
# A bracket is a detail or a reason ("(certificate approved Oct 30)"): its dates count only when the
# bracket is nothing but a date, as in "(Mon Nov 9)", the day the step was for.
_BRACKET = re.compile(r"\(([^()]*)\)")
_ONLY_DATE = re.compile(rf"^\s*{S.WDAY_OPT}{S.DATE_RX}\s*$")
# "Oct 10 to 11", "Nov 5 to Nov 12": a range is past only when its end is.
_RANGE_END = re.compile(rf"^(?:,?\s*\d{{4}})?\s*(?:to|or|and|-|–)\s*(?:({S.MONTHS_RX})\w*\.?\s+)?(\d{{1,2}})\b(?:,?\s*(\d{{4}}))?")
HIS = re.compile(r"\(the owner\b")   # his attribution: his words, never rewritten (proposed instead)
# The words that lead into a date: "for Nov 10", "by Mon Nov 9", "on Nov 3".
_LEAD = re.compile(rf"(?:\b(?:for|on|{S.DUE_WORDS_RX})\s+)?\(?\s*$", re.I)
# A date the step is scheduled for, the only kind that goes stale (Nov 12: "Carl's Nov 11 call" and
# "the Mon Nov 9 run" are references to past events, not stale steps): a deadline or scheduling word
# right before it (the shared deadline words, plus on and for), the step's own time at its start
# ("Oct 15: …"), a range ("weekend Oct 10 to 11"), or a bracket holding only the date ("(Mon Nov 9)").
_SCHEDULED_BEFORE = re.compile(rf"\b(?:{S.DUE_WORDS_RX}|on|for)\s+$", re.I)
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.M)


def _f(task: str, step: str, code: str, what: str, fix: str) -> dict:
    return {"task": task, "step": step, "code": code, "what": what, "fix": fix}


def _words(x: S.Step, n: int = 8) -> str:
    w = S.clean(S.HIS_TAG.sub("", x.text)).split()
    return " ".join(w[:n]) + ("…" if len(w) > n else "")


def _label(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


# --- the rules, on a note's steps ---------------------------------------------------------------

def stale_links(steps: list[S.Step]) -> list[tuple[S.Step, S.Step]]:
    """(step, the open Waiting it should follow) for each open step of his that Carmen shows free
    (not in blocked_ids) although its `after:` names only closed steps and an open Waiting line sits
    after the latest of them: the wait moved on and the link stayed behind (HR, Nov 11). The one
    rule; the pre-run proposes the relink with it."""
    pos = {x.id: k for k, x in enumerate(steps) if x.id}
    by = {x.id: x for x in steps if x.id}
    blocked = S.blocked_ids(steps)
    out = []
    for x in steps:
        if x.state != "open" or x.kind == "Waiting" or not x.id or not x.after or x.id in blocked:
            continue
        targets = [t for t in x.after if t in by]
        if not targets or any(by[t].state == "open" for t in targets):
            continue
        last = max(pos[t] for t in targets)
        newer = next((w for w in steps[last + 1:] if w.state == "open" and w.kind == "Waiting" and w.id
                      and w.id not in x.after), None)
        if newer:
            out.append((x, newer))
    return out


def date_findings(task: str, steps: list[S.Step], lines: list[str], today: dt.date) -> list[dict]:
    """past_words and past_due, open steps of his only (a Waiting line's dates say when he asked)."""
    out = []
    for ln in lines:
        x = S.parse_line(ln, today)
        if not x or x.state != "open" or x.kind == "Waiting":
            continue
        if x.due and x.due < today.isoformat() and (S.DUE_F.search(ln) or S.DUE_OLD.search(ln)):
            out.append({**_f(task, x.id, "past_due",
                             f"\"{_words(x)}\" is still open and its date, {_label(x.due)}, has passed.",
                             "Tick it if it is done, else move the 📅 to the day it now happens."), "date": x.due})
            continue
        # Masked with spaces of the same length, so a match's span is the span in x.text (a line of
        # his keeps its tag in the text; spans are only used on lines that carry none).
        words = _BRACKET.sub(lambda m: m.group(0) if _ONLY_DATE.match(m.group(1)) else " " * len(m.group(0)),
                             x.text if not HIS.search(x.text) else S.HIS_TAG.sub("", x.text))
        for m in S.DATE_WORDS.finditer(words):
            iso = S.month_day(m.group(2), m.group(3), today)
            yr = re.match(r",?\s*(\d{4})\b", words[m.end():])   # "Oct 9, 2027": the year it names
            iso = yr.group(1) + iso[4:] if iso and yr else iso
            if not iso or iso >= today.isoformat() or _HISTORY_BEFORE.search(words[:m.start()]):
                continue
            end = _RANGE_END.match(words[m.end():])
            if end and _range_end(end, m.group(2), today) >= today.isoformat():
                continue
            after = words[m.end() + (end.end() if end else 0):]
            before = words[:m.start()]
            if not (_SCHEDULED_BEFORE.search(before) or end or re.search(r"\(\s*$", before)
                    or (not before.strip() and after.lstrip().startswith(":"))):
                continue   # a past event named as context, not the step's own date
            stop = m.end() + (end.end() if end else (yr.end() if yr else 0))
            out.append({**_f(task, x.id, "past_words",
                             f"\"{_words(x)}\" names {_label(iso)} in its words, a day that has passed.",
                             "Reword the step for when it happens now, or strike it if the moment is gone."),
                        "span": (m.start(), stop), "date": iso})
            break
    return out


def _range_end(end, mon: str, today: dt.date) -> str:
    """The end of a "Nov 5 to Nov 12" / "Oct 9, 2026 to Oct 9, 2027" range as YYYY-MM-DD, or ""."""
    iso = S.month_day(end.group(1) or mon, end.group(2), today)
    if iso and end.group(3):
        iso = end.group(3) + iso[4:]
    return iso


def part_findings(task: str, steps: list[S.Step], plan: list | None = None) -> list[dict]:
    """Parts of one series (steps.part_of): numbered twice, out of order in the note, chained
    backwards, unchained (steps.earlier_needed through part_order), out of order in Carmen's plan."""
    out = []
    series: dict = {}
    for k, x in enumerate(steps):
        p = S.part_of(x.text) if x.state == "open" and x.id else None
        if p:
            series.setdefault((p[0], p[2]), []).append((p[1], k, x))
    need = S.earlier_needed(steps)
    order = {sid: k for k, sid in enumerate(_plan_ids(plan))}
    for (_name, total), parts in series.items():
        nums = [n for n, _, _ in parts]
        dup = sorted({n for n in nums if nums.count(n) > 1})
        if dup:
            out.append(_f(task, parts[0][2].id, "part_twice",
                          f"\"{_words(parts[0][2], 5)}\" has part {dup[0]} of {total} twice.",
                          "Renumber the parts, or strike the duplicate."))
        in_file = [n for n, _, _ in sorted(parts, key=lambda t: t[1])]
        if in_file != sorted(in_file):
            out.append(_f(task, parts[0][2].id, "part_order",
                          f"The parts of \"{_words(parts[0][2], 5)}\" stand in the note as {', '.join(map(str, in_file))}.",
                          "Put the lines in part order."))
        num = {x.id: n for n, _, x in parts}
        for n, _, x in parts:
            back = [t for t in x.after if t in num and num[t] >= n]
            if back:
                out.append(_f(task, x.id, "part_backwards",
                              f"Part {n} of \"{_words(x, 5)}\" waits on part {num[back[0]]}, which comes after it.",
                              f"Link part {n} to part {n - 1} instead, or leave it free if it is part 1."))
            elif x.id in need and not x.after:
                out.append(_f(task, x.id, "part_unchained",
                              f"Part {n} of \"{_words(x, 5)}\" has no `after:` on the part before it, so it can be planned first.",
                              f"Add (after: ^{need[x.id]})."))
        if order:
            ranked = [n for n, _, x in sorted((t for t in parts if t[2].id in order), key=lambda t: order[t[2].id])]
            if ranked != sorted(ranked):
                out.append(_f(task, parts[0][2].id, "part_plan",
                              f"Carmen plans the parts of \"{_words(parts[0][2], 5)}\" as {', '.join(map(str, ranked))}.",
                              "Chain each part to the one before it with (after: ^id)."))
    return out


def _plan_ids(plan) -> list[str]:
    out = []
    for x in plan or []:
        sid = x.get("id") if isinstance(x, dict) else x
        if isinstance(sid, str) and sid:
            out.append(sid)
    return out


class Links:
    """Resolves a wikilink the way Obsidian does: a path, else a file name anywhere in the vault."""

    def __init__(self, vault: Path):
        self.vault = vault
        self._index: dict | None = None
        self._heads: dict = {}

    def _files(self) -> dict:
        if self._index is None:
            self._index = {}
            for p in self.vault.rglob("*"):
                rel = p.relative_to(self.vault).parts
                if any(s.startswith(".") for s in rel) or not p.is_file():
                    continue
                self._index.setdefault(p.name.lower(), p)
                if p.suffix == ".md":
                    self._index.setdefault(p.stem.lower(), p)
        return self._index

    def note(self, name: str, here: Path) -> Path | None:
        if name == here.stem:
            return here
        if "/" in name:
            for c in (self.vault / name, self.vault / f"{name}.md"):
                if c.is_file():
                    return c
            return None
        c = here.parent / f"{name}.md"
        return c if c.is_file() else self._files().get(name.lower())

    def has_section(self, p: Path, sec: str) -> bool:
        if p not in self._heads:
            text = p.read_text(encoding="utf-8", errors="replace")
            self._heads[p] = ({_norm_head(h) for h in _HEADING.findall(text)},
                              set(re.findall(r"\^([A-Za-z0-9-]+)\s*$", text, re.M)))
        heads, blocks = self._heads[p]
        if sec.startswith("^"):
            return sec[1:] in blocks
        return _norm_head(sec.split("#")[-1]) in heads


def _norm_head(h: str) -> str:
    return re.sub(r"\s+", " ", h).strip().casefold()


def link_findings(task: str, p: Path, lines: list[str], links: Links) -> list[dict]:
    """bad_link: the wikilinks of open steps and of their workspace lines."""
    out = []
    r = S.block_range(lines)
    if not r:
        return out
    for i in range(r[0] + 1, r[1]):
        x = S.parse_line(lines[i])
        if not x or x.state != "open":
            continue
        for ln in [lines[i]] + S.workspace_of(lines, i):
            for m in S.WIKILINK.finditer(ln):
                w = S.wikilink(m.group(0), p.stem)
                if not w:
                    continue
                name, sec, label = w
                target = links.note(name, p)
                if target is None:
                    out.append(_f(task, x.id, "bad_link",
                                  f"The link \"{label}\" under \"{_words(x, 6)}\" points at a note that does not exist: {name}.",
                                  "Point it at the right note, or remove it."))
                elif sec and not links.has_section(target, sec):
                    where = "this note" if target == p else target.stem
                    out.append(_f(task, x.id, "bad_link",
                                  f"The link \"{label}\" under \"{_words(x, 6)}\" points at a section of {where} that does not exist: {sec}.",
                                  "Point it at a heading that exists, or link the note without a section."))
    return out


def check_note(p: Path, today: dt.date, links: Links, live: dict | None = None) -> list[dict]:
    """Every finding for one task note. `live`: Carmen's read of it (tasks.read_note), for the
    computed step order; None checks the note's own text only."""
    text = p.read_text(encoding="utf-8", errors="replace")
    task = p.stem
    out = []
    for b in S.stray_boxes(text):
        out.append(_f(task, "", "stray_box", f"An open checkbox sits outside ## Next action: \"{b[:90]}\".",
                      "Move it into the Next action block as a step, or make it plain text."))
    head, sep, body = text.partition("\n---\n")
    lines = body.split("\n")
    steps = S.steps_of(body, today)
    if not steps:
        return out
    for x, w in stale_links(steps):
        out.append(_f(task, x.id, "stale_link",
                      f"\"{_words(x)}\" shows as free: it waits only on done steps while \"{_words(w, 6)}\" is open.",
                      f"Link it to the open wait: (after: ^{w.id})."))
    r = S.block_range(lines)
    out += date_findings(task, steps, lines[r[0] + 1:r[1]] if r else [], today)
    for i, (ln, probs) in S.note_problems(body, today).items():
        x = S.parse_line(ln, today)
        if not x or x.state != "open":
            continue
        for pr in probs:
            out.append(_f(task, x.id, "form", f"\"{_words(x)}\": {pr['title'].lower()}.", pr["why"]))
    parts = S.part_order(steps)
    for sid, prev in S.earlier_needed(steps).items():
        if sid in parts:
            continue   # a part: part_findings says it in its own words
        x = next(y for y in steps if y.id == sid)
        y = next((y for y in steps if y.id == prev), None)
        out.append(_f(task, sid, "earlier",
                      f"\"{_words(x)}\" takes the result of \"{_words(y, 6) if y else prev}\" but has no `after:`.",
                      f"Add (after: ^{prev}), or leave it if it really is free."))
    plan = ((live or {}).get("order") or {}).get("plan")
    out += part_findings(task, steps, plan)
    out += link_findings(task, p, lines, links)
    return out


# --- repairs (Nov 11, the owner: "contradictions i wont check manually myself this has to be automated") -
#
# What is decidable is fixed through the shared step code (tools/steps op_*, the same operations as
# `vt step`), one repair at a time, each with a Log line on the task; the note is re-read after each.
# A line carrying "(the owner" is his: never rewritten here (a link on it is proposed by the pre-run);
# moving it is fine, its words stay. Anything ambiguous is left on the page.

def _rep(step: str, code: str, said: str, fn) -> dict:
    return {"step": step, "code": code, "said": said, "fn": fn}


def _raw(lines: list[str], r, sid: str) -> tuple[int, str]:
    for i in range(r[0] + 1, r[1]):
        st = S.parse_line(lines[i])
        if st and st.id == sid:
            return i, lines[i]
    return -1, ""


def _prev_step(lines: list[str], r, i: int) -> str:
    """The id of the open step line right before line i in the block (workspace lines, done and
    struck lines skipped), or ""."""
    for j in range(i - 1, r[0], -1):
        st = S.parse_line(lines[j])
        if st and st.state == "open":
            return st.id if st.kind != "Waiting" else ""
    return ""


def _names_producer(x: S.Step, y: S.Step) -> bool:
    """Whether step x's words tie its result to step y, the step right before it: a pronoun ("pack
    it", "wear them") or a result y's words name ("send the certificate" after "get the
    certificate"). "Pick up at Walgreens" names no producer, so it stays a proposal."""
    m = S.RESULT_STEP.match(S.clean(S.HIS_TAG.sub("", x.text)))
    if not m:
        return False
    last = m.group(0).split()[-1].lower()
    if last in ("it", "them", "this", "these"):
        return True
    noun = last.rstrip("s")
    return len(noun) >= 4 and noun in S.clean(y.text).lower()


def strip_date(text: str, span: tuple[int, int]) -> str:
    """The step's words without the date at span and the words leading into it ("for Nov 10 or
    11", "(Mon Nov 9)"), tidied."""
    a, b = span
    lead = _LEAD.search(text[:a])
    a = lead.start() if lead else a
    if "(" in text[a:span[0]] and text[b:].lstrip().startswith(")"):
        b += len(text[b:]) - len(text[b:].lstrip()) + 1
    out = text[:a].rstrip() + (" " if text[b:].strip() and not text[b:].lstrip()[:1] in ",.;:)" else "") + text[b:].lstrip()
    out = re.sub(r"\s+([,.;:])", r"\1", re.sub(r"\s{2,}", " ", out))
    return re.sub(r"[\s,;:]+$", "", out).strip()


def _move_stray(stem: str, box: str):
    def fn(t: str) -> str:
        head, sep, body = t.partition("\n---\n")
        lines = body.split("\n")
        r = S.block_range(lines)
        if not sep or not r:
            raise S.Refused("no `## Next action` block")
        at = next((i for i, l in enumerate(lines) if l.strip() == box and not (r[0] < i < r[1])), None)
        if at is None:
            raise S.Refused("the box is no longer there")
        end = at + 1
        while end < len(lines) and re.match(r"^\s{2,}\S", lines[end]) and re.match(r"^(\s*)", lines[at]).group(1) < re.match(r"^(\s*)", lines[end]).group(1):
            end += 1
        rest = re.sub(r"^- \[ \]\s*(?:(?:Next|Then):\s*)?", "", box)
        moved = ["- [ ] Then: " + rest] + ["    " + l.strip() for l in lines[at + 1:end]]
        del lines[at:end]
        r = S.block_range(lines)
        j = r[1]
        while j > r[0] + 1 and not lines[j - 1].strip():
            j -= 1
        lines[j:j] = moved
        return head + sep + "\n".join(lines)
    return fn


def _link_lines(lines: list[str], r) -> list[str]:
    """The workspace lines in the block with a same-note link to a section this note has."""
    heads = {_norm_head(h) for h in _HEADING.findall("\n".join(lines))}
    return [l.strip() for l in lines[r[0] + 1:r[1]] if re.match(r"^\s{2,}- ", l)
            and any(_norm_head(m) in heads for m in re.findall(r"\[\[#([^\]|]+)", l))]


def _name_links(stem: str, heads: set, only: str = ""):
    """Same-note links given the note's name: every such workspace line not carrying "(the owner"
    (D-29), or with `only`, that one line (his tap on the proposal)."""
    def fn(t: str) -> str:
        head, sep, body = t.partition("\n---\n")
        lines = body.split("\n")
        r = S.block_range(lines)
        n = 0
        for i in range(r[0] + 1, r[1]):
            if only and lines[i].strip() != only or not only and HIS.search(lines[i]):
                continue
            if re.match(r"^\s{2,}- ", lines[i]) and "[[#" in lines[i]:
                def sub(m):
                    nonlocal n
                    if _norm_head(m.group(1)) in heads:
                        n += 1
                        return f"[[{stem}#{m.group(1)}"
                    return m.group(0)
                lines[i] = re.sub(r"\[\[#([^\]|]+)", sub, lines[i])
        if not n:
            raise S.Refused("no same-note link to a section that exists")
        return head + sep + "\n".join(lines)
    return fn


# D-29
def repairs(p: Path, text: str, today: dt.date) -> list[dict]:
    """The decidable repairs for a note as it stands, in order; the caller applies the first."""
    stem = p.stem
    head, sep, body = text.partition("\n---\n")
    lines = body.split("\n")
    r = S.block_range(lines)
    if not sep or not r:
        return []
    steps = S.steps_of(body, today)
    his = {x.id for x in steps if x.id and HIS.search(_raw(lines, r, x.id)[1])}
    out = []
    for x, w in stale_links(steps):
        if x.id not in his:
            out.append(_rep(x.id, "stale_link", f"\"{_words(x, 6)}\" now waits on the open \"{_words(w, 5)}\" (^{w.id}); its old link was to done steps only",
                            lambda t, x=x, w=w: S.op_link(t, x.id, [w.id], today)))
    # Parts of a series: in part order in the note, each after the part before it.
    series: dict = {}
    for k, x in enumerate(steps):
        q = S.part_of(x.text) if x.state == "open" and x.id else None
        if q:
            series.setdefault((q[0], q[2]), []).append((q[1], k, x))
    need = S.earlier_needed(steps)
    for parts in series.values():
        nums = [n for n, _, _ in parts]
        if len(set(nums)) != len(nums):
            continue   # numbered twice: which is which is his call
        parts.sort()
        for (n0, k0, a), (n1, k1, b) in zip(parts, parts[1:]):
            if k1 < k0 and b.id not in his:   # his line is never moved (D-29)
                out.append(_rep(b.id, "part_order", f"part {n1} of \"{_words(b, 4)}\" moved after part {n0}",
                                lambda t, a=a, b=b: S.op_move(t, b.id, a.id, "after")))
                break
        ids = {x.id for _, _, x in parts}
        num = {x.id: n for n, _, x in parts}
        for i, (n, _, x) in enumerate(parts):
            prev = parts[i - 1][2].id if i else ""
            back = [t for t in x.after if t in num and num[t] >= n]
            if back and x.id not in his:
                keep = [t for t in x.after if t not in ids] + ([prev] if prev else [])
                out.append(_rep(x.id, "part_backwards", f"part {n} of \"{_words(x, 4)}\" now follows part {parts[i - 1][0]}" if prev
                                else f"part {n} of \"{_words(x, 4)}\" no longer waits on a later part",
                                lambda t, x=x, keep=keep: S.op_link(t, x.id, keep, today)))
            elif prev and not x.after and x.id in need and x.id not in his:
                out.append(_rep(x.id, "part_unchained", f"part {n} of \"{_words(x, 4)}\" now follows part {parts[i - 1][0]} (^{prev})",
                                lambda t, x=x, prev=prev: S.op_link(t, x.id, [prev], today)))
    parts_of = S.part_order(steps)
    for sid, prev in need.items():
        if sid in parts_of or sid in his:
            continue
        i, _ = _raw(lines, r, sid)
        if _prev_step(lines, r, i) != prev:
            continue   # the producer is not the step right before: ambiguous, left on the page
        x = next(y for y in steps if y.id == sid)
        y = next(y for y in steps if y.id == prev)
        if not _names_producer(x, y):
            continue   # "pick up ..." takes some earlier result, not necessarily this one: proposed
        out.append(_rep(sid, "earlier", f"\"{_words(x, 6)}\" now follows \"{_words(y, 5)}\" (^{prev}), whose result it takes",
                        lambda t, sid=sid, prev=prev: S.op_link(t, sid, [prev], today)))
    for box in S.stray_boxes(text):
        if HIS.search(box):
            continue   # his line: never moved or rewritten, proposed in the panel (D-29)
        out.append(_rep("", "stray_box", f"the open box \"{box[6:70]}\" moved into ## Next action as a step", _move_stray(stem, box)))
    heads = {_norm_head(h) for h in _HEADING.findall(text)}
    if any(not HIS.search(l) for l in _link_lines(lines, r)):
        out.append(_rep("", "same_note_link", "same-note links given the note's name", _name_links(stem, heads)))
    blocked = S.blocked_ids(steps)
    for f in date_findings(stem, steps, lines[r[0] + 1:r[1]], today):
        x = next((y for y in steps if y.id == f["step"]), None)
        if not x or not x.id or x.id in his:
            continue
        _, raw = _raw(lines, r, x.id)
        if f["code"] == "past_words" and S.DUE_F.search(raw) and x.due >= today.isoformat():
            words = strip_date(x.text, f["span"])
            if words and words != x.text:
                out.append(_rep(x.id, "past_words", f"\"{_words(x, 6)}\": the passed date taken out of its words, the 📅 {_label(x.due)} holds",
                                lambda t, x=x, words=words: S.op_reword(t, x.id, words, "", today)))
        elif f["code"] == "past_due" and x.id in blocked:
            out.append(_rep(x.id, "past_due", f"\"{_words(x, 6)}\": the passed 📅 {_label(x.due)} dropped, the step waits now",
                            lambda t, x=x: S.op_date(t, x.id, "none", today)))
    return out


def repair_note(p: Path, today: dt.date, dry: bool = False, limit: int = 25) -> tuple[list[dict], list[dict]]:
    """Apply the repairs one at a time, re-reading the note after each, through the Notes adapter
    (compare-and-swap, steps.write, the step history) with a Log line each. (done, refused); dry:
    computed on the text in memory, nothing written."""
    from jobs.checkin_app.adapters import notes, clock
    from tools.step import _log
    done, refused, skip = [], [], set()
    text = p.read_text(encoding="utf-8")
    label = clock._label(today)
    for _ in range(limit):
        todo = [x for x in repairs(p, text, today) if (x["code"], x["step"], x["said"]) not in skip]
        if not todo:
            break
        x = todo[0]
        line = f"- {label} · consistency check: {x['said']} · automatic"
        try:
            new = _log(x["fn"](text), line)
        except S.Refused as e:
            skip.add((x["code"], x["step"], x["said"]))
            refused.append({"task": p.stem, "step": x["step"], "code": x["code"], "said": x["said"], "why": str(e)})
            continue
        if dry:
            text = new
        else:
            def again(cur, x=x, line=line):
                try:
                    return _log(x["fn"](cur), line)
                except S.Refused:
                    notes.refuse()
            try:
                notes.write_from(p, text, new, again, "consistency_check")
            except notes.NoteChanged as e:
                refused.append({"task": p.stem, "step": x["step"], "code": x["code"], "said": x["said"], "why": str(e) or "the note changed"})
                break
            text = p.read_text(encoding="utf-8")
        skip.add((x["code"], x["step"], x["said"]))
        done.append({"task": p.stem, "step": x["step"], "code": x["code"], "said": x["said"]})
    return done, refused


# --- what is left goes to his morning panel (D-29) ------------------------------------------------
_OPEN_LINE = re.compile(r"^\s*- \[ \] (?:(Next|Then):\s*)?(?!Waiting:)(.+?)\s*$")


def _old(raw: str) -> str:
    """A step line as a panel proposal names it ("Then: <the line after the box>"), the form
    tasks.actions.apply_prop matches; a line without Next/Then reads as Then."""
    m = _OPEN_LINE.match(raw)
    return f"{m.group(1) or 'Then'}: {m.group(2)}" if m else ""


# D-29
def panel_asks(today: dt.date, vault: Path | None = None) -> dict:
    """{task: [proposal]} for what the repairs left that only he can decide, in the pre-run's
    `prop` shape with its own tap labels: a step whose scheduled date has passed (Strike or Keep),
    a free step whose 📅 has passed (Move to today or Keep), and his own lines the repairs never
    touch: a box of his outside the block (Move in or Keep), a same-note link of his (Fix link). Read only. The pre-run puts them on
    the task's row in the morning panel (plan_today_prerun._consistency_rows)."""
    vault = vault or config.VAULT
    links = Links(vault)
    out: dict = {}
    for p in open_tasks(vault / "Inbox"):
        try:
            fs = [f for f in check_note(p, today, links) if f["code"] in ("past_words", "past_due")]
        except Exception:   # noqa: BLE001 - the job reports it; the panel just goes without
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        body = text.partition("\n---\n")[2]
        blines = body.split("\n")
        br = S.block_range(blines)
        # His lines the repairs leave alone (D-29): asked, his tap does it.
        for box in S.stray_boxes(text):
            if HIS.search(box):
                out.setdefault(p.stem, []).append({"old": box, "new": "", "field": "move_in", "why": "it sits outside ## Next action",
                                                   "yes": "Move in", "no": "Keep"})
        for l in (_link_lines(blines, br) if br else []):
            if HIS.search(l):
                out.setdefault(p.stem, []).append({"old": l, "new": "", "field": "name_link", "why": "a same-note link names no note",
                                                   "yes": "Fix link", "no": "Keep"})
        if not fs:
            continue
        raws = {S.parse_line(l).id: l for l in body.split("\n") if S.parse_line(l) and S.parse_line(l).id}
        for f in fs:
            old = _old(raws.get(f["step"], ""))
            if not old:
                continue
            day = _label(f["date"])
            if f["code"] == "past_words":
                prop = {"old": old, "new": "", "why": f"it names {day}, which has passed", "yes": "Strike", "no": "Keep"}
            else:
                prop = {"old": old, "new": old, "field": "due", "due": today.isoformat(),
                        "why": f"its date, {day}, has passed", "yes": "Move to today", "no": "Keep"}
            out.setdefault(p.stem, []).append(prop)
    return out


# --- yesterday's Do (M1, Nov 11) ------------------------------------------------------------------

def do_findings(day: dt.date, note) -> list[dict]:
    """do_finished: tasks Do showed Finished on `day` (no step of the day's chunk left open,
    planning.do._today_left) while a block booked that day held a step that is still open.
    `note(stem)` is Carmen's read of a task (tasks.read_note) or None. Reads do.json only."""
    from jobs.checkin_app.planning import do
    e = do._day(do._load(), day)
    out = []
    for i in sorted({b.get("task") for b in e["block_steps"] if b.get("task")}):
        st = note(i)
        if not st or st.get("status") in ("done", "cancelled") or do._today_left(e, i, note) \
                or not (e["chunk_ids"].get(i)):
            continue
        by = {x["id"]: x for x in st.get("step_states") or [] if x.get("id")}
        for b in e["block_steps"]:
            if b.get("task") != i:
                continue
            held = [by[s] for s in b.get("sids") or [] if s in by and by[s]["state"] == "open" and not do._waits(by[s])]
            if held:
                s0 = int(b.get("s") or 0)
                out.append(_f(i, held[0]["id"], "do_finished",
                              f"Do showed this task Finished on {day:%b} {day.day} while the {s0 // 60:02d}:{s0 % 60:02d} block "
                              f"still held its open step \"{S.clean(held[0]['text'])[:70]}\".",
                              "Tick the step if it was done; if not, plan it again. Do's chunk missed a booked step: worth a bug note."))
                break
    return out


# --- the run ---------------------------------------------------------------------------------------

def open_tasks(inbox: Path) -> list[Path]:
    out = []
    for p in sorted(inbox.glob("*.md")):
        if S.is_template(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        head = text.partition("\n---\n")[0]
        if S.is_task(text) and re.search(r"(?m)^status:\s*(inbox|open|in-progress)\s*$", head):
            out.append(p)
    return out


def run(today: dt.date, vault: Path | None = None, repair: bool = False, dry: bool = False) -> tuple[list[dict], list[str]]:
    """(findings, errors) for every open task and yesterday's Do; with repair, (findings, errors,
    repaired, refused): the decidable repairs applied first (dry: computed, not written), the
    findings what is left. A note that cannot be checked is an error, never a silent skip."""
    from jobs.checkin_app.tasks import read as tasks_read
    from jobs.checkin_app.adapters import notes
    vault = vault or config.VAULT
    links = Links(vault)
    findings, errors, repaired, refused = [], [], [], []
    for p in open_tasks(vault / "Inbox"):
        if repair:
            try:
                d, r = repair_note(p, today, dry)
                repaired += d
                refused += r
            except Exception as e:   # noqa: BLE001 - the note is still checked as it stands
                errors.append(f"{p.stem}: repair failed ({type(e).__name__}: {e})")
        live = None
        try:
            live = tasks_read.read_note(p)
        except Exception as e:   # noqa: BLE001 - the note's own text is still checked
            errors.append(f"{p.stem}: Carmen could not read it ({type(e).__name__}: {e})")
        try:
            findings += check_note(p, today, links, live)
        except Exception as e:   # noqa: BLE001
            errors.append(f"{p.stem}: {type(e).__name__}: {e}")

    def note(i):
        q = notes.task_path(i)
        return tasks_read.read_note(q) if q else None
    try:
        findings += do_findings(today - dt.timedelta(days=1), note)
    except Exception as e:   # noqa: BLE001
        errors.append(f"yesterday's Do: {type(e).__name__}: {e}")
    return (findings, errors, repaired, refused) if repair else (findings, errors)


PROPOSED = ("stale_link", "earlier", "part_unchained", "past_words", "past_due")   # asked in the morning panel


def page(findings: list[dict], errors: list[str], today: dt.date, now: dt.datetime,
         repaired: list[dict] = (), refused: list[dict] = ()) -> str:
    """The record of a run: what was repaired (each also a Log line on its task), and what was left
    (his lines, proposed in the morning panel, and what needs a decision)."""
    out = ["---", "type: reference", "component: task-consistency", f"last_run: {now:%Y-%m-%dT%H:%M:%SZ}",
           f"healthy: {'false' if errors else 'true'}", "---", "",
           f"# Task consistency, {today:%b} {today.day}", "",
           f"**Generated {now:%b} {now.day} {now:%H:%M} UTC by `jobs/consistency_check.py`. Do not edit; every run "
           "overwrites this file.** A record, not a reading list: decidable contradictions are repaired in the "
           "task note with a Log line (`consistency check: … · automatic`), what it cannot decide is asked "
           "as a one-tap proposal in the morning check-in panel (D-29). What it checks and why: [[checkin-app]] § Agents and jobs.", ""]
    out.append(f"**{len(repaired)} repaired, {len(findings)} left.**" if repaired or findings else
               "**Nothing off.** Every open task note agrees with what Carmen shows today.")
    if repaired:
        out += ["", "## Repaired", ""]
        out += [f"- [[{x['task']}]]: {x['said']}" + (f" `^{x['step']}`" if x["step"] else "") for x in repaired]
    if refused:
        out += ["", "## Repair refused", ""]
        out += [f"- [[{x['task']}]]: {x['said']}. Refused: {x['why']}" for x in refused]
    tasks = list(dict.fromkeys(f["task"] for f in findings))
    if tasks:
        out += ["", "## Left", ""]
    for t in tasks:
        out += [f"### [[{t}]]", ""]
        for f in (x for x in findings if x["task"] == t):
            sid = f" `^{f['step']}`" if f["step"] else ""
            how = " Asked in the morning panel." if f["code"] in PROPOSED else ""
            out.append(f"- {f['what']}{sid} Fix: {f['fix']}{how} <!--{f['code']}-->")
        out.append("")
    if errors:
        out += ["", "## Could not check", ""] + [f"- {e}" for e in errors]
    return "\n".join(out).rstrip() + "\n"


def deliver(findings: list[dict], errors: list[str], today: dt.date, now: dt.datetime,
            repaired: list[dict] = (), refused: list[dict] = ()) -> None:
    """The page, every run, so yesterday's record never lingers. No push: findings are repaired or
    proposed, never a list for him (Nov 11); a failed or missing run reaches him through the job's
    checks and heartbeat."""
    (config.VAULT / PAGE_REL).write_text(page(findings, errors, today, now, repaired, refused), encoding="utf-8")


def main() -> int:
    from jobs.checkin_app.adapters import clock
    with Job("consistency_check", description="Task notes agree with Carmen") as job:
        if not job.check("Inbox reachable", ok=config.INBOX.is_dir(), tier=IMPORTANT, owner=AUTOMATIC,
                         covers="vault-reachable", detail=f"{config.INBOX} is not a directory"):
            return 0
        now = dt.datetime.now(dt.timezone.utc)
        today = clock.carmen_day()
        findings, errors, repaired, refused = run(today, repair=True, dry=job.dry_run)
        tasks = len({f["task"] for f in findings})
        job.record("repaired", len(repaired))
        job.record("repair_refused", len(refused))
        job.record("findings_left", len(findings))
        job.record("tasks_with_findings", tasks)
        job.record("errors", len(errors))
        job.check("every open task could be checked and repaired", ok=not errors, tier=IMPORTANT, owner=AUTOMATIC,
                  covers=COVERS, key="consistency-errors",
                  detail=f"{len(errors)} could not be checked: " + "; ".join(errors[:3]))
        for x in repaired:
            log(f"  repaired {x['task']} {x['step'] or '-'} {x['code']}: {x['said']}")
        for x in refused:
            log(f"  refused  {x['task']} {x['step'] or '-'} {x['code']}: {x['why']}")
        for f in findings:
            log(f"  left     {f['task']} {f['step'] or '-'} {f['code']}: {f['what']}")
        if job.dry_run:
            log(f"consistency_check: dry run, would repair {len(repaired)}, {len(findings)} left; nothing written")
            return 0
        deliver(findings, errors, today, now, repaired, refused)
        log(f"consistency_check: repaired {len(repaired)}, {len(findings)} left in {tasks} task(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
