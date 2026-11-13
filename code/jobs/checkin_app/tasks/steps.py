"""Done on a Do card means the step is done, not always the task (owner decision, Nov 3: "the main next
step basically was completed there already"). What comes next is read from the note:

  - the step was a message to someone that asks for an answer (Ask, Offer, Chase, or "ask",
    "confirm", "?" in it; Nov 4): the answer is theirs, so the task waits on them: `waiting: true`
    and a Waiting line, and Do moves it into the Waiting fold until the wait ends. A message that
    only tells (Text Chris that ..., Send Nora the receipts) never waits;
  - else the next open step line (a Then: line, or a plain checkbox) becomes the Next step;
  - else nothing is left: the task is done.

Undo restores the note as it was; Not waiting takes the wait back out. Pure text in, text out:
the app writes the files and keeps the undo copy.
"""
from __future__ import annotations

import datetime as dt
import re

from tools import steps as S   # the line format: fields, IDs, history (Nov 4)

# QA Nov 8 item 7: "Propose Sat Nov 14 19:00 in The Corner" was sent and never ticked, so the
# same invite came back on Replies after midnight. Proposing, suggesting, inviting, telling,
# sharing and letting someone know are messages too.
MSG_VERB = re.compile(r"^(?:reply|answer|respond|email|e-mail|mail|whatsapp|text|message|ask|offer|"
                      r"propose|suggest|invite|tell|share|let|confirm with|"
                      r"send|write|ping|dm|nudge|chase|remind|follow up with)\b", re.I)
_NAME = r"[A-ZÀ-Ý][\w'’.-]*(?:\s+[A-ZÀ-Ý][\w'’.-]*)*(?:\s*\([^)]*\))?"
WHO = re.compile(rf"^(?:follow up with|[\w-]+)\s+(?:back\s+)?(?:to\s+)?({_NAME}(?:\s*(?:,|and|&)\s*{_NAME})*)")
OPEN = re.compile(r"^(\s*)- \[ \] (?:(Next|Then|Waiting): )?(.*)$")
MONTHS = S.MON   # the one month list (Nov 11)


# A message waits on its answer only when it asks for one (owner decision, Nov 4: Chris's "Text Chris that
# you're going to Nepal instead of China" went to Waiting: "it was more just to tell him"). Telling,
# sending a receipt or thanking is done once sent; asking, offering or chasing waits.
ASK_VERB = re.compile(r"^(?:ask|offer|invite|propose|request|chase|nudge|remind|follow up with|check with)\b", re.I)
ASK_WORD = re.compile(r"\?|\b(?:ask|asking|asks|what time|if (?:he|she|they|it)|confirm|"
                      r"find out|check (?:if|whether|with)|propose|proposing|offer|offering|invite|inviting|"
                      r"request|requesting|let me know|get back|agree on|pick a|for (?:a|the) (?:time|date|slot)|"
                      r"to (?:approve|sign|decide|choose)|for (?:his|her|their) (?:ok|okay|approval|answer|reply|input))\b", re.I)


def _words(step: str) -> str:
    """A step's words: no estimate, no dates, no ID (tools/steps.py)."""
    return S.clean(step or "")


def asks(step: str) -> bool:
    """Whether a message step asks for an answer, so the task should wait on it."""
    s = _words(step)
    return bool(ASK_VERB.match(s) or ASK_WORD.search(s))


def who_of(step: str) -> str:
    """Whom a message step goes to, or "" when the step is not a message to a named person."""
    s = _words(step)
    if not MSG_VERB.match(s):
        return ""
    m = WHO.match(s)
    who = m.group(1).strip(" ,") if m else ""
    if who.split(" ")[0] in _SKIP:   # "Propose Sat Nov 14 ... in The Corner": a date, not a person
        m = IN_CHAT.search(s)
        who = m.group(1).strip(" ,") if m else ""
    return who


IN_CHAT = re.compile(rf"\b(?:in|to|with)\s+({_NAME})\s*$")
# A two-part step whose first part is a message ("Reply to Hana and put Sun Nov 8 19:00 in the
# calendar", QA Nov 8: Sent ticked it all and the calendar half was lost). The second part starts
# with a verb of work, lower case, so "Text Eli and Rosa" stays one message.
WORK_VERB = (r"(?:put|add|book|block|schedule|set|create|pay|buy|order|update|move|cancel|file|save|log|"
             r"call|go|pick|print|sign|submit|fill|upload|check|review|prepare|plan|note|make|get|bring|"
             r"enter|record|split|transfer|reserve)")
COMPOUND = re.compile(rf"^(.+?),?\s+(?:and then|and|then)\s+({WORK_VERB}\b.*)$")


def split_message(step: str) -> tuple[str, str] | None:
    """(the message part, the rest as a step) of a two-part step that starts with a message, or None."""
    s = _words(step)
    if not MSG_VERB.match(s):
        return None
    m = COMPOUND.match(s)
    if not m:
        return None
    rest = m.group(2).strip()
    return m.group(1).strip(" ,"), rest[:1].upper() + rest[1:]


def _split(text: str) -> tuple[str, str]:
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError("this note has no frontmatter, fix it in Obsidian")
    head, _, body = text.partition("\n---\n")
    return head, body


def _set(head: str, key: str, val: str | None) -> str:
    lines = [l for l in head.split("\n") if not l.startswith(f"{key}:")]
    if val is not None:
        lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _block(lines: list[str]) -> tuple[int, int] | None:
    for i, l in enumerate(lines):
        if l.strip() == "## Next action":
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
            return i, end
    return None


def _log(body: str, line: str) -> str:
    body = body.rstrip()
    if re.search(r"^## Log\s*$", body, re.M):
        return f"{body}\n{line}\n"
    return f"{body}\n\n## Log\n{line}\n"


# D-13 D-6
def _promote(lines: list[str], a: int, b: int) -> str:
    """The first open Then: line, else the first plain open step, becomes Next. Its text, or "".
    An optional step never becomes the Next on its own (Nov 11): with only optional steps left
    nothing comes up, so the task closes and they are struck "not needed" (tools/steps.normalize)."""
    for kind in ("Then", None):
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == kind and m.group(3).strip() and not (S.parse_line(lines[i]) or S.Step("open")).optional:
                lines[i] = f"{m.group(1)}- [ ] Next: {m.group(3).strip()}"   # fields and ID move with it
                return S.parse_line(lines[i]).display()
    return ""


_SKIP = {"I", *S.WEEKDAYS_RX.split("|"), *S.MON, "WhatsApp", "Gmail", "Claude"}


def guess_who(step: str) -> str:
    """A name to prefill "Waiting on": whom a message goes to, else the first capitalised
    word after the verb ("Open Sam's chat" gives Sam). Only a prefill; he confirms it."""
    w = who_of(step)
    if w:
        return w
    for m in re.finditer(r"(?<=\s)([A-ZÀ-Ý][\w-]*?)(?:'s|’s)?\b", _words(step)):
        if m.group(1) not in _SKIP:
            return m.group(1)
    return ""


FOR = " · for: "   # a step in its waiting state: "<who / until> · for: <the step>" (Nov 5)


def _wait_flag(head: str, lines: list[str]) -> str:
    """`waiting:` as a cache of the one rule (Nov 10, tools/steps.task_waiting): true only when a
    Waiting line is open and no unblocked step of his is."""
    return _set(head, "waiting", "true" if S.task_waiting(S.steps_of("\n".join(lines))) else None)


def _link_wait(lines: list[str], a: int, b: int, w: int, who: str, what: str) -> int:
    """The Waiting line at w gets an id, and every later open step whose words need its answer
    (S.needs_wait) gets `(after: ^id)`. Returns how many steps it linked."""
    wl = S.parse_line(lines[w])
    if not wl:
        return 0
    taken = {x.id for x in (S.parse_line(lines[i]) for i in range(a + 1, b)) if x and x.id}
    if not wl.id:
        wl.id = S._new_id(taken)
        lines[w] = S.format_line(wl)
    n = 0
    for i in range(w + 1, b):
        x = S.parse_line(lines[i])
        if x and x.state == "open" and x.kind != "Waiting" and wl.id not in x.after and S.needs_wait(x.text, who, what):
            x.after = x.after + [wl.id]
            lines[i] = S.format_line(x)
            n += 1
    return n


def set_waiting(text: str, who: str, label: str) -> str:
    """Waiting is a state of a step, not of the task (owner decision, Nov 5: "the wait is tied to a
    substep"): the open Next becomes a Waiting line that keeps its text, fields and id
    ("Dan, Omar, Jade · for: Get the squash money back"), and his next own step comes up, so
    the task leaves for the Waiting fold only when nothing of his is left. waiting: true."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    if not blk:
        lines[0:0] = ["", "## Next action", ""]
        blk = (1, 3)
    a, b = blk
    done, w, what = False, None, ""
    for i in range(a + 1, b):
        m = OPEN.match(lines[i])
        if m and m.group(2) == "Next":
            stp = S.parse_line(lines[i])
            what = stp.text
            stp.kind, stp.text = "Waiting", f"{who}{FOR}{stp.text}"
            lines[i] = S.format_line(stp)
            done, w = True, i
            break
    if not done:   # no step of his open: the wait itself goes on top
        first = next((i for i in range(a + 1, b) if OPEN.match(lines[i])), a + 1)
        lines.insert(first, f"- [ ] Waiting: {who} ({label})")
        b += 1
        w = first
    linked = _link_wait(lines, a, b, w, who, what)   # the steps that need the answer (Nov 10)
    _promote(lines, a, b)
    head = _wait_flag(head, lines)
    body = _log("\n".join(lines), f"- {label} · waiting on {who} · check-in app")
    if linked:
        body = _log(body, f"- {label} · {linked} step{'s' if linked > 1 else ''} wait for {who} · check-in app")
    return head + "\n---\n" + body


def step_done(text: str, today: dt.date, label: str, who: str | None = None,
              wait_over: bool = False, to: str = "") -> tuple[str, dict]:
    """The note after Done on its card, and {"state": waiting|next|done, "who", "step", "next",
    "guess"}. who, when given, is his answer to "Waiting instead": wait on that person.
    wait_over: the card said "Wait over", so Done ends the wait (ticks the Waiting line) and the
    next open step comes up, or the task closes when none is left (Nov 4, Anna's dinner).
    to: whom the message went, when the step names nobody ("Propose ... in the group chat")."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    nxt = None
    if blk:
        for i in range(blk[0] + 1, blk[1]):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Next":
                nxt = (i, m)
                break
    wait = None
    if blk and not nxt:
        wait = next((m for m in (OPEN.match(lines[i]) for i in range(blk[0] + 1, blk[1]))
                     if m and m.group(2) == "Waiting"), None)
    if wait and wait_over:
        a, b = blk
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Waiting":
                lines[i] = f"{m.group(1)}- [x] Waiting: {m.group(3)}"
                break
        head = _set(head, "waiting", None)
        who = re.split(r",\s*(?:on|to)\b", _words(wait.group(3)), maxsplit=1)[0].strip()
        new = _promote(lines, a, b)
        if new:
            return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · wait over ({who}), next: {new} · check-in app"),
                    {"state": "next", "step": "", "next": new, "guess": guess_who(new)})
        head = S.close_head(head, today)
        return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · wait over ({who}) · done · check-in app"),
                {"state": "done", "step": ""})
    if wait:      # no step of his own, already waiting: Done never closes it by accident
        head = _wait_flag(head, lines)
        who = re.split(r",\s*(?:on|to)\b", _words(wait.group(3)), maxsplit=1)[0].strip()
        return head + "\n---\n" + body, {"state": "waiting", "who": who, "step": ""}
    if not nxt:   # nothing to tick and nothing waited on: the card's Done closes the task
        head = S.close_head(head, today)
        return head + "\n---\n" + _log(body, f"- {label} · done · check-in app"), {"state": "done", "step": ""}
    i, m = nxt
    step = _words(m.group(3))
    lines[i] = f"{m.group(1)}- [x] Next: {m.group(3)}"   # its ✅ date is added when the note is written
    a, b = blk
    forced = who is not None
    who = who.strip() if forced else ((who_of(step) or to) if asks(step) else "")
    if who:
        line = (f"{who} ({label}, after: {step})" if forced
                else f"{who}, to reply to your message ({label}: {step})")
        lines.insert(i + 1, f"{m.group(1)}- [ ] Waiting: {line}")
        linked = _link_wait(lines, a, b + 1, i + 1, who, step)   # the steps that need the answer (Nov 10)
        new = _promote(lines, a, b + 1)   # Nov 5: the wait is the step's; his next step comes up
        head = _wait_flag(head, lines)
        out = ({"state": "next", "step": step, "next": new, "who": who, "guess": guess_who(new)} if new
               else {"state": "waiting", "who": who, "step": step})
        note = f"waiting on {who}" + (f", next: {new}" if new else "") + (f", {linked} step{'s' if linked > 1 else ''} wait for the reply" if linked else "")
    else:
        new = _promote(lines, a, b)
        if new:
            out = {"state": "next", "step": step, "next": new, "guess": guess_who(step)}
            note = f"next: {new}"
        else:
            head = S.close_head(head, today)
            out = {"state": "done", "step": step, "guess": guess_who(step)}
            note = "task done"
    body = _log("\n".join(lines), f"- {label} · step done: {step} · {note} · check-in app")
    return head + "\n---\n" + body, out


_ID_END = re.compile(r"\^(s[0-9a-z]+)\s*$")


def _open_lines(text: str, kind: str) -> list[str]:
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    if not blk:
        return []
    return [lines[i] for i in range(blk[0] + 1, blk[1])
            if (m := OPEN.match(lines[i])) and m.group(2) == kind]


def next_id(text: str) -> str:
    """The ^id of the open Next step, or ""."""
    for line in _open_lines(text, "Next")[:1]:
        m = _ID_END.search(line)
        return m.group(1) if m else ""
    return ""


def chunk_done(text: str, today: dt.date, label: str, ids: list[str],
               wait_over: bool = False) -> tuple[str, dict]:
    """Done on a Do card closes today's chunk, not one step (owner decision, Nov 8: "the preferred
    behavior would be to close the entire chunk"). The Next step is ticked, then each step that
    comes up next while it is one of today's (ids), each through step_done, so waits and the close
    work as for one step. A wait one of these steps opened keeps the task open: it waits instead
    of closing. Returns what step_done returns plus "steps", the words of every step ticked."""
    ids = {x for x in ids or [] if x}
    waits0 = len(_open_lines(text, "Waiting"))
    cur, res = step_done(text, today, label, wait_over=wait_over)
    steps = [res["step"]] if res.get("step") else []
    whos = [res["who"]] if res.get("who") else []
    while res.get("state") == "next" and ids and next_id(cur) in ids:
        before = cur
        cur, res = step_done(cur, today, label)
        if res.get("step"):
            steps.append(res["step"])
        if res.get("who"):
            whos.append(res["who"])
        if res.get("state") == "done" and len(_open_lines(before, "Waiting")) > waits0:
            hb, _ = _split(before)
            hc, bc = _split(cur)
            m = re.search(r"^status:\s*(.*)$", hb, re.M)
            hc = _set(S.reopen_head(hc, m.group(1).strip() if m else "open"), "waiting", "true")
            who = whos[-1] if whos else ""
            k = bc.rfind(" · task done · check-in app")
            if k >= 0:
                bc = bc[:k] + f" · waiting on {who} · check-in app" + bc[k + len(" · task done · check-in app"):]
            cur, res = hc + "\n---\n" + bc, {"state": "waiting", "who": who, "step": res.get("step", "")}
    res["steps"] = steps
    return cur, res


def finish_task(text: str, today: dt.date, label: str) -> tuple[str, dict]:
    """Close task (owner decision, Nov 8: "differentiating between actually having done a task / substep
    and just closing them to close the task"): the task closes, and every step still open is
    struck, "(left open when closed, <label>)", never ticked, so the step history and the
    estimates only learn from what he did. Returns {"state": "done", "closed": True, "left"}."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    left = []
    if blk:
        for i in range(blk[0] + 1, blk[1]):
            m = OPEN.match(lines[i])
            if not m or not m.group(3).strip():
                continue
            txt = m.group(3).rstrip()
            idm = _ID_END.search(txt)
            core, tail = (txt[:idm.start()].rstrip(), f" ^{idm.group(1)}") if idm else (txt, "")
            kind = f"{m.group(2)}: " if m.group(2) else ""
            lines[i] = f"{m.group(1)}- ~~{kind}{core}~~ (left open when closed, {label}){tail}"
            left.append(_words(core))
    head = S.close_head(head, today)
    note = f"task closed, {len(left)} step{'s' if len(left) != 1 else ''} left open" if left else "task closed"
    return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · {note} · check-in app"),
            {"state": "done", "closed": True, "left": left})


def _next_line(lines: list[str]):
    blk = _block(lines)
    if blk:
        for i in range(blk[0] + 1, blk[1]):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Next":
                return blk, i
    return blk, None


def message_half_done(text: str, label: str, verb: str = "sent") -> tuple[str, dict] | None:
    """The message part of a two-part Next step sent (or dropped, verb "not replying"): the Next
    keeps its ID and becomes the rest ("Put Sun Nov 8 19:00 in the calendar"); nothing is ticked
    and the task stays open. None when the Next is not such a step."""
    head, body = _split(text)
    lines = body.split("\n")
    blk, i = _next_line(lines)
    if i is None:
        return None
    st = S.parse_line(lines[i])
    half = split_message(st.text) if st else None
    if not half:
        return None
    st.text = half[1]
    lines[i] = S.format_line(st)
    return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · {verb}: {half[0]} · next: {half[1]} · check-in app"),
            {"state": "next", "step": half[0], "next": half[1], "half": True, "guess": guess_who(half[1])})


def not_replying(text: str, today: dt.date, label: str) -> tuple[str, dict] | None:
    """Skip > Not replying (owner decision, Nov 9, QA decision 2): the message step is struck, never ticked,
    "(not replying, <label>)"; the next step comes up, or the task closes as cancelled when nothing
    of it is left open. A two-part step loses only its message part. None when the Next step is not
    a message, so there is nothing to strike."""
    half = message_half_done(text, label, "not replying")
    if half:
        return half
    head, body = _split(text)
    lines = body.split("\n")
    blk, i = _next_line(lines)
    if i is None:
        return None
    st = S.parse_line(lines[i])
    if not st or not MSG_VERB.match(st.text):
        return None
    step = st.text
    st.state, st.note = "struck", f"(not replying, {label})"
    lines[i] = S.format_line(st)
    a, b = blk
    new = _promote(lines, a, b)
    still = new or any(OPEN.match(lines[j]) for j in range(a + 1, b))
    if still:
        out, note = {"state": "next", "step": step, "next": new}, f"not replying: {step}" + (f", next: {new}" if new else "")
    else:
        head = S.close_head(head, today, "cancelled")
        out, note = {"state": "closed", "step": step}, f"not replying: {step} · task closed"
    return head + "\n---\n" + _log("\n".join(lines), f"- {label} · {note} · check-in app"), out


def discard_step(text: str, want: str, label: str) -> tuple[str, dict] | None:
    """The × on a step in the Start panel (owner decision, Nov 4: "a small x on hover on substeps where i
    can discard them"): the open step line struck, never deleted, with "(discarded, <label>)"; a
    discarded Next brings up the next Then or open step. None when no open line says want."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    key = _words(want)
    if not blk or not key:
        return None
    a, b = blk
    for i in range(a + 1, b):
        m = OPEN.match(lines[i])
        if m and _words(m.group(3)) == key:
            kind = m.group(2) or ""
            st = S.parse_line(lines[i])
            st.state, st.note = "struck", f"(discarded, {label})"
            lines[i] = S.format_line(st)
            new = _promote(lines, a, b) if kind == "Next" else ""
            return (head + "\n---\n" + _log("\n".join(lines), f"- {label} · step discarded: {key} · check-in app"),
                    {"kind": kind or "step", "next": new})
    return None


def not_waiting(text: str, label: str) -> str:
    """Take the wait back out: waiting off, the open Waiting line struck, the next step up."""
    head, body = _split(text)
    lines = body.split("\n")
    head = _set(head, "waiting", None)
    blk = _block(lines)
    if blk:
        a, b = blk
        for i in range(a + 1, b):
            m = OPEN.match(lines[i])
            if m and m.group(2) == "Waiting":
                st = S.parse_line(lines[i])
                if FOR in st.text:   # the step itself was waiting: it is his Next again
                    for j in range(a + 1, b):
                        mj = OPEN.match(lines[j])
                        if mj and mj.group(2) == "Next":
                            lines[j] = f"{mj.group(1)}- [ ] Then: {mj.group(3)}"
                    st.kind, st.text = "Next", st.text.split(FOR, 1)[1]
                else:
                    st.state, st.note = "struck", f"(not waiting, {label})"
                lines[i] = S.format_line(st)
                break
        if not any((OPEN.match(lines[j]) or [None, None, None])[2] == "Next" for j in range(a + 1, b)):
            _promote(lines, a, b)
    return head + "\n---\n" + _log("\n".join(lines), f"- {label} · not waiting · check-in app")


def edit_waiting(text: str, new: str, label: str) -> str:
    """His own words for what the task waits on: the open Waiting line, rewritten."""
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    for i in range(blk[0] + 1, blk[1]) if blk else []:
        m = OPEN.match(lines[i])
        if m and m.group(2) == "Waiting":
            st = S.parse_line(lines[i])
            if FOR in st.text:
                new = new.split(FOR, 1)[0] + FOR + st.text.split(FOR, 1)[1]
            if st.text == new:
                return text
            st.text = new
            lines[i] = S.format_line(st)
            return head + "\n---\n" + _log("\n".join(lines), f"- {label} · waiting on: {new} · check-in app")
    return set_waiting(text, new, label)


def waiting_since(text: str, today: dt.date) -> dt.date | None:
    """The day the current wait began: the last Log line that started one."""
    found = None
    for m in re.finditer(r"^- ([A-Z][a-z]{2}) (\d{1,2}) · [^\n]*waiting on ", text, re.M):
        mon = MONTHS.get(m.group(1))
        if not mon:
            continue
        d = dt.date(today.year, mon, int(m.group(2)))
        if d > today:
            d = d.replace(year=today.year - 1)
        found = d
    return found


def switch_box(text: str, sid: str, label: str, box: int | None = None) -> tuple[str, dict] | None:
    """Known or not known yet (Tasks v2, Nov 6): a tap on the step's time switches it. A known
    step gets a timebox (30 min unless box says 15 or 45) instead of its estimate; a timeboxed
    step gets an estimate from the estimator. None when no open step has that id."""
    from . import estimates
    head, body = _split(text)
    lines = body.split("\n")
    blk = _block(lines)
    for i in range(blk[0] + 1, blk[1]) if blk else []:
        st = S.parse_line(lines[i])
        if not st or st.id != sid or st.state != "open" or st.kind == "Waiting":
            continue
        if st.box:
            kind, m, _ = estimates.estimate("", [{"text": st.text}])[0]
            st.est, st.box = estimates.fmt(m if kind == "est" else estimates.known(st.text)), None
            what = {"known": True, "est": st.est}
            line = f"- {label} · step known now: {st.text} ({st.est}) · check-in app"
        else:
            st.box, st.est = (box if box in estimates.BOXES else 30), ""
            what = {"known": False, "box": st.box}
            line = f"- {label} · step not known yet: {st.text} (up to {st.box} min) · check-in app"
        lines[i] = S.format_line(st)
        return head + "\n---\n" + _log("\n".join(lines), line), what
    return None
