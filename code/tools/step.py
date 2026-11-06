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

<task> is the note's name (Inbox/<task>.md) or its task_id (t-xxxxxx). Exit 1 with a reason when
the note or the open step is not found.
"""
from __future__ import annotations

import argparse
import datetime as dt
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vt step")
    ap.add_argument("op", choices=["list", "done", "discard"])
    ap.add_argument("task")
    ap.add_argument("sid", nargs="?")
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
    hit = next((st for st in steps if st.id == a.sid and st.state == "open"), None)
    if not a.sid or hit is None:
        print(f"vt step: no open step {a.sid!r} in {p.stem}; run `vt step list {a.task}`", file=sys.stderr)
        return 1
    today = dt.datetime.now(TZ).date()
    label = _label(today)
    from jobs.checkin_app.adapters import notes

    def change(text):
        """(the note after the change, what to print), or None when the step is gone."""
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
