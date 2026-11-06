"""Tasks, its writes: every change Carmen makes to a task note (done, next, note, the edit form, move
and schedule, DoD, his step lines, a reply recorded on its step), and the undo and part-tick state
of today's step taps. The tap handlers join here when actions.py is split.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path

from starlette.responses import JSONResponse

from runner import config

from . import cardnotes   # re-exported: Planning and Outbox touch and close card notes through Tasks
from . import read as tasks
from ..adapters import clock
from ..adapters import notes
from ..adapters import state
from ..adapters.clock import TZ
from ..adapters.clock import _label
from ..adapters.notes import _log
from ..adapters.notes import _one_line
from ..adapters.notes import _write
from ..adapters.notes import task_path
from ..adapters.state import _load_json
from ..adapters.state import _save_json
from ..adapters.taplog import log_event
from ..adapters.taplog import signal
from ..direction import read as direction
from ..planning import actions as plan_actions


STEP_UNDO = config.STATE_DIR / "checkin_app" / "step_undo.json"   # today's notes before a step tap

# Ticks on the parts of a Next step in the Start panel (Nov 3). The note keeps the full step as
# written ("the full info per next step in the background should still be saved"); these ticks
# only mark parts of it, and reset when the Next line changes.
SUBTICKS = config.STATE_DIR / "checkin_app" / "subticks.json"


@notes.cas
def _reply_sent(p: Path, row: dict, who: str, via: str, body: str) -> dict:
    """A reply he sent (Nov 5): a message on the task's Next step, never the task closed by it;
    the step is ticked only when it is that message (messages.reply_sent)."""
    from . import cardnotes
    from . import messages
    today = dt.datetime.now(clock.TZ).date()
    text, res = messages.reply_sent(notes.read_text(p), who=who, via=via, body=body,
                                    today=today, label=clock._label(today))
    notes._write(p, text, "check-in app (reply sent)")
    plan_actions.save_override("reply_skip", today, row.get("id") or p.stem)   # out of Replies; the task stays
    if res.get("state") == "done":
        cardnotes.closed(p.stem, "check-in app (reply sent)")
    return res


# --- the DoD and his step lines (Nov 1, SYSTEM.md § Capture note schema, "A task states its
# Definition of Done"). The DoD is the first line of ## Next action; a step line carrying
# "(the owner" is his direction, so everything written here from his taps carries it.


@notes.cas
def write_dod(p: Path, dod: str, confirmed: bool, day: dt.date, src: str = "check-in app") -> None:
    """Set the DoD line: confirmed carries his attribution, a guess is marked as Claude's."""
    dod = notes._one_line(dod).strip()
    if not dod:
        raise ValueError("empty Definition of Done")
    line = f"DoD: {tasks.HIS_TAG.sub('', dod)} (the owner, {clock._label(day)})" if confirmed else f"DoD (Claude's guess): {dod}"
    text = notes.read_text(p)
    a, b = notes._next_block(text)
    block = text[a:b]
    block = tasks.DOD_RE.sub(lambda _: line, block, count=1) if tasks.DOD_RE.search(block) else line + "\n" + block
    notes._with_log(p, text[:a] + block + text[b:],
              f"- {clock._label(day)} · DoD {'set' if confirmed else 'proposed'}: {tasks.HIS_TAG.sub('', dod)} · {src}")


@notes.cas
def apply_prop(p: Path, old: str, new: str, day: dt.date) -> None:
    """He accepted a rewrite the pre-run only proposed for one of his lines: the new line
    carries his attribution, the old one stays, struck, as the pre-run does it."""
    kind, _, old_text = old.partition(": ")
    text = notes.read_text(p)
    a, b = notes._next_block(text)
    lines = text[a:b].split("\n")
    for i, ln in enumerate(lines):
        m = tasks.STEP_OPEN.match(ln)
        if m and (m.group(2) or "") == kind and tasks._norm_step(m.group(3)) == tasks._norm_step(old_text):
            ind = m.group(1)
            new_kind, _, new_text = new.partition(": ") if new else ("", "", "")
            lines[i:i + 1] = ([f"{ind}- [ ] {new_kind or kind}: {tasks.HIS_TAG.sub('', new_text)} (the owner, {clock._label(day)})"] if new else []) + \
                [f"{ind}- ~~{kind}: {m.group(3)}~~ ({'replaced' if new else 'struck'}, accepted in the check-in app, {clock._label(day)})"]
            notes._with_log(p, text[:a] + "\n".join(lines) + text[b:],
                      f"- {clock._label(day)} · accepted Claude's proposal: {new or 'struck ' + old} · check-in app")
            return
    raise ValueError("that step is no longer in the note")


@notes.cas
def save_steps(p: Path, steps: list[str], day: dt.date) -> None:
    """His edit of the planned steps (Next, then the rest): unchanged lines stay as they were,
    changed and new ones carry his attribution, removed ones stay struck."""
    steps = [notes._one_line(x) for x in steps if notes._one_line(x)][:12]
    if not steps:
        raise ValueError("at least a next step is needed")
    text = notes.read_text(p)
    a, b = notes._next_block(text)
    lines = text[a:b].split("\n")
    old = [(i, tasks.STEP_OPEN.match(ln)) for i, ln in enumerate(lines)]
    old = [(i, m) for i, m in old if m and m.group(2) != "Waiting"]
    by_norm = {tasks._norm_step(m.group(3)): m for _, m in old}
    # Unchanged lines first, by their words; then a changed line at the place of an old line nobody
    # kept is that step reworded: it keeps its ID and fields (Nov 6, refactor Phase 2: Edit steps
    # gave a reworded step a new ID, so today's chunk lost it).
    exact = {n: by_norm.pop(tasks._norm_step(st), None) for n, st in enumerate(steps)}
    left = [m for _, m in old if m in by_norm.values()]
    from tools import steps as S
    new_lines = []
    for n, st in enumerate(steps):
        m = exact[n]
        if m:
            body = m.group(3)
        elif n < len(old) and old[n][1] in left:
            m = old[n][1]
            left.remove(m)
            by_norm = {k: v for k, v in by_norm.items() if v is not m}
            was = S.parse_line(f"- [ ] {m.group(2) + ': ' if m.group(2) else ''}{m.group(3)}")
            now = S.parse_line(f"- [ ] {tasks.HIS_TAG.sub('', st)} (the owner, {clock._label(day)})")
            if was and now:
                now.id, now.due = was.id, now.due or was.due
                body = S.format_line(now).split("] ", 1)[1]
                body = re.sub(r"^(?:Next|Then): ", "", body)
            else:
                body = f"{tasks.HIS_TAG.sub('', st)} (the owner, {clock._label(day)})"
        else:
            body = f"{tasks.HIS_TAG.sub('', st)} (the owner, {clock._label(day)})"
        prefix = "Next: " if n == 0 else ("Then: " if m and m.group(2) == "Then" else "")
        new_lines.append(f"- [ ] {prefix}{body}")
    struck = [f"- ~~{m.group(3)}~~ (removed in the check-in app, {clock._label(day)})" for m in by_norm.values()]
    at = old[0][0] if old else next((i + 1 for i, ln in enumerate(lines) if tasks.DOD_RE.match(ln)), 0)
    keep = [ln for i, ln in enumerate(lines) if i not in {j for j, _ in old}]
    at = min(at, len(keep)) - sum(1 for j, _ in old if j < at)
    keep[max(at, 0):max(at, 0)] = new_lines + struck
    notes._with_log(p, text[:a] + "\n".join(keep) + text[b:], f"- {clock._label(day)} · steps edited · check-in app")


def _same_step(old_line: str, words: str) -> str:
    """The Next line rewritten to new words, keeping its ID and, unless the words give their
    own, its deadline: a rewrite is the same step reworded, never a new one (step history)."""
    from tools import steps as S
    old = S.parse_line(old_line)
    new = S.parse_line(f"- [ ] Next: {words}")
    if old and new:
        new.id, new.due = old.id, new.due or old.due
        return S.format_line(new)
    return f"- [ ] Next: {words}"


@notes.cas
def apply(p: Path, action: str, today: dt.date | None = None, text_in: str = "",
          dry: bool = False, base: dt.date | None = None, why: str = "",
          fields: dict | None = None) -> str:
    """done | tomorrow | next | note. Same schema the vt tasks hook enforces:
    completedDate on done, the open Next ticked, and a Log line for today.
    base: the day the ToDo switch shows; "tomorrow" means the day after it.
    dry: compute the message, write nothing (the app's test mode)."""
    today = today or dt.datetime.now(clock.TZ).date()
    base = base or today
    text = notes.read_text(p)
    # Without a frontmatter block the partition below finds none (or a rule in the body), and
    # status: done would land in the text. Refuse instead of guessing (Nov 1).
    if not text.startswith("---\n") or "\n---\n" not in text:
        raise ValueError("this note has no frontmatter, fix it in Obsidian")
    head, sep, body = text.partition("\n---\n")
    note = notes._one_line(text_in)
    if action in ("next", "note") and not note:
        raise ValueError("empty text")
    if action == "next":
        if re.search(r"^- \[ \] Next: .*$", body, re.M):
            body = re.sub(r"^- \[ \] Next: .*$", lambda m: _same_step(m.group(0), note), body,
                          count=1, flags=re.M)
        else:
            body = f"\n## Next action\n- [ ] Next: {note}\n" + body
        body = notes._log(body, f"- {clock._label(today)} · Next set to: {note} · check-in app")
        msg = "Next updated"
    elif action == "note":
        # Verbatim, in his words: the next pre-run and any session read the Log.
        body = notes._log(body, f"- {clock._label(today)} · the owner: \"{note}\" · check-in app")
        msg = "Note saved"
    elif action == "done":
        head = notes._set_fm(notes._set_fm(head, "status", "done"), "completedDate", today.isoformat())
        body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
        body = notes._log(body, f"- {clock._label(today)} · done · check-in app")
        msg = "Done"
    elif action == "sent":
        # Send in Replies (Oct 31): closes the task as Replied does; the log names who got it.
        head = notes._set_fm(notes._set_fm(head, "status", "done"), "completedDate", today.isoformat())
        body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
        body = notes._log(body, f"- {clock._label(today)} · sent mail to {note} · check-in app")
        msg = f"Sent to {note}"
    elif action == "update":
        # The Edit task form (Oct 30): only the fields he changed, each logged old → new.
        f = fields or {}
        before = tasks.read_note(p)
        changes = []
        # Status in the form too (owner decision, Oct 31). done and cancelled close the task the way
        # Done and Shouldn't exist do; reopening clears completedDate.
        st = f.get("status")
        if st in ("inbox", "open", "in-progress", "done", "cancelled") and st != before.get("status"):
            head = notes._set_fm(head, "status", st)
            if st in ("done", "cancelled"):
                head = notes._set_fm(head, "completedDate", today.isoformat())
                if st == "done":
                    body = re.sub(r"^- \[ \] Next:", "- [x] Next:", body, count=1, flags=re.M)
            else:
                head = "\n".join(l for l in head.split("\n") if not l.startswith("completedDate:"))
            changes.append(("status", before.get("status") or "", st))
        if f.get("priority") in direction.PRIO_STEPS and f["priority"] != before["priority"]:
            head = notes._set_fm(head, "priority", f["priority"])
            changes.append(("priority", before["priority"], f["priority"]))
        for key in ("due", "scheduled"):
            if key not in f or f[key] == before[key]:
                continue
            val = str(f[key])[:10]
            if val == "":
                head = "\n".join(l for l in head.split("\n") if not l.startswith(f"{key}:"))
            elif re.match(r"^\d{4}-\d{2}-\d{2}$", val):
                head = notes._set_fm(head, key, val)
            else:
                continue
            changes.append((key, before[key], val))
        if "time_est" in f and str(f["time_est"]).strip() != str(before["time_est"]):
            v = str(f["time_est"]).strip()
            if v.isdigit():
                head = notes._set_fm(head, "timeEstimate", v)
                changes.append(("whole task", before["time_est"], v + " min"))
        if "next" in f or "next_min" in f:
            old = before["next"]
            base_next = notes._one_line(f.get("next", old)) or old
            base_next = re.sub(r"\s*\(\d+\s*min\)\s*$", "", base_next)
            mins = str(f.get("next_min", "")).strip()
            if not mins:
                m = re.search(r"\((\d+)\s*min\)\s*$", old)
                mins = m.group(1) if m else ""
            new = base_next + (f" ({mins} min)" if mins.isdigit() else "")
            if new and new != old:
                if re.search(r"^- \[ \] Next: .*$", body, re.M):
                    body = re.sub(r"^- \[ \] Next: .*$", lambda m: _same_step(m.group(0), new), body, count=1, flags=re.M)
                else:
                    body = f"\n## Next action\n- [ ] Next: {new}\n" + body
                changes.append(("next", old, new))
        if not changes:
            return "Nothing changed"
        def show(v):
            return clock._label(dt.date.fromisoformat(v)) if re.match(r"^\d{4}-\d{2}-\d{2}$", str(v or "")) else v
        body = notes._log(body, f"- {clock._label(today)} · edited: " +
                    "; ".join(f"{k} {show(o) or '–'} → {show(n) or 'cleared'}" for k, o, n in changes) + " · check-in app")
        msg = "Saved"
        if fields is not None:
            fields["_changes"] = changes
    elif action == "lower":
        m = re.search(r"^priority:\s*(\S+)", head, re.M)
        cur = m.group(1) if m else "3-normal"
        new = direction.PRIO_STEPS[max(0, direction.PRIO_STEPS.index(cur) - 1)] if cur in direction.PRIO_STEPS else "2-low"
        head = notes._set_fm(head, "priority", new)
        head = "\n".join(l for l in head.split("\n") if not l.startswith("scheduled:"))
        body = notes._log(body, f"- {clock._label(today)} · removed from the selection: not important, priority {cur} → {new}, date cleared · check-in app")
        msg = f"Removed, priority now {new}"
    elif action == "cancel":
        head = notes._set_fm(notes._set_fm(head, "status", "cancelled"), "completedDate", today.isoformat())
        reason = notes._one_line(text_in) or "shouldn't exist"
        body = notes._log(body, f"- {clock._label(today)} · dropped: {reason} · check-in app")
        msg = "Dropped"
    elif action == "schedule":
        head = notes._set_fm(head, "scheduled", base.isoformat())
        body = notes._log(body, f"- {clock._label(today)} · scheduled for {clock._label(base)} · check-in app")
        msg = f"Added to {clock._label(base)}"
    elif action == "tomorrow":
        tmrw = base + dt.timedelta(days=1)
        head = notes._set_fm(head, "scheduled", tmrw.isoformat())
        body = notes._log(body, f"- {clock._label(today)} · moved to {clock._label(tmrw)} · check-in app")
        msg = f"Moved to {clock._label(tmrw)}"
    else:
        raise ValueError(action)
    if why:   # his optional reason, verbatim (Oct 30)
        body = notes._log(body, f"- {clock._label(today)} · the owner, why: \"{notes._one_line(why)}\" · check-in app")
    if dry:
        return f"Test mode, nothing written: {msg[0].lower()}{msg[1:]}"
    notes._write(p, f"{head}{sep}{body}")
    return msg


def on_add(t):
    body, text_in, dry = t.body, t.text_in, t.dry
    from tools.todos import add_task
    title = _one_line(text_in)
    if not title:
        return JSONResponse({"error": "empty task"}, status_code=400)
    if dry:
        return JSONResponse({"ok": True, "msg": f"Test mode, nothing written: would add \"{title}\""})
    # Added from the pre-selection (Oct 30): dated for that day and in his selection.
    add_day = dt.datetime.now(TZ).date() + dt.timedelta(days=1 if body.get("day") == "tomorrow" else 0)
    path = add_task(title, False, None, add_day.isoformat(), "", None)
    plan_actions.save_override("selected", add_day, Path(path).stem)
    signal("added", None, body.get("day", "today"), source="manual", title_added=title)
    return JSONResponse({"ok": True, "msg": f"Added for {_label(add_day)}"})


def on_step_discard(t):
    body, row_id, text_in, dry = t.body, t.row_id, t.text_in, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    want = _one_line(text_in)
    sid = str(body.get("sid", ""))[:12]
    if sid:   # by its id (refactor step 1): the step's words as the note has them now
        hit = next((x for x in tasks.read_note(p).get("step_states") or [] if x["id"] == sid and x["state"] == "open"), None)
        if hit is None:
            return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
        want = _one_line(hit["text"])
    today_d = dt.datetime.now(TZ).date()
    base = p.read_text(encoding="utf-8")
    res = tasks._stepmod().discard_step(base, want, _label(today_d))
    if res is None:
        return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
    signal("step_discarded", {"id": row_id, "title": row_id}, "today", test=dry, text=want[:300],
           step_kind=res[1]["kind"], next=res[1]["next"][:300])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})

    def again(cur):   # the note changed underneath: the same discard on what is there now
        r = tasks._stepmod().discard_step(cur, want, _label(today_d))
        return r[0] if r else notes.refuse()
    notes.write_from(p, base, res[0], again)
    return JSONResponse({"ok": True, "keep": True, "msg": "", **res[1]})


def on_step_box(t):
    """step_box (Tasks v2, Nov 6): the step's time tapped, known and not known yet switched."""
    from ..adapters import flags
    body, row_id, dry = t.body, t.row_id, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    if not flags.on("timebox"):
        return JSONResponse({"error": "timeboxed steps are switched off"}, status_code=400)
    sid = str(body.get("sid", ""))[:12]
    box = body.get("box") if isinstance(body.get("box"), int) else None
    label = _label(dt.datetime.now(TZ).date())
    base = p.read_text(encoding="utf-8")
    res = tasks._stepmod().switch_box(base, sid, label, box)
    if res is None:
        return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
    signal("step_box", {"id": row_id, "title": row_id}, "today", test=dry, sid=sid, known=res[1]["known"])
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    notes.write_from(p, base, res[0], lambda cur: (tasks._stepmod().switch_box(cur, sid, label, box) or notes.refuse())[0])
    return JSONResponse({"ok": True, "keep": True, "msg": "Estimated: " + res[1]["est"] if res[1]["known"]
                         else f"Not known yet: up to {res[1]['box']} min", **res[1]})


def _started(p: Path, sid: str, via: str) -> bool:
    """The step's start, for its actual time (Tasks v2): the Start tap, or its first part ticked."""
    from tools import steps as S
    from . import estimates
    text = p.read_text(encoding="utf-8")
    st = next((x for x in S.steps_of(text.partition("\n---\n")[2]) if x.id == sid and x.state == "open"), None)
    if st is None:
        return False
    S.append(S._task_key(text, p.stem), p.stem, f"tap:{via}", estimates.started_event(sid, st.text, st.est, st.box, via))
    return True


_refining = {"on": False}


def refine_estimates() -> None:
    """The minute tick: the model's estimates for steps a write gave the rules' estimate (Nov 6:
    rules now, the model later), in a thread of its own so no tap ever waits on it."""
    import threading
    from ..adapters import flags
    from . import estimates
    if not flags.on("estimator") or not estimates.MODEL or _refining["on"] or not estimates.queued():
        return
    _refining["on"] = True

    def run():
        try:
            n = estimates.refine(task_path)
            if n:
                log_event("estimates_refined", n=n)
        except Exception as exc:   # noqa: BLE001 - the queue stays for the next tick
            log_event("error", where="refine_estimates", detail=repr(exc)[:200])
        finally:
            _refining["on"] = False
    threading.Thread(target=run, daemon=True).start()


def reestimate(p: Path, source: str = "day-turn") -> bool:
    """Every remaining open step of a task estimated again by the estimator (slice 6b: a task
    carried to a new day). Compares and swaps; True when the note changed."""
    from . import estimates

    def run(cur):
        return estimates.on_write(None, cur, source, force=True)[0]
    base = p.read_text(encoding="utf-8")
    new = run(base)
    if new == base:
        return False
    notes.write_from(p, base, new, run, source)
    return True


def piece(p: Path) -> list[str]:
    """A fresh proposed piece for today from the note's open steps, Next first (slice 6b)."""
    from . import estimates
    st = tasks.read_note(p)
    opn = [x for x in st.get("step_states") or [] if x["state"] == "open" and x["kind"] != "Waiting"]
    opn.sort(key=lambda x: 0 if x["kind"] == "Next" else 1)
    return estimates.propose_piece(opn)


def on_cleanup(t):
    """cleanup_apply, cleanup_skip (Tasks v2 slice 7): one task's proposed step cleanup.
    cleanup_keep, cleanup_close (Nov 6): an old task, Keep, or Close (how: done) or Not a task
    (how: notatask, the task cancelled and the verdict kept for the golden set)."""
    from . import backfill
    row_id, dry, action = t.row_id, t.dry, t.action
    if action in ("cleanup_keep", "cleanup_close"):
        how = str((getattr(t, "body", None) or {}).get("how", "done"))
        signal(action, {"id": row_id, "title": row_id}, "today", test=dry, how=how)
        if dry:
            return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
        if action == "cleanup_keep":
            res = backfill.keep_old(row_id)
            return JSONResponse({"ok": True, "keep": True, "state": res,
                                 "msg": {"cleanup": "Kept; its step cleanup is next", "kept": "Kept", "gone": ""}[res]})
        p = task_path(row_id)
        if not p:
            return JSONResponse({"error": "no task note for this card"}, status_code=400)
        msg = apply(p, "cancel" if how == "notatask" else "done", why="old task, from the backfill")
        backfill.mark(row_id, state="notatask" if how == "notatask" else "closed")
        cardnotes.closed(row_id, f"check-in app (backfill, {how})")
        return JSONResponse({"ok": True, "keep": True, "msg": "Not a task: cancelled" if how == "notatask" else "Closed"})
    signal(action, {"id": row_id, "title": row_id}, "today", test=dry)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    res = backfill.decide(task_path(row_id), row_id, action == "cleanup_apply")
    return JSONResponse({"ok": True, "keep": True, "state": res,
                         "msg": {"applied": "Steps cleaned up", "skipped": "Kept as it was",
                                 "stale": "The task changed since; nothing written", "gone": ""}[res]})


def on_step_fine(t):
    """step_fine (Nov 6): the review list's Fine, the step is clear enough as it is. Logged so the
    rules that misfire show; hidden until the step is reworded."""
    from . import review
    body, row_id, dry = t.body, t.row_id, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    sid = str(body.get("sid", ""))[:12]
    signal("step_fine", {"id": row_id, "title": row_id}, "today", test=dry, sid=sid)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    if not review.mark_fine(p, row_id, sid):
        return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
    return JSONResponse({"ok": True, "keep": True})


def on_step_feedback(t):
    """step_feedback (Nov 6): the review list's feedback on one step: Right or Wrong per reason and
    on the estimate, his minutes, a note; verdict save, rewrite or fine. The golden set's rows."""
    from . import review
    body, row_id, dry = t.body, t.row_id, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    sid = str(body.get("sid", ""))[:12]
    verdict = str(body.get("verdict", "save"))
    signal("step_feedback", {"id": row_id, "title": row_id}, "today", test=dry, sid=sid, verdict=verdict)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    votes = body.get("votes") if isinstance(body.get("votes"), dict) else {}
    est = body.get("est") if isinstance(body.get("est"), dict) else {}
    res = review.feedback(p, row_id, sid, votes, est, str(body.get("note", ""))[:2000], verdict)
    if res is None:
        return JSONResponse({"error": "nothing to save: vote, give your minutes or write a note"}, status_code=400)
    if not res:
        return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
    return JSONResponse({"ok": True, "keep": True, "msg": "Feedback saved"})


def on_step_start(t):
    """step_start (Tasks v2, Nov 6): Start on a card, the Next step's start for its actual time."""
    from ..adapters import flags
    body, row_id, dry = t.body, t.row_id, t.dry
    p = task_path(row_id)
    if not p or dry or not flags.on("actuals"):
        return JSONResponse({"ok": True, "keep": True, "msg": ""})
    _started(p, str(body.get("sid", ""))[:12] or tasks.read_note(p).get("next_id", ""), "start")
    return JSONResponse({"ok": True, "keep": True, "msg": ""})


def on_actual_check(t):
    """actual_check (Tasks v2): his one tap on a step far off its estimate: shorter, right, longer."""
    from tools import steps as S
    body, row_id, dry = t.body, t.row_id, t.dry
    answer, sid = str(body.get("answer", "")), str(body.get("sid", ""))[:12]
    if answer not in ("shorter", "right", "longer") or not sid:
        return JSONResponse({"error": "bad request"}, status_code=400)
    p = task_path(row_id)
    key = S._task_key(p.read_text(encoding="utf-8"), p.stem) if p else str(body.get("task_id", ""))
    signal("actual_check", {"id": row_id, "title": row_id}, "today", test=dry, sid=sid, answer=answer)
    if not dry:
        S.append(key, row_id, "tap:actual_check", {"event": "actual_check", "step": sid, "answer": answer})
    return JSONResponse({"ok": True, "keep": True, "msg": "Thanks, noted"})


def on_step_tick(t):
    """subtick, tick_line."""
    body, action, row_id, text_in, dry = t.body, t.action, t.row_id, t.text_in, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    if action == "subtick":
        ticks = _load_json(SUBTICKS, {})
        nxt, n = str(body.get("next", ""))[:2000], body.get("n")
        sid = str(body.get("sid", ""))[:12]
        if not isinstance(n, int) or n < 0:
            return JSONResponse({"error": "bad request"}, status_code=400)
        e = ticks.get(p.stem) if isinstance(ticks.get(p.stem), dict) else {}
        # The same step (id) with the same words: a reworded step splits into other parts,
        # so its old part ticks would land on the wrong parts (Nov 6).
        same = e.get("next") == nxt and (not sid or not e.get("sid") or e.get("sid") == sid)
        done = set(e.get("done", [])) if same else set()
        if body.get("on") and not done and sid:   # the first part ticked: the step started (Tasks v2)
            from ..adapters import flags
            if flags.on("actuals"):
                _started(p, sid, "subtick")
        done.add(n) if body.get("on") else done.discard(n)
        ticks[p.stem] = {"sid": sid, "next": nxt, "done": sorted(done), "at": dt.datetime.now(TZ).date().isoformat()}
        _save_json(SUBTICKS, ticks)
        signal("subtick", {"id": row_id, "title": row_id}, "today", n=n, on=bool(body.get("on")))
        return JSONResponse({"ok": True, "keep": True, "msg": "", "done": sorted(done)})
    # tick_line: one later step of the note, ticked where it stands, nothing else changed.
    # Matched by its words (the page sends the display text), its ✅ date added on write.
    from tools import steps as S
    base = p.read_text(encoding="utf-8")
    want = _one_line(text_in)
    key = tasks._norm_step(want)
    sid = str(body.get("sid", ""))[:12]

    def tick(text):
        lines = text.split("\n")
        # by its id (refactor step 1, Nov 5); by its words only for a page loaded before that
        hit = next((i for i, ln in enumerate(lines) if (st := S.parse_line(ln)) and st.state == "open"
                    and st.kind != "Waiting" and (st.id == sid if sid else (key and tasks._norm_step(st.text) == key))), None)
        if hit is None:
            return None
        st = S.parse_line(lines[hit])
        st.state = "done"
        lines[hit] = S.format_line(st)
        head, _, rest = "\n".join(lines).partition("\n---\n")
        return head + "\n---\n" + _log(rest, f"- {_label(dt.datetime.now(TZ).date())} · step ticked: {st.text} · check-in app")
    text = tick(base)
    if text is None:
        return JSONResponse({"error": "that step changed in the note; refresh"}, status_code=409)
    notes.write_from(p, base, text, lambda cur: tick(cur) or notes.refuse())
    signal("step_ticked", {"id": row_id, "title": row_id}, "today", text=want[:300])
    return JSONResponse({"ok": True, "keep": True, "msg": ""})


def on_step(t):
    """step_done, step_undo, not_waiting, step_wait, set_waiting, edit_waiting."""
    body, action, row_id, text_in, dry = t.body, t.action, t.row_id, t.text_in, t.dry
    # Done on a Do card is the step, not always the task (Nov 3): steps.py reads what
    # comes next. Undo puts the note back as it was, if nothing changed it since.
    import hashlib
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card"}, status_code=400)
    today_d = dt.datetime.now(TZ).date()
    undo_all = _load_json(STEP_UNDO, {})
    before = read0 = p.read_text(encoding="utf-8")
    if action == "step_undo":
        u = undo_all.get(p.stem)
        if not u or hashlib.sha256(before.encode()).hexdigest() != u.get("after"):
            return JSONResponse({"error": "the note changed since; undo it in Obsidian"}, status_code=409)
        signal("step_undone", {"id": row_id, "title": row_id}, "today", test=dry)
        if dry:
            return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
        notes.write_from(p, read0, u["before"], lambda cur: u["before"] if hashlib.sha256(
            cur.encode()).hexdigest() == u.get("after") else notes.refuse())
        undo_all.pop(p.stem, None)
        _save_json(STEP_UNDO, undo_all)
        return JSONResponse({"ok": True, "keep": True, "msg": "", "state": "undone"})
    who = _one_line(text_in)[:120]
    if action in ("step_wait", "set_waiting", "edit_waiting") and not who:
        return JSONResponse({"error": "say whom you wait on"}, status_code=400)
    u = undo_all.get(p.stem)

    def compute(cur):
        """(the note after the tap, what it did, the note before it for Undo), or None."""
        if action == "step_wait":
            # "Waiting instead" (Nov 3): the Done just made, redone as a wait on him/her.
            if not u or hashlib.sha256(cur.encode()).hexdigest() != u.get("after"):
                return None
            return (*tasks._stepmod().step_done(u["before"], today_d, _label(today_d), who=who), u["before"])
        if action == "edit_waiting":
            return tasks._stepmod().edit_waiting(cur, who, _label(today_d)), {"state": "waiting", "who": who}, cur
        if action == "set_waiting":
            return tasks._stepmod().set_waiting(cur, who, _label(today_d)), {"state": "waiting", "who": who}, cur
        if action == "not_waiting":
            return tasks._stepmod().not_waiting(cur, _label(today_d)), {"state": "not_waiting"}, cur
        return (*tasks._stepmod().step_done(cur, today_d, _label(today_d), wait_over=bool(body.get("over"))), cur)
    r = compute(before)
    if r is None:
        return JSONResponse({"error": "the note changed since; set the wait in the card"}, status_code=409)
    after, res, before = r
    signal(action, {"id": row_id, "title": row_id}, "today", test=dry, **res)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written", **res})
    held = {"before": before}

    def again(cur):   # the note changed underneath: the same tap on what is there now
        r2 = compute(cur)
        if r2 is None:
            notes.refuse()
        held["before"] = r2[2]
        return r2[0]
    written = notes.write_from(p, read0, after, again)
    before = held["before"]
    if action == "not_waiting":
        # Ending a wait makes it his again today (Nov 4: India prep, carried and not in the
        # plan, went to Left open after Not waiting): it joins today's Do list.
        try:
            from ..planning.actions import do as _dm
            _dm.add(clock.do_day(dt.datetime.now(TZ)), row_id)
        except Exception as exc:
            log_event("error", where="do.add after not_waiting", detail=repr(exc)[:200])
    undo_all[p.stem] = {"before": before, "after": hashlib.sha256(written.encode()).hexdigest(),
                        "at": today_d.isoformat()}
    _save_json(STEP_UNDO, {k: v for k, v in undo_all.items() if v.get("at") == today_d.isoformat()})
    return JSONResponse({"ok": True, "keep": True, "msg": "", **res})


def on_note_edit(t):
    """dod_confirm, dod_set, prop_accept, prop_ignore, steps_save."""
    body, action, row_id, text_in, dry = t.body, t.action, t.row_id, t.text_in, t.dry
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note"}, status_code=400)
    today_d = dt.datetime.now(TZ).date()
    # The DoD as it was, so the log shows what Claude guessed against what he kept (Nov 1).
    was = tasks.read_note(p)
    signal({"dod_confirm": "dod_confirmed", "dod_set": "dod_set", "prop_accept": "prop_accepted",
            "prop_ignore": "prop_ignored", "steps_save": "steps_edited"}[action],
           {"id": row_id, "title": row_id}, "today", test=dry, text=text_in[:400],
           old=str(body.get("old", ""))[:300], new=str(body.get("new", ""))[:300],
           dod_before=was.get("dod", ""), dod_before_guess=was.get("dod_guess", False))
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing written"})
    if action == "dod_confirm":
        live = tasks.read_note(p)
        if not live.get("dod"):
            return JSONResponse({"error": "no Definition of Done to confirm"}, status_code=400)
        write_dod(p, live["dod"], True, today_d)
        return JSONResponse({"ok": True, "keep": True, "msg": "Definition of Done confirmed"})
    if action == "dod_set":
        write_dod(p, text_in, True, today_d)
        return JSONResponse({"ok": True, "keep": True, "msg": "Definition of Done saved"})
    if action == "prop_accept":
        apply_prop(p, str(body.get("old", "")), str(body.get("new", "")), today_d)
        return JSONResponse({"ok": True, "keep": True, "msg": "Step updated"})
    if action == "prop_ignore":
        d = tasks._ignored_props()
        d.setdefault(row_id, []).append(str(body.get("old", ""))[:300])
        state.write(tasks.PROP_IGNORED, d)
        return JSONResponse({"ok": True, "keep": True, "msg": "Kept your line"})
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    save_steps(p, [str(x) for x in steps], today_d)
    return JSONResponse({"ok": True, "keep": True, "msg": "Steps saved"})


def on_notdone(t):
    dry, day_name, row = t.dry, t.day_name, t.row
    signal("probably_done_rejected", row, day_name, test=dry)
    return JSONResponse({"ok": True, "keep": True, "msg": "Noted: still open"})


# Remove, with one of the four reasons (adopted Oct 30) and an optional why.
def on_remove(t):
    body, row_id, dry, day_name, base_day, row, why = t.body, t.row_id, t.dry, t.day_name, t.base_day, t.row, t.why
    reason = body.get("reason") if isinstance(body.get("reason"), str) else ""
    do = {"done": "done", "nottoday": "tomorrow", "notimportant": "lower",
          "shouldnt": "cancel"}.get(reason)
    if not do:
        return JSONResponse({"error": "pick a reason"}, status_code=400)
    signal("removed", row, day_name, test=dry, reason=reason, why=why)
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card; refresh"}, status_code=400)
    msg = apply(p, do, base=base_day, dry=dry, why=why)
    if do in ("done", "cancel") and not dry:   # the same message does not come back as a card
        cardnotes.closed(row_id, f"check-in app ({reason})")
    return JSONResponse({"ok": True, "keep": dry, "msg": msg})


def on_update(t):
    body, row_id, dry, day_name, base_day, row = t.body, t.row_id, t.dry, t.day_name, t.base_day, t.row
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card; refresh"}, status_code=400)
    # Every field is text from the form; a number is accepted as text, anything else dropped.
    fields = {k: v if isinstance(v, str) else str(v)
              for k, v in (body.get("fields") if isinstance(body.get("fields"), dict) else {}).items()
              if k in ("status", "priority", "due", "scheduled", "time_est", "next", "next_min")
              and isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    msg = apply(p, "update", base=base_day, dry=dry, fields=fields)
    if fields.get("_changes"):
        signal("edited", row, day_name, test=dry,
               changes=[{"field": k, "old": o, "new": n} for k, o, n in fields["_changes"]])
    return JSONResponse({"ok": True, "keep": True, "msg": msg})


def on_auto_close_undo(t):
    row_id, dry, day_name, row = t.row_id, t.dry, t.day_name, t.row
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note"}, status_code=400)
    signal("auto_close_undone", row, day_name, test=dry)
    if dry:
        return JSONResponse({"ok": True, "keep": True, "msg": "Test mode, nothing changed"})
    from . import autoclose
    err = autoclose.undo(p, dt.datetime.now(TZ).date())
    if err:
        return JSONResponse({"error": err}, status_code=409)
    return JSONResponse({"ok": True, "keep": True, "msg": "Back open"})


def on_reopen(t):
    row_id, dry, day_name, base_day, row = t.row_id, t.dry, t.day_name, t.base_day, t.row
    # Reopen from the Done fold: back to open, completedDate cleared, logged (Oct 31).
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "this task is archived; reopen it in Obsidian"}, status_code=400)
    signal("reopened", row, day_name, test=dry)
    fields = {"status": "open"}
    msg = apply(p, "update", base=base_day, dry=dry, fields=fields)
    return JSONResponse({"ok": True, "keep": True, "msg": "Reopened" if msg == "Saved" else msg})


def on_task(t):
    """done, tomorrow, next, note, schedule."""
    action, row_id, text_in, dry, day_name, base_day, tab_name, row, why = t.action, t.row_id, t.text_in, t.dry, t.day_name, t.base_day, t.tab_name, t.row, t.why
    p = task_path(row_id)
    if not p:
        return JSONResponse({"error": "no task note for this card; refresh"}, status_code=400)
    if action == "done":
        name = {"replies": "reply_done_himself"}.get(tab_name) or (
            "probably_done_confirmed" if row.get("guess") == "done" else "done_missed")
        signal(name, row, day_name, test=dry, why=why)
    elif action == "tomorrow":
        signal({"replies": "reply_skipped", "claude": "claude_not_now"}.get(tab_name, "moved"),
               row, day_name, test=dry)
    elif action == "next":
        signal("next_rewritten", row, day_name, test=dry, old=row.get("next", ""), new=_one_line(text_in))
    elif action == "note":
        signal("note", row, day_name, test=dry, note=_one_line(text_in))
    elif action == "schedule":
        signal("added", row, day_name, test=dry, source="suggested", suggest=row.get("suggest", ""))
    try:
        msg = apply(p, action, base=base_day, text_in=text_in, dry=dry, why=why)
        if action == "done" and not dry:
            cardnotes.closed(row_id, "check-in app (done)")
        return JSONResponse({"ok": True, "msg": msg, "keep": dry or action in ("next", "note")})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
