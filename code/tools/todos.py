#!/usr/bin/env python3
"""Fast reader/writer for the owner's Obsidian to-do system (Inbox/, type: action).

Parses the YAML frontmatter of every `type: action` note in `Inbox/` in one
pass and prints them grouped into the SAME views the cockpit uses, so there is
one definition of "active" in the system. The cockpit is `ToDo.base` at the
vault root; this script is a fast reader of its definitions, never a second
opinion. If a view's filter changes in `ToDo.base`, mirror it here.

View definitions mirrored from ToDo.base (verified 2026-10-12). "Closed" is
status done or cancelled (CLOSED below):
  ToDo         (due is set OR (scheduled is set AND scheduled <= today))
               AND not closed
  Triage       status == "inbox"   (inbox == untriaged, as of 2026-08-04)
  Unscheduled  no due AND no scheduled AND status != "inbox"
               AND (not recurring -> not closed;
                    recurring     -> not completed today)
  All Tasks    not closed

Also writes a new capture note the same way the Raycast extension and the
iPhone Shortcut do, matching the vault's capture schema in _system/SYSTEM.md.

Usage:
  python3 todos.py                            # all three cockpit views (default)
  python3 todos.py --view todo                # the ToDo view only
  python3 todos.py --view triage              # untriaged captures (status: inbox)
  python3 todos.py --view unscheduled         # the Someday bucket
  python3 todos.py --view all                 # every not-done task, flat
  python3 todos.py --json                     # machine-readable dump
  python3 todos.py --add "Text of the task" [--urgent] [--due 2026-08-11]
                    [--scheduled 2026-08-04] [--priority 3-normal]
                    [--project "Productivity Stack Build"] [--next "First physical step"]
"""
import argparse
import datetime as dt
import glob
import os
import re

# Resolved at runtime so this works on vault-host as well as the Mac. It
# used to hardcode the Mac path, which is why the skill could never have
# run unattended on the box even once the file got there.
from runner.config import INBOX as _INBOX
INBOX_DIR = str(_INBOX)

# Filenames to always skip: the schema doc / template.
SKIP_FILES = {"_Inbox Template"}

# Locked priority scale (task-cockpit.md). P1/P2/P3 was abandoned 2026-07-28.
PRIORITIES = ["1-none", "2-low", "3-normal", "4-high"]
DEFAULT_PRIORITY = "1-none"
URGENT_PRIORITY = "4-high"


_FM_CACHE = {}   # path -> ((mtime_ns, size), fm); Carmen reads the Inbox several times a view


def parse_fm(path):
    """parse_fm, kept per file until the file changes (Nov 3: the check-in app read all 400
    notes three times per page load). Returns a copy, so a caller can never edit the cache."""
    try:
        st = os.stat(path)
    except OSError:
        return _parse_fm(path)
    sig = (st.st_mtime_ns, st.st_size)
    hit = _FM_CACHE.get(path)
    if hit and hit[0] == sig:
        return dict(hit[1]) if hit[1] is not None else None
    fm = _parse_fm(path)
    _FM_CACHE[path] = (sig, fm)
    return dict(fm) if fm is not None else None


def _parse_fm(path):
    """Line-based frontmatter reader. Handles `key: value` and block lists
    (`key:` followed by indented `- item` lines). Not a full YAML parser —
    enough for this schema, which is flat."""
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except Exception:
        return None
    m = re.match(r"^---\n(.*?)\n---", txt, re.S)
    if not m:
        return None
    fm = {}
    key = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s+-\s", line) and key:
            item = line.strip()[1:].strip().strip('"').strip("'")
            prev = fm.get(key, "")
            fm[key] = f"{prev}, {item}" if prev else item
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            key = k.strip()
            fm[key] = v.strip().strip('"').strip("'")
    return fm


def truthy(v):
    return str(v).strip().lower() == "true"


def as_date(raw):
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def as_int(raw):
    """Numeric frontmatter as int, or None. Lets consumers sum without parsing."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _step_due(path: str) -> str:
    """The earliest 📅 of an open step in the note's Next action block, or "" (Nov 11, audit A9)."""
    from tools import steps as S
    try:
        body = open(path, encoding="utf-8", errors="replace").read().partition("\n---\n")[2]
    except OSError:
        return ""
    st = S.steps_of(body)
    ds = [s.due for s in st if s.state == "open" and s.kind != "Waiting" and s.due]
    # D-35
    # A chain that must start today (a wait before a dated step) makes the task due today.
    from tools import backsched as B
    if B.enabled():
        try:
            import datetime as _dt
            today = S._today()
            r = B.compute(B.from_steps(st, today), today)
            if r.get("state"):
                ds.append(max(r["start_by"], today.isoformat()))
        except Exception:   # noqa: BLE001 - its own dates as before
            pass
    return min(ds) if ds else ""


def effective_due(t: dict) -> str:
    """A task's deadline: its own `due` or the earliest open step 📅, whichever comes first
    (Nov 11, audit A9: a task whose only deadline was a step 📅 was due on Today but missing from
    Tomorrow and the morning panel). t: a load_tasks row or a Carmen read_note dict."""
    ds = [str(x)[:10] for x in (t.get("due"), t.get("step_due")) if x]
    return min(ds) if ds else ""


def is_later(t: dict, day) -> bool:
    """Planned for a later day and not due by `day` (the one "later" rule)."""
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)
    sch, due = (t.get("scheduled") or "")[:10], effective_due(t)
    return bool(sch) and sch > iso and not (due and due <= iso)


def load_tasks():
    """Every `type: action` note in Inbox/, done included (views filter)."""
    rows = []
    for p in glob.glob(os.path.join(INBOX_DIR, "*.md")):
        name = os.path.basename(p)[:-3]
        if name in SKIP_FILES:
            continue
        fm = parse_fm(p)
        if not fm or fm.get("type") != "action":
            continue
        rows.append({
            "name": name,
            "status": fm.get("status", ""),
            "urgent": truthy(fm.get("urgent", "")),
            # `waiting` is a boolean frontmatter field, not a status value.
            "waiting": truthy(fm.get("waiting", "")),
            "priority": fm.get("priority", ""),
            "due": fm.get("due", ""),
            "step_due": _step_due(p),   # the earliest open step 📅 (audit A9); effective_due() takes both
            "scheduled": fm.get("scheduled", ""),
            "recurrence": fm.get("recurrence", ""),
            "complete_instances": fm.get("complete_instances", ""),
            "project": fm.get("projects", "") or fm.get("project", ""),
            # Placement fields, added 2026-08-05 after the first `schedule-work`
            # run. That skill is told to use this reader and warned not to build
            # a second one — but its placement table runs on exactly these four,
            # none of which were emitted, so the run had to build a second
            # reader anyway. Keep them here rather than re-solving it per run.
            "timeEstimate": as_int(fm.get("timeEstimate", "")),
            "quadrant": fm.get("quadrant", ""),
            "startable": fm.get("startable", ""),
            "tags": fm.get("tags", ""),
            # `decision: pending | made`, added 2026-08-07 with the Undecided
            # view in ToDo.base as its consumer. Emitted here for the same
            # reason as the four above: the reader has to emit what the other
            # skills consume, or they build a second reader. Empty means the
            # task is not decision-shaped, which is most of them.
            "decision": fm.get("decision", ""),
            "ctime": getattr(os.stat(p), "st_birthtime", os.stat(p).st_mtime),
            "path": p,
        })
    return rows


# --- view filters, mirrored from ToDo.base -------------------------------

# Finished (done) or dropped (cancelled): neither is live work (owner decision, 2026-10-12).
CLOSED = ("done", "cancelled")


def v_todo(rows, today):
    out = []
    for r in rows:
        if r["status"] in CLOSED:
            continue
        sched = as_date(r["scheduled"])
        if r["due"] or (sched and sched <= today):
            out.append(r)
    return sorted(out, key=lambda r: (as_date(r["due"]) or dt.date.max,
                                      as_date(r["scheduled"]) or dt.date.max,
                                      r["name"].lower()))


def v_triage(rows, today):
    return sorted([r for r in rows if r["status"] == "inbox"],
                  key=lambda r: -r["ctime"])


def v_unscheduled(rows, today):
    out = []
    iso = today.isoformat()
    for r in rows:
        if r["due"] or r["scheduled"]:
            continue
        if r["status"] == "inbox":
            continue
        if r["recurrence"]:
            # recurring: live unless already completed today
            done_today = iso in [c[:10] for c in
                                 re.split(r",\s*", r["complete_instances"]) if c]
            if done_today:
                continue
        elif r["status"] in CLOSED:
            continue
        out.append(r)
    return by_priority(out)


def by_priority(rows):
    """Priority DESC (the base's sort), name ASC as the tiebreak."""
    return sorted(sorted(rows, key=lambda r: r["name"].lower()),
                  key=lambda r: r["priority"] or "", reverse=True)


def v_all(rows, today):
    return by_priority([r for r in rows if r["status"] not in CLOSED])


VIEWS = {"todo": v_todo, "triage": v_triage,
         "unscheduled": v_unscheduled, "all": v_all}

HEADERS = {"todo": "✅ ToDo", "triage": "📥 Triage",
           "unscheduled": "🗓 Unscheduled", "all": "🗃 All Tasks"}


# --- output ---------------------------------------------------------------


def clean_project(raw):
    names = re.findall(r"\[\[([^\]|]+)", raw)
    return ", ".join(names) if names else raw


def line(r):
    tags = []
    if r["urgent"]:
        tags.append("🔥urgent")
    if r["waiting"]:
        tags.append("⏳waiting")
    if r["priority"] and r["priority"] != DEFAULT_PRIORITY:
        tags.append(f"❗{r['priority']}")
    if r["due"]:
        tags.append(f"due:{r['due'][:10]}")
    if r["scheduled"]:
        tags.append(f"sched:{r['scheduled'][:10]}")
    if r["project"] and r["project"] not in ("", "[]"):
        tags.append(f"#{clean_project(r['project'])}")
    suffix = ("  " + " ".join(tags)) if tags else ""
    return f"- {r['name']} [{r['status'] or 'inbox'}]{suffix}"


def print_view(key, rows, today, show_empty=True):
    sel = VIEWS[key](rows, today)
    if not sel and not show_empty:
        return
    print(f"{HEADERS[key]} — {len(sel)} task(s)")
    for r in sel:
        print(line(r))
    print()


# --- writing --------------------------------------------------------------


def sanitize_filename(text):
    first_line = text.splitlines()[0] if text.splitlines() else text
    safe = re.sub(r"[\\/:#^\[\]|]", "-", first_line)
    safe = re.sub(r"\s+", " ", safe).strip()
    return safe[:80] or "Untitled"


def unique_path(name):
    path = os.path.join(INBOX_DIR, f"{name}.md")
    if not os.path.exists(path):
        return path
    i = 1
    while True:
        candidate = os.path.join(INBOX_DIR, f"{name} {i}.md")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def add_task(text, urgent, due, scheduled, priority, project, next_action="", body=""):
    """Matches the capture schema in _system/SYSTEM.md: every capture writes
    `priority` explicitly (1-none normally); Urgent writes urgent: true,
    priority: 4-high AND scheduled: <today> — the date is what actually gets
    it into the cockpit's ToDo view, priority is not a membership trigger."""
    # Pacific, never the box clock (Nov 4: on vault-host, which runs UTC, a note made after 16:00 PT
    # was captured as the next day, and Urgent scheduled it for tomorrow).
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
    today = now.date().isoformat()
    if urgent:
        priority = priority or URGENT_PRIORITY
        scheduled = scheduled or today
    else:
        priority = priority or DEFAULT_PRIORITY

    from tools import taskids   # the stable key blocks and Carmen join on (Nov 3)
    fm = ["---", "type: action", f"task_id: {taskids.new_id(set(taskids.index().keys()))}",
          f"captured: {now.strftime('%Y-%m-%dT%H:%M')}", "status: inbox"]
    if urgent:
        fm.append("urgent: true")
    fm.append(f"priority: {priority}")
    if due:
        fm.append(f"due: {due}")
    if scheduled:
        fm.append(f"scheduled: {scheduled}")
    if project:
        fm.append(f'projects: ["[[{project}]]"]')
    fm.append("---")
    # Every task carries a next action as a checkbox, first under the frontmatter
    # (SYSTEM.md § Capture note schema, 2026-09-27). The capture text is the
    # starting step; triage (prioritise Pass 1) sharpens it. With --next, the
    # text names the note and the next action is given separately.
    step = (next_action or text).strip()
    content = "\n".join(fm) + "\n\n## Next action\n- [ ] Next: " + step + "\n"
    # Context goes BELOW the next-action block, never above it (the schema's
    # "## Facts, Source:" slot).
    if body.strip():
        content += "\n" + body.strip() + "\n"

    try:   # step ids from the start (tools/steps.py); a layer on top, never blocks the capture
        from tools import steps as S
        content = S.normalize(content)
    except Exception:
        pass
    path = unique_path(sanitize_filename(text))
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        from tools import steps as S
        import pathlib
        S.observe(pathlib.Path(path), "create")
    except Exception:
        pass
    return path


def main():
    ap = argparse.ArgumentParser(
        description="Read/write the owner's Obsidian to-dos (Inbox/, type: action).")
    ap.add_argument("--view", default="cockpit",
                    choices=["cockpit", "todo", "triage", "unscheduled", "all"],
                    help="cockpit (default) prints ToDo + Triage + Unscheduled")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--add", metavar="TEXT", help="Create a new action note in Inbox/")
    ap.add_argument("--urgent", action="store_true",
                    help="with --add: urgent: true + priority 4-high + scheduled today")
    ap.add_argument("--due", default="", metavar="YYYY-MM-DD",
                    help="real deadlines only")
    ap.add_argument("--scheduled", default="", metavar="YYYY-MM-DD",
                    help="day you intend to work on it")
    ap.add_argument("--priority", default="", choices=[""] + PRIORITIES,
                    help="1-none | 2-low | 3-normal | 4-high")
    ap.add_argument("--project", default="", metavar="NAME",
                    help="exact title of an existing project note")
    ap.add_argument("--next", default="", metavar="TEXT",
                    help="with --add: the next action, when TEXT is only the title")
    args = ap.parse_args()

    if args.add:
        path = add_task(args.add, args.urgent, args.due, args.scheduled,
                        args.priority, args.project, args.next)
        print(f"Created: {path}")
        return

    rows = load_tasks()
    from runner import clock   # his day, Pacific: the box runs UTC (audit A4)
    today = clock.today()

    if args.json:
        import json
        payload = {k: [{kk: vv for kk, vv in r.items() if kk != "ctime"}
                       for r in VIEWS[k](rows, today)]
                   for k in ("todo", "triage", "unscheduled", "all")}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if args.view != "cockpit":
        print_view(args.view, rows, today)
        return

    active = len(v_all(rows, today))
    print(f"ACTIVE TASKS: {active}  (mirroring ToDo.base — {today.isoformat()})\n")
    for k in ("todo", "triage", "unscheduled"):
        print_view(k, rows, today)


if __name__ == "__main__":
    main()
