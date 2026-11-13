"""`vt step`: change one step of a task note by its ID, the way Carmen's own taps do (Nov 6,
refactor Phase 5).

A chat that edits `## Next action` by hand works, but the step history then sees an unannounced
edit ("outside") and cannot tell a step Claude ticked from one the owner ticked. This command makes
the same change Carmen's Done or × makes, written through tools/steps.write with source `chat`
(or --source), with a Log line in the app's format, so Progress, today's part and the history
all read it.

    vt step list <task>                 the open steps with their IDs
    vt step done <task> <sid>           tick it (the Next step: what comes next follows, as Done)
    vt step discard <task> <sid>        strike it, never delete it
    vt step wait <task> "<who>" --for <sid>   tick the step he sent, add the Waiting line, move to it
    vt step result <task> "<who>, on <what>" --after <sid> --span N [--moves ^sid,^sid]
                                        a result that takes up to N days after his step (D-35)
                                        the steps that waited on that step
    vt step link <task> <sid> --after ^sid[,^sid]|none   what the step waits on (no loops)
    vt step date <task> <sid> YYYY-MM-DD|none            the step's 📅
    vt step reword <task> <sid> "<words>" [--his]        new words, id and fields kept (--his: he
                                        dictated them, his attribution added)
    vt step ws <task> <sid> --add <url|[[Note#Section]]|[[#Section]]> [--label L]   a link under it
    vt step move <task> <sid> --before|--after <sid>     its place in the block
    vt step est <task> <sid> "<N min|N h>"               its estimate (the estimator never overwrites it)
    vt step add <task> "<words>" --due YYYY-MM-DD|none --after ^sid[,^sid]|none [--optional]
                                        a new step at the end of the open ones (Next if none is open;
                                        --optional: a nice-to-have, never the Next on its own);
                                        its estimate comes from the one estimator, as every step's

`add` makes the writer decide (owner decision, Nov 10: "the writer should be forced to make a selection"):
--due and --after are required, and "none" is a choice, not a default. It refuses words that name
a deadline with --due none or a different --due, two actions on one line ("; then", ", then"), and
--after ids that are not open steps or Waiting lines of the note. A --due after the task's own
`due:` is written, with a warning. There is no per-step scheduled date: the task's `scheduled`
and today's chunk place steps.

<task> is the note's name (Inbox/<task>.md) or its task_id (t-xxxxxx). Exit 1 with a reason when
the note or the open step is not found.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path


def _note(task: str) -> Path | None:
    from runner import config
    from tools import taskids
    p = config.INBOX / f"{task}.md"
    if "/" not in task and p.is_file():
        return p
    if task.startswith("t-"):
        stem = taskids.index(config.INBOX).get(task)
        if stem:
            return config.INBOX / f"{stem}.md"
    return None


def _log(text: str, line: str) -> str:
    head, sep, body = text.partition("\n---\n")
    from jobs.checkin_app.adapters.notes import _log as app_log
    return f"{head}{sep}{app_log(body, line)}"


def _err(msg: str) -> int:
    print(f"vt step: {msg}", file=sys.stderr)
    return 1


def _add_checks(a, text: str, today: dt.date):
    """(due, after, est) as the line takes them, or an error string."""
    from tools import steps as S
    words = " ".join((a.sid or "").split())
    if not words:
        return "add needs the step's words"
    if a.due is None or a.after is None:
        return ('add needs both --due YYYY-MM-DD|none and --after ^sid|none: decide each, '
                '"none" included')
    if re.search(r"[;,]\s*(?:then|and then|if not)\b", words, re.I):
        return "one action per line: add the second action as its own step"
    if S.LINE.match(words) or "📅" in words or re.search(r"\^s[0-9a-f]{4}", words) or S.OPTIONAL.search(" " + words):
        return "give the words only; the fields come from --due, --after and --optional"
    due = "" if a.due.lower() == "none" else a.due
    if due:
        try:
            dt.date.fromisoformat(due)
        except ValueError:
            return f"--due {a.due!r} is not YYYY-MM-DD or none"
    named = S.prose_due(words, today)
    if named and not due:
        return f"the words name a deadline ({named}); pass --due {named}, or reword"
    if named and due and named != due:
        return f"the words name {named} but --due is {due}; make them agree"
    after = []
    if a.after.lower() == "none" and not getattr(a, "free", False):
        # A step that takes an earlier step's result, or part N of a series, needs its link (items 8, 10).
        body0 = text.partition("\n---\n")[2]
        cur = [s for s in S.steps_of(body0, today) if s.state == "open" and s.kind != "Waiting" and s.id]
        probe = S.Step(state="open", kind="Then", text=words, id="snew")
        need = S.earlier_needed(cur + [probe]).get("snew")
        if need:
            return (f"it takes an earlier step's result or follows a part: pass --after ^{need}, "
                    "or --free if it really needs nothing before it")
    if a.after.lower() != "none":
        after = [x.strip().lstrip("^") for x in a.after.split(",") if x.strip()]
        body = text.partition("\n---\n")[2]
        ids = {s.id for s in S.steps_of(body, today) if s.state == "open" and s.id}
        bad = [x for x in after if x not in ids]
        if bad:
            return f"--after {', '.join('^' + x for x in bad)}: not an open step or Waiting line here"
    return due, after, ""   # no estimate: the one estimator writes it (Tasks v2, Nov 6)


def _add(text: str, words: str, due: str, after: list, est: str, label: str, source: str,
         today: dt.date, optional: bool = False) -> str | None:
    """The note with the step added after the last open Next/Then (and its sub-lines)."""
    from tools import steps as S
    head, sep, body = text.partition("\n---\n")
    lines = body.split("\n")
    r = S.block_range(lines)
    if not r:
        return None
    parsed = [(i, S.parse_line(lines[i], today)) for i in range(r[0] + 1, r[1])]
    work = [i for i, s in parsed if s and s.state == "open" and s.kind in ("Next", "Then")]
    anchor = work or [i for i, s in parsed if s] or \
        [i for i in range(r[0] + 1, r[1]) if lines[i].startswith("DoD")] or [r[0]]
    j = anchor[-1] + 1
    while j < r[1] and lines[j][:1] in (" ", "\t") and lines[j].strip():
        j += 1
    # An optional step is never the Next on its own (Nov 11): it is a Then even with no Next open.
    kind = "Then" if optional or any(s and s.state == "open" and s.kind == "Next" for _, s in parsed) else "Next"
    lines.insert(j, S.format_line(S.Step(state="open", kind=kind, text=words, est=est, due=due,
                                         after=after, optional=optional)))
    return _log(head + sep + "\n".join(lines), f"- {label} · step added: {words} · {source}")


def _structure(a, p: Path, text: str, S, _label, TZ) -> int:
    """wait, link, date, reword, ws, move: one tools/steps operation each, compare-and-swap, a Log
    line, the step history (source: chat or --source). Refused with the reason, exit 1."""
    from jobs.checkin_app.adapters import notes
    today = dt.datetime.now(TZ).date()
    label = _label(today)
    sid = (a.sid or "").lstrip("^")
    if a.op == "wait":
        if not a.for_:
            return _err("wait needs --for <the step he sent>")
        fn, said = (lambda t: S.op_wait(t, a.sid, a.for_, label, today)), f"waiting on {a.sid}, for ^{a.for_.lstrip('^')}"
    elif a.op == "link":
        if a.after is None:
            return _err("link needs --after ^sid[,^sid] or none")
        ids = [] if a.after.lower() == "none" else [x for x in a.after.split(",") if x.strip()]
        fn, said = (lambda t: S.op_link(t, sid, ids, today)), f"^{sid} after " + (", ".join("^" + x.lstrip("^") for x in ids) or "nothing")
    elif a.op == "date":
        if not a.value:
            return _err("date needs YYYY-MM-DD or none")
        fn, said = (lambda t: S.op_date(t, sid, a.value, today)), f"^{sid} due {a.value}"
    elif a.op == "reword":
        if not a.value:
            return _err('reword needs the new words: vt step reword <task> <sid> "<words>"')
        fn, said = (lambda t: S.op_reword(t, sid, a.value, label if a.his else "", today)), f"^{sid} reworded"
    elif a.op == "est":
        if not a.value:
            return _err('est needs the estimate: vt step est <task> <sid> "30 min" (or "1.5 h")')
        fn, said = (lambda t: S.op_est(t, sid, a.value)), f"^{sid} estimate {a.value}"
    elif a.op == "result":
        if not a.after or not a.span:
            return _err('result needs --after <his step> and --span N (days)')
        mv = [x for x in a.moves.split(",") if x.strip()]
        fn, said = (lambda t: S.op_result(t, a.sid, a.after, a.span, mv, today)), f"result wait after ^{a.after.lstrip('^')}, up to {a.span:g} d"
    elif a.op == "ws":
        if not a.link:
            return _err("ws needs --add <url|[[Note#Section]]>")
        fn, said = (lambda t: S.op_ws(t, sid, a.link, a.label, p.stem)), f"^{sid} link added"
    else:
        target = a.before or a.after
        if not target:
            return _err("move needs --before <sid> or --after <sid>")
        fn, said = (lambda t: S.op_move(t, sid, target.lstrip("^"), "before" if a.before else "after")), f"^{sid} moved"
    try:
        new = fn(text)
    except S.Refused as exc:
        return _err(str(exc))
    line = f"- {label} · step {a.op}: {said} · {a.source}"

    def again(cur):
        try:
            return _log(fn(cur), line)
        except S.Refused:
            notes.refuse()
    try:
        # An estimate he gave (est --his) is his: the estimator never overwrites it (Nov 11).
        notes.write_from(p, text, _log(new, line), again, a.source + ("-his" if a.op == "est" and a.his else ""))
    except notes.NoteChanged as exc:
        return _err(str(exc))
    print(said)
    return 0


# D-11
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vt step")
    ap.add_argument("op", choices=["list", "done", "discard", "add", "wait", "link", "date", "reword", "ws", "move", "est", "result"])
    ap.add_argument("task")
    ap.add_argument("sid", nargs="?", help="the step's ID; for add, its words; for wait, whom")
    ap.add_argument("value", nargs="?", help="date: YYYY-MM-DD|none; reword: the new words")
    ap.add_argument("--for", dest="for_", help="wait: the step he sent")
    ap.add_argument("--before", help="move: the step to go before")
    ap.add_argument("--add", dest="link", help="ws: a URL or [[Note#Section]]")
    ap.add_argument("--label", default="", help="ws: the link's label")
    ap.add_argument("--his", action="store_true", help="reword: words he dictated (his attribution); est: his estimate")
    ap.add_argument("--free", action="store_true", help="add: it really needs no earlier step")
    ap.add_argument("--proof", default="", help="done: his words when he says he did it")
    ap.add_argument("--due", help="add: YYYY-MM-DD or none (required)")
    ap.add_argument("--after", help="add: ^sid[,^sid] or none (required)")
    ap.add_argument("--optional", action="store_true", help="add: a nice-to-have (Nov 11)")
    ap.add_argument("--span", type=float, help="result: up to this many days")
    ap.add_argument("--moves", default="", help="result: the steps that need the result, ^sid[,^sid]")
    ap.add_argument("--source", default="chat")
    a = ap.parse_args(argv)
    from jobs.checkin_app.adapters.clock import TZ, _label
    from jobs.checkin_app.tasks import steps as appsteps
    from jobs.checkin_app.tasks import estimates
    estimates.MODEL = True   # a chat's step change is estimated by the model (Tasks v2)
    from tools import steps as S
    p = _note(a.task)
    if not p:
        print(f"vt step: no task note '{a.task}' in Inbox/", file=sys.stderr)
        return 1
    text = p.read_text(encoding="utf-8")
    body = text.partition("\n---\n")[2]
    steps = S.steps_of(body)
    if a.op == "list":
        for st in steps:
            if st.state == "open":
                print(f"{st.id or '(no id)'}\t{st.kind or '-'}\t{st.display()}")
        return 0
    if a.op in ("wait", "link", "date", "reword", "ws", "move", "est", "result"):
        return _structure(a, p, text, S, _label, TZ)
    if a.op == "add":
        today = dt.datetime.now(TZ).date()
        chk = _add_checks(a, text, today)
        if isinstance(chk, str):
            return _err(chk)
        due, after, est = chk
        words = " ".join(a.sid.split())
        m = re.search(r"^due:\s*(\d{4}-\d{2}-\d{2})", text.partition("\n---\n")[0], re.M)
        if due and m and due > m.group(1):
            print(f"vt step: warning: --due {due} is after the task's due {m.group(1)}", file=sys.stderr)
        label = _label(today)
        from jobs.checkin_app.adapters import notes
        new = _add(text, words, due, after, est, label, a.source, today, a.optional)
        if new is None:
            return _err(f"no `## Next action` block in {p.stem}")

        def again_add(cur):
            n2 = _add(cur, words, due, after, est, label, a.source, today, a.optional)
            if n2 is None:
                notes.refuse()
            return n2
        try:
            notes.write_from(p, text, new, again_add, a.source)
        except notes.NoteChanged as exc:
            return _err(str(exc))
        print(f"added: {words}" + (f" · due {due}" if due else " · no deadline")
              + (" · after " + ", ".join("^" + x for x in after) if after else " · not waiting")
              + (" · optional" if a.optional else ""))
        return 0
    hit = next((st for st in steps if st.id == a.sid and st.state == "open"), None)
    if not a.sid or hit is None:
        print(f"vt step: no open step {a.sid!r} in {p.stem}; run `vt step list {a.task}`", file=sys.stderr)
        return 1
    today = dt.datetime.now(TZ).date()
    label = _label(today)
    from jobs.checkin_app.adapters import notes

    def change(text):
        """(the note after the change, what to print), or None when the step is gone."""
        if a.op == "done" and a.proof:   # his word, on the line, so the app counts the tick as his
            ls = text.split("\n")
            for k, ln in enumerate(ls):
                st0 = S.parse_line(ln)
                if st0 and st0.id == a.sid and st0.state == "open":
                    st0.text += f' (proof: the owner in chat {label}: "{" ".join(a.proof.split())[:200]}")'
                    ls[k] = S.format_line(st0)
                    break
            text = "\n".join(ls)
        hit = next((st for st in S.steps_of(text.partition("\n---\n")[2]) if st.id == a.sid and st.state == "open"), None)
        if hit is None:
            return None
        if a.op == "discard":
            res = appsteps.discard_step(text, hit.display(), label)
            if res is None:
                return None
            return res[0], f"discarded: {hit.text}" + (f"; next: {res[1]['next']}" if res[1].get("next") else "")
        if hit.kind == "Next":   # Done on the Next step: what comes next follows, as his Done does
            new, res = appsteps.step_done(text, today, label)
            return new, f"done: {hit.text}; now {res.get('state')}" + (f": {res.get('next')}" if res.get("next") else "")
        lines = text.split("\n")   # a later step: ticked where it stands, nothing else moves
        for i, ln in enumerate(lines):
            st = S.parse_line(ln)
            if st and st.id == a.sid and st.state == "open":
                st.state = "done"
                lines[i] = S.format_line(st)
                break
        return _log("\n".join(lines), f"- {label} · step ticked: {hit.text} · {a.source}"), f"ticked: {hit.text}"
    r = change(text)
    if r is None:
        print("vt step: that step changed in the note", file=sys.stderr)
        return 1
    said = [r[1]]

    def again(cur):   # the note changed underneath: the same change on what is there now
        r2 = change(cur)
        if r2 is None:
            notes.refuse()
        said[0] = r2[1]
        return r2[0]
    try:   # compare-and-swap, as every task note write (Nov 6)
        notes.write_from(p, text, r[0], again, a.source)
    except notes.NoteChanged as exc:
        print(f"vt step: {exc}", file=sys.stderr)
        return 1
    print(said[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
