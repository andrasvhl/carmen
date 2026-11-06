"""Tasks, the step contract (Tasks v2, decided with the owner Nov 5, tested against 40 real open steps).

A good step, in seven rules:
  1. One action, one sitting, 60 min or less; an "and" joining two different actions means split.
  2. A short verb plus object, naming who or where when relevant, 15 words at most; links and
     context go in the workspace lines under it.
  3. Known (an estimate) or not known yet (a timebox); message and wait as today.
  4. Ready to start now; a dependency is a wait step or a Then with "after: ...", never the Next.
  5. His by default; Claude's steps start with "Claude:" and do not count against his time.
  6. Estimated including friction, at least 10 min except a message; deadlines as a date, never
     "ASAP".
  7. Done is clear: implicit for a message (sent), a wait (answer seen) and a timeboxed step
     (its findings written as steps).
  Events are steps too: an appointment step names its calendar event, `(ev-xxxxxxxx)`, is exempt
  from the 60 minutes (its length is the event's) and needs no estimate. Not a step: a verdict
  such as "Mark as done".

`check` judges one step and returns what breaks the contract, as short reasons; the card shows
"unclear step · sharpen?" when there is any. `on_write` is the one function every writer goes
through (tools/steps.write: Carmen's taps, the pre-run sweep, prep, a chat turn, `vt step`): it
records a failing step in the step history and runs the estimator (Tasks v2 slice 2). Pure
judgment of words: nothing here calls a model.
"""
from __future__ import annotations

import re

from tools import steps as S

from . import steps as stepmod

MAX_MIN = 60
MAX_WORDS = 15
FLOOR = 10

RULES = """\
A step is one action for one sitting, 60 min or less: a short verb plus object, naming who or where
when it matters, 15 words at most (links and context go in the lines under it). Never two actions
joined by "and" or "then": write a second Then line. Each step is known, with an estimate that
counts friction (logins, portals, forms, uploads, payments), at least 10 min except a single
message, or not known yet, with `(timebox 15 min)`, `(timebox 30 min)` or `(timebox 45 min)`
instead of an estimate (30 by default). When the steps are not known, the Next is one timeboxed
finding-out step; never guess a chain. The Next is ready to start now: a dependency is a
`Waiting:` line or a Then with "after: ...". Claude's own steps start with "Claude:". Deadlines are
a date, never "ASAP". Not a step: verdicts such as "Mark as done"."""

# Imperative verbs a step may start with. A first word outside this list is not an error by
# itself only when it is unknown; the words below it are never a step's start.
VERBS = set("""
accept add agree answer apply approve archive arrange ask attach back book bring buy call cancel
change charge chase check choose claim clean clear close collect comment compare complete confirm
contact copy create decide delete describe dispute do download draft drop email enable enter
export file fill find finish fix follow forward get give go install invite join list log look mail
make mark meet message move note notify offer open order organise organize outline pack pay pick
ping plan post prepare price print propose put read rebook record redo register reject remind
remove renew reply report request reserve reschedule resend respond return review run save
schedule search see select sell send set share sign skim sketch solve sort start submit swap take
talk tell test text thank tick top transfer try turn unsubscribe update upload use verify visit
wait watch whatsapp withdraw write zelle venmo rsvp dm nudge refund block hang ship drive walk
attend do prep reread summarise summarize translate draft edit rewrite outline estimate measure
""".split())
NOT_A_START = set("""
the a an maybe need needs should ideally possibly probably every each some any this that it its
mon tue wed thu fri sat sun monday tuesday wednesday thursday friday saturday sunday jan feb mar
apr may jun jul aug sep oct nov dec today tomorrow tonight in on at for with if once when while
""".split())
# Second verbs that stay inside one message: "Reply to Pal and offer a call time" is one message.
IN_MESSAGE = {"ask", "offer", "propose", "tell", "say", "suggest", "mention", "confirm", "thank", "explain", "share", "attach"}
VAGUE = re.compile(r"^(?:work (?:on|through)|look into|think about|deal with|handle|figure out|explore|research|"
                   r"investigate|sort out|check out|go through|dig into|play with|clarify)\b", re.I)
VERDICT = re.compile(r"^(?:mark (?:it |this )?(?:as )?done|close (?:the |this )?task|done|nothing (?:left|to do))\b\.?$", re.I)
ASAP = re.compile(r"\bASAP\b|\burgent(?:ly)?\b|\bimmediately\b", re.I)
DEPEND = re.compile(r"\b(?:once|as soon as|after (?:she|he|they|it|we|the)\b|when (?:she|he|they|it|the)\b[^,.]*\b"
                    r"(?:answer|answers|reply|replies|respond|responds|send|sends|arrive|arrives|confirm|confirms)|"
                    r"if (?:not|no|she|he|they)\b)", re.I)
AFTER = re.compile(r"\bafter:\s*\S", re.I)
CLAUDE_DOES = re.compile(r"^(?:have|let|get|ask) Claude\b|\bClaude (?:will|re)?(?:build|draft|write|check|find|book|send|"
                         r"price|research|prepare|look|summari[sz]e|compare|do|make|update)s?\b(?!:)")
JOIN = re.compile(r"(?:,?\s+and(?: then)?|,\s*then|;\s*(?:then\s+)?)\s+([A-Za-z]+)\b")
# A German step keeps its verb at the end (Nov 6, his feedback: "In german verbs can be at the end
# of a sentence. It should check that a verb is in the sentence in general").
GERMAN = re.compile(r"\b(?:und|oder|aus|den|der|die|das|dem|des|von|mit|für|fuer|nach|bei|zum|zur|ein|eine|einen|"
                    r"auf|ist|sind|nicht|für|über|im)\b", re.I)
GERMAN_VERB = re.compile(r"^[a-zäöüß]+(?:en|ern|eln)$")
# A step that only makes sense with context (Nov 6, his feedback on "the two drafts" and "the lean
# plan": "without context and just reading this on the do page, this is very hard to understand").
# Detail in the step's own line (round 2, his feedback: "the substep still reads like links, which is
# very annoying"; "the due date should not be in the title of the substep").
DETAIL = re.compile(r"\[\[|\]\(|https?://|\b(?:by|before|until|due)\b[^,;()]{0,24}?\b(?:jan|feb|mar|apr|may|jun|jul|aug|"
                    r"sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.I)
# What makes a long step cluttered rather than just long (round 2: his Rights at 16 to 29 words all
# carried parentheses, amounts, links or a colon list; his Wrongs at 16 to 21 were plain sentences).
CLUTTER = re.compile(r"\(|\[|:\s|\$\d|\d+(?:[.,]\d+)?\s*(?:%|l\b|kg|km|eur|usd)|(?:,[^,]+){2,}", re.I)
MAX_WORDS_PLAIN = 25
CONTEXT = re.compile(r"\b(?:the|these|those)\s+(?:(?:two|three|four|\d+)\s+)?(?:\w+\s+)?"
                     r"(?:drafts?|lists?|plans?|docs?|documents?|files?|items?|options?|points?|changes?|edits?|"
                     r"versions?|ones?|stuff|things?)(?![-\w])(?!\s*:)|\b(?:confirm|approve|review|check|send|finali[sz]e)\s+(?:them|those|these)\b", re.I)
# Not context-dependent: "the first item: order Euro flags" names it after the colon, and
# "the plan-today delta" is a name (Nov 6, the two false hits of 5 on his 116 open Next steps).
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)|\[\[([^\]|]*)(?:\|([^\]]*))?\]\]|https?://\S+")


def _words(text: str) -> list[str]:
    t = LINK.sub(lambda m: m.group(3) or m.group(2) or m.group(1) or "link", text or "")
    t = re.sub(r"[*_`]", "", t)
    return [w for w in re.split(r"\s+", t.strip()) if re.search(r"\w", w)]


def is_claude(text: str) -> bool:
    return bool(re.match(r"^\s*Claude:", text or ""))


ALWAYS_MSG = re.compile(r"^(?:reply|answer|respond|text|whatsapp|dm|message|email|e-mail|ping|nudge|chase)\b", re.I)


def is_message(text: str) -> bool:
    """A single message: a reply or text-type verb, or a message verb with a named person ("Send
    Mathias the offer"). "Write the clearance summary" and "Send the forms to MyHealth" are work."""
    t = re.sub(r"^\s*Claude:\s*", "", text or "")
    return bool(ALWAYS_MSG.match(t) or (stepmod.MSG_VERB.match(t) and stepmod.who_of(t)))


def _first_verb(words: list[str]) -> str:
    w = (words[0] if words else "").lower().strip(".,:;!?\"'()")
    return w


WHY = {
    "verdict": "not a step: a verdict, close the task instead",
    "verb": "start with a verb",
    "two": "two actions: split it",
    "words": f"over {MAX_WORDS} words: details go under it",
    "long": "over 60 min: split it",
    "box": "timebox over 60 min",
    "estimate": "no estimate",
    "floor": f"under {FLOOR} min: count the friction",
    "ready": "not ready: make it a wait or a later step",
    "after": 'a dependency: write "after: ..."',
    "claude": 'Claude\'s step: start it with "Claude:"',
    "asap": "a date, not ASAP",
    "done": "done unclear: timebox it or name the result",
    "context": "needs context: name what it refers to",
    "detail": "a link or date in the step: move it under",
}


def check(step: S.Step) -> list[str]:
    """The rules one open step breaks, as codes (keys of WHY; [] when it is a good step). Done,
    struck and Waiting lines are not judged: a wait ends on evidence."""
    if step is None or step.state != "open" or step.kind == "Waiting":
        return []
    text = (step.text or "").strip()
    body = re.sub(r"^Claude:\s*", "", text)
    words = _words(body)
    if not words:
        return ["verb"]
    if VERDICT.match(body):
        return ["verdict"]
    out = []
    core = re.sub(r"^[\W_]*(?:ASAP[\W_]*)?", "", body)   # "**ASAP** text Fede": the verb is "text"
    first = _first_verb(_words(core))
    msg = is_message(core)
    german = bool(GERMAN.search(core)) and any(GERMAN_VERB.match(w.lower().strip(".,;:!?()")) for w in _words(core)[-3:])
    if first not in VERBS and not german and (first in NOT_A_START or re.search(r"\d", first) or first.endswith("ing")
                                             or (first.endswith("ed") and len(first) > 4)):
        out.append("verb")
    for m in JOIN.finditer(body):
        v = m.group(1).lower()
        if re.search(r",\s*[\w'-]+$", body[:m.start()]):   # "model, size and price": a list, one action
            continue
        if v in VERBS and not (msg and v in IN_MESSAGE):
            out.append("two")
            break
    if len(words) > MAX_WORDS_PLAIN or (len(words) > MAX_WORDS and CLUTTER.search(body)):
        out.append("words")
    if not step.event:
        mins = step.minutes
        if mins is not None and mins > MAX_MIN:
            out.append("long")
        if step.box and step.box > MAX_MIN:
            out.append("box")
        if not is_claude(text):
            if mins is None and not step.box:
                out.append("estimate")
            elif mins is not None and mins < FLOOR and not msg:
                out.append("floor")
    if step.kind == "Next" and (DEPEND.search(body) or AFTER.search(body)):
        out.append("ready")
    elif step.kind != "Next" and DEPEND.search(body) and not AFTER.search(body):
        out.append("after")
    if CLAUDE_DOES.search(core) and not is_claude(text):
        out.append("claude")
    if ASAP.search(body):
        out.append("asap")
    if CONTEXT.search(LINK.sub(" ", body)) and not LINK.search(body):
        out.append("context")
    if VAGUE.match(body) and not step.box:
        out.append("done")
    if DETAIL.search(body):
        out.append("detail")
    # Two actions alone are one sitting to him (round 2: wrong 4 of 5, "Open HW2 and note the due
    # date", "Pick a weekend and ask two friends"); they count with another problem only.
    if "two" in out and not [c for c in out if c not in ("two", "estimate", "floor")]:
        out.remove("two")
    return out


def reasons(step: S.Step) -> list[str]:
    return [WHY[c] for c in check(step)]


def check_line(line: str) -> list[str]:
    return check(S.parse_line(line))


def _skip() -> tuple:
    """Codes about estimates count only while the estimator is on: with it off nearly every step
    has none, and "no estimate" or "under 10 min" alone is noise he cannot act on (Nov 6, his
    review list: "Choose Alice's Hike or the Black Mountain run (2 min)" flagged for its 2 min)."""
    from ..adapters import flags
    return () if flags.on("estimator") else ("estimate", "floor")


def unclear(body: str) -> dict[str, list[str]]:
    """{step id: reasons} for every open step of a note body that breaks the contract."""
    return {sid: [x["title"] for x in xs] for sid, xs in unclear_explained(body).items()}


def unclear_explained(body: str) -> dict[str, list[dict]]:
    """{step id: [{code, title, why}]}: explain() for every open step that breaks the contract."""
    skip = _skip()
    out = {}
    for s in S.steps_of(body):
        r = [x for x in explain(s) if x["code"] not in skip] if s.id else []
        if r:
            out[s.id] = r
    return out


def _quote(text: str, start: int, end: int, pad: int = 18) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def explain(step: S.Step) -> list[dict]:
    """Why a step breaks the contract, in words that quote it (Nov 6, the owner: "I don't understand
    why something was suggested ... which criteria were not met, and why exactly"):
    [{code, title, why}], one per code check() returns."""
    out = []
    text = (step.text or "").strip() if step else ""
    body = re.sub(r"^Claude:\s*", "", text)
    words = _words(body)
    mins = step.minutes if step else None
    for c in check(step):
        title, why = WHY[c], ""
        if c == "verdict":
            title, why = "Not a step", f'"{body}" is a verdict, not an action. Close the task instead.'
        elif c == "verb":
            first = _first_verb(_words(re.sub(r"^[\W_]*(?:ASAP[\W_]*)?", "", body))) or "nothing"
            title, why = "Doesn't start with a verb", f'It starts with "{first}". Start with what you do, like "Book" or "Send".'
        elif c == "two":
            m = next((m for m in JOIN.finditer(body) if m.group(1).lower() in VERBS), None)
            q = _quote(body, m.start(), m.end()) if m else body[:60]
            title, why = "Two actions", f'"{q}" starts a second action ("{m.group(1) if m else "and"}"). Split it into two lines, or call this wrong if it is one sitting.'
        elif c == "words":
            title, why = (f"{len(words)} words with detail in the line" if len(words) <= MAX_WORDS_PLAIN else f"{len(words)} words, the limit is {MAX_WORDS_PLAIN}"), "Keep the action, move the details under it."
        elif c == "long":
            title, why = f"{mins} min, the limit is {MAX_MIN}", "Split it into parts of an hour or less."
        elif c == "box":
            title, why = f"Timebox of {step.box} min, over {MAX_MIN}", "Timebox 15, 30 or 45 min, then write what you found as steps."
        elif c == "estimate":
            title, why = "No estimate", "Say how long it takes, friction included."
        elif c == "floor":
            title, why = f"Says {mins} min", f"Steps under {FLOOR} min often hide a login, a form or an upload. Wrong if it really is that quick."
        elif c in ("ready", "after"):
            m = DEPEND.search(body) or AFTER.search(body)
            q = _quote(body, m.start(), m.end(), 24) if m else body[:60]
            if c == "ready":
                title, why = "Depends on something", f'"{q}" waits on something first. The Next should be ready now: make it a Waiting line or a later step.'
            else:
                title, why = "Dependency not marked", f'"{q}" waits on something. Write "after: ..." so it shows.'
        elif c == "claude":
            title, why = "Reads like Claude's work", 'Start it with "Claude:" so it does not count against your time.'
        elif c == "asap":
            m = ASAP.search(body)
            title, why = "Urgency, no date", f'Says "{m.group(0) if m else "ASAP"}". Give a date instead.'
        elif c == "detail":
            m = DETAIL.search(body)
            q = m.group(0) if m else ""
            title, why = "Link or date in the step", (f'"{q}…" belongs under the step: a link as a line below it, a date as its due date.'
                                                       if q and not q.startswith(("[[", "](", "http")) else
                                                       "The link belongs on a line under the step, so the step reads as plain words.")
        elif c == "context":
            m = CONTEXT.search(body)
            q = m.group(0) if m else "it"
            title, why = "Needs context", f'"{q}" only makes sense if you remember which. Name it, or link it.'
        elif c == "done":
            m = VAGUE.match(body)
            title, why = "Open-ended", f'"{m.group(0) if m else body[:30]}" has no clear end. Timebox it or name the result.'
        out.append({"code": c, "title": title, "why": why})
    return out


def on_write(prev: list[dict] | None, text: str, source: str, today=None, stem: str = "") -> tuple[str, list[dict]]:
    """The one function every writer goes through (tools/steps.write and sweep call it after
    normalize, before the note is saved). prev: the steps as last seen (the snapshot), None for a
    note never seen. Returns the text to save and extra history events: the contract verdict of
    every step this write added or reworded that breaks it ("unclear"), and what the estimator
    changed. Never raises: a write is never refused for its words."""
    from ..adapters import flags
    extra: list[dict] = []
    try:
        head, sep, body = text.partition("\n---\n")
        if not sep:
            return text, extra
        if flags.on("estimator") or flags.on("timebox"):
            from . import estimates
            text, ev = estimates.on_write(prev, text, source, today, stem=stem)
            extra += ev
            head, sep, body = text.partition("\n---\n")
        if flags.on("contract"):
            seen = {(p.get("id"), S._key(p.get("text", ""))) for p in prev or []}
            ids_seen = {p.get("id") for p in prev or [] if p.get("id")}
            for s in S.steps_of(body, today):
                if s.state != "open" or (s.id in ids_seen and (s.id, S._key(s.text)) in seen):
                    continue
                r = check(s)
                if r:
                    extra.append({"step": s.id, "text": s.text, "event": "unclear", "rules": r})
        if flags.on("timebox"):
            extra += _found(prev, text, stem, today)
    except Exception:   # noqa: BLE001 - the contract is a layer on top of the write
        pass
    return text, extra


def _found(prev: list[dict] | None, text: str, stem: str, today=None) -> list[dict]:
    """Steps written while a timeboxed finding-out step was open carry found_by: that step. They
    never join today's chunk by themselves (Planning reads found_by)."""
    boxes = [x for x in prev or [] if x.get("state") == "open" and x.get("box") and x.get("id")]
    if not boxes:
        return []
    by = boxes[0]["id"]
    known = {x.get("id") for x in prev if x.get("id")} | {S._key(x.get("text", "")) for x in prev}
    head, _, body = text.partition("\n---\n")
    new = [s for s in S.steps_of(body, today)
           if s.id and s.id != by and s.kind != "Waiting" and s.id not in known and S._key(s.text) not in known]
    if not new:
        return []
    S.mark_found(S._task_key(text, stem), {s.id: by for s in new})
    return [{"step": s.id, "text": s.text, "event": "found", "found_by": by} for s in new]
