"""Daily: refresh the courses the owner is enrolled in, and keep his tasks in step.

    python3 -m jobs.canvas_tasks --dry-run    # plans and reports, writes nothing

Built 2026-09-23 to his spec (relayed from the commit-to-action session):

1. `vt canvas refresh` for the current quarter's ENROLLED courses only - never
   waitlisted or shopping ones.
2. Assignment tasks in Inbox/: due dates follow Canvas (converted to Pacific
   time), a Canvas submission marks the task done, new assignments due within
   21 days become Triage tasks, and an assignment removed from Canvas is
   flagged, never deleted. Never a duplicate, done tasks included. All rules
   live in `tools/canvas/tasks.py`.
3. Each assignment's due date lands in its archive note as `canvas_due` (the
   pull writes it).
4. Every new task lists its readings under the Next line (2026-09-28), read
   from the freshly refreshed archive (`tasks.readings_block`).

    python3 -m jobs.canvas_tasks --backfill-readings [--dry-run]

adds the readings block to open assignment tasks that have none (created
before 2026-10-05, or filled by nobody). No refresh and no Canvas calls: the
course comes from the task's `Course:` line. The one body edit this job makes,
and only ever an insertion; a task with any readings block, his hand-written
ones included, is left alone.

Does not schedule anything, write calendar events, or create class-prep tasks.

Writes into live task notes, which the runner's pre/post vault git snapshot
makes revertable (scheduled-execution.md gap 1). Edits are single frontmatter
lines, and a task edited in the last 15 minutes is left for the next run.

Holds the Canvas lock (tools/canvas/lock.py) for the whole run, so it never
overlaps a manual `vt canvas` command; if one is running, this run is skipped.
"""

import os
import sys
from datetime import date

from runner import config
from runner.job import AUTOMATIC, IMPORTANT, ROUTINE, YOU, Job, log

CONCERN = "canvas-task-sync"
MBA_ENDS = date(2027, 6, 30)
TOKEN_WARN_DAYS = 7


def main() -> int:
    from tools.canvas import archive, tasks as T
    from tools.canvas.__main__ import _client, main as canvas_main
    from tools.canvas.layout import PROGRAM, group_key, quarter_of
    from tools.canvas.lock import Busy, canvas_lock

    with Job("canvas_tasks", description="Canvas to tasks sync",
             watches=(CONCERN, "canvas-token")) as job:
        today = T.today_pacific()

        if today > MBA_ENDS:
            job.check("MBA is over: disable the canvas_tasks timer", False,
                      detail="sudo systemctl disable --now automation@canvas_tasks.timer",
                      tier=ROUTINE, owner=YOU, covers=CONCERN)
            return 0

        env = config.load_env()
        raw = env.get("CANVAS_TOKEN_EXPIRES", "")
        try:
            left = (date.fromisoformat(raw) - today).days
        except ValueError:
            left = None
        job.check("Canvas token not expiring within a week", left is not None and left > TOKEN_WARN_DAYS,
                  detail=(f"CANVAS_TOKEN_EXPIRES={raw or 'unset'}: {left} day(s) left. Mint a new "
                          "token at canvas.stanford.edu/profile/settings and update CANVAS_TOKEN and "
                          "CANVAS_TOKEN_EXPIRES in ~/.automation.env on vault-host"),
                  tier=IMPORTANT, owner=YOU, key="canvas-token-expiry", covers="canvas-token")
        if left is not None and left < 0:
            return 0  # the token is dead; the check above is the report

        try:
            with canvas_lock("canvas_tasks job"):
                if "--backfill-readings" in sys.argv:
                    _backfill(job, archive, T)
                else:
                    _run(job, today, archive, T, _client, canvas_main, PROGRAM, group_key, quarter_of)
            job.check("Canvas lock was free", True, key="canvas-lock-free", tier=ROUTINE,
                      owner=AUTOMATIC, covers=CONCERN)
        except Busy as who:
            log(f"canvas_tasks: skipped, Canvas lock held by {who}")
            job.record("skipped", f"Canvas lock held by {who}")
            job.check("Canvas lock was free", False, key="canvas-lock-free", tier=ROUTINE,
                      owner=AUTOMATIC, covers=CONCERN,
                      detail=f"held by {who}; this run did nothing. Escalates if it keeps happening")
    return 0


def _run(job, today, archive, T, _client, canvas_main, PROGRAM, group_key, quarter_of):
    client = _client()
    courses = archive.list_courses(client)
    by_q = {}
    for c in courses:
        by_q.setdefault(quarter_of(archive.course_folder(c)), []).append(c)
    terms = sorted((q for q in by_q if q != PROGRAM), key=group_key)
    if not job.check("a current quarter found on Canvas", bool(terms),
                     detail="no dated enrolments came back", covers=CONCERN):
        return
    enrolled = [c for c in by_q[terms[0]] if archive.enrollment_of(c) == "enrolled"]
    job.record("quarter", terms[0])
    job.record("enrolled courses", sorted(T.course_number(c.get("course_code", "")) for c in enrolled))
    if not enrolled:
        return

    if not job.dry_run:
        argv = ["refresh"] + [a for c in enrolled for a in ("--course", str(c["id"]))]
        try:
            rc = canvas_main(argv)
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else 1
            log(f"canvas_tasks: refresh stopped: {e.code}")
        job.check("archive refresh of enrolled courses completed", rc == 0,
                  detail=f"vt canvas refresh exited {rc}; see the journal for this run",
                  tier=IMPORTANT, owner=AUTOMATIC, key="canvas-refresh", covers=CONCERN)

    plan_input = []
    for c in enrolled:
        rows, status = client.try_paginate(f"courses/{c['id']}/assignments", include=["submission"])
        if not job.check(f"assignments readable: {T.course_number(c.get('course_code', ''))}",
                         status == "ok", detail=f"Canvas said {status}", tier=ROUTINE,
                         key=f"assignments-{c['id']}", covers=CONCERN):
            continue  # an unreadable course must not look like "all assignments removed"
        plan_input.append({"id": c["id"], "code": c.get("course_code", ""),
                           "folder": archive.course_folder(c), "assignments": rows})

    actions = T.plan(plan_input, T.load_tasks(config.VAULT), today)
    done = {"create": [], "set_due": [], "set_done": [], "readings": []}
    for a in actions:
        label = f"{a.title} ({a.course})"
        if a.kind == "create":
            writing = None
            if not job.dry_run:
                try:
                    writing = T.ensure_writing_file(archive.ROOT, a)
                except Exception as e:  # a missing writing file must not block the task
                    log(f"canvas_tasks: writing file for {label} failed: {e}")
                    job.record(f"writing file not created: {label}", str(e))
            try:
                reading_text = T.readings_block(config.VAULT, archive.ROOT, a.folder, a.assignment["id"])
            except Exception as e:  # missing readings must not block the task either
                log(f"canvas_tasks: readings for {label} failed: {e}")
                job.record(f"readings not listed: {label}", str(e))
                reading_text = ""
            if job.dry_run or T.create_task(config.VAULT, a, writing, reading_text):
                done["create"].append(label)
                done["readings"].append(f"{label}: {_summary(reading_text)}")
            else:
                job.record(f"not created, name taken: {a.new_title}", label)
        elif a.kind in ("set_due", "set_done"):
            if job.dry_run or T.set_fields(a.task.path, a.fields):
                done[a.kind].append(f"{label}{': ' + a.detail if a.detail else ''}")
        elif a.kind == "removed":
            job.check(f"assignment still on Canvas: {a.title}", False, detail=a.detail,
                      tier=IMPORTANT, owner=YOU, key=f"removed-{a.task.aid}", covers=CONCERN)
        elif a.kind == "due_before_capture":
            job.check(f"due date plausible: {a.task.title}", False, detail=a.detail,
                      tier=ROUTINE, owner=YOU, key=f"due-before-capture-{a.task.aid}", covers=CONCERN)
        elif a.kind == "ambiguous":
            job.check(f"task links one assignment: {a.task.title}", False, detail=a.detail,
                      tier=ROUTINE, owner=YOU, key=f"ambiguous-{a.task.path.stem[:60]}", covers=CONCERN)

    job.record("tasks created", done["create"])
    job.record("readings listed in new tasks", done["readings"])
    job.record("due dates updated", done["set_due"])
    job.record("marked done (submitted on Canvas)", done["set_done"])
    job.record("possible matches, not created",
               [f"{a.title} ({a.course}): {a.detail}" for a in actions if a.kind == "possible_match"])
    job.record("done in Obsidian, not submitted on Canvas",
               [f"{a.title} ({a.course})" for a in actions if a.kind == "done_not_submitted"])
    job.record("deferred, task edited in the last 15 min",
               [f"{a.title} ({a.course}): {a.detail}" for a in actions if a.kind == "recent"])
    if job.dry_run:
        for k, v in done.items():
            log(f"canvas_tasks dry run: would {k}: {v}")


def _summary(block: str) -> str:
    n = sum(line.startswith("- [ ] ") for line in block.splitlines())
    return f"{n} reading(s)" if n else (block.strip().splitlines() or ["none"])[0]


def _backfill(job, archive, T):
    fill, wait = T.plan_backfill(T.load_tasks(config.VAULT))
    added, failed = [], []
    for t, folder in fill:
        try:
            block = T.readings_block(config.VAULT, archive.ROOT, folder, t.aid)
            if job.dry_run or T.insert_readings(t.path, block):
                added.append(f"{t.title}: {_summary(block)}")
        except Exception as e:
            log(f"canvas_tasks backfill: {t.title} failed: {e}")
            failed.append(f"{t.title}: {e}")
    job.record("readings added to existing tasks", added)
    job.record("deferred, task edited in the last 15 min", [t.title for t, _ in wait])
    job.check("readings backfill wrote every task it planned", not failed,
              detail="; ".join(failed), tier=ROUTINE, owner=AUTOMATIC,
              key="readings-backfill", covers=CONCERN)
    if job.dry_run:
        for x in added:
            log(f"canvas_tasks dry run: would add readings: {x}")


if __name__ == "__main__":
    sys.exit(main())
