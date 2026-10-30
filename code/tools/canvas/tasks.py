"""Canvas assignments -> the owner's task notes. The logic of the canvas_tasks job.

Pure planning plus two small file operations, so the whole thing is testable
offline. The job (`jobs/canvas_tasks.py`) fetches, calls `plan()`, and applies.

The rules are his (2026-09-23, commit-to-action SKILL.md § Canvas):

- One task per assignment, in `Inbox/`, for courses he is ENROLLED in only.
- **Never duplicate**, done tasks included. Match on the Canvas assignment URL
  in the task body; failing that, the same course number in the title plus a
  very similar title counts as existing and is reported, never created.
- A changed due date updates `due`. A submission on Canvas marks the task done.
  A task done in Obsidian but not submitted stays done and is only recorded.
- New tasks only for assignments due within HORIZON_DAYS, not yet submitted.
- An assignment gone from Canvas is flagged, never deleted.
- Every new task links to the file he writes the assignment in (2026-09-30,
  SYSTEM.md § Capture note schema): `<Course>/_mine/Assignments/<Name>/<Name>.md`,
  created if missing, found by its Canvas URL so a renamed one is not duplicated.
  `_mine/` is outside prune's whitelist, so the archive can never delete it.
- A task edited in the last RECENT_MINUTES is left alone this run: Sync is
  last-write-wins, so writing while he edits on the phone would lose his edit
  (scheduled-execution.md, gap 1).
- Every new task lists its readings under the Next line (2026-09-28, "always"):
  one checkbox each, the archive note plus the original link, never a bare
  title. Read from the archive on disk, not the links pass's state: that only
  knows external links, and he counts session pages and Canvas files too. The
  session is the assignment's Canvas module; an assignment in no module gets
  only what its own text links to, and says so (never a guess by date: the
  POLECON hand mapping was one session late until corrected, 2026-09-30).
"""

import difflib
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from urllib.parse import quote

from .archive import PACIFIC, pacific
from .layout import locate
from .render import html_to_markdown, safe_filename

HORIZON_DAYS = 21
RECENT_MINUTES = 15
SIMILAR = 0.75
CLOSED = ("done", "cancelled")   # finished or dropped; neither is live (2026-10-12)
TASK_DIRS = ("Inbox", "Archive/Inbox")
ASSIGNMENT_URL = re.compile(r"https?://[\w.-]+/courses/(\d+)/assignments/(\d+)")
OWN_URL = re.compile(r"^https?://[\w.-]+/courses/(\d+)/assignments/(\d+)\s*$", re.M)
# Submitted means he handed something in. "graded" with no submission is a
# grade entered for something done on paper or in class, and is not treated as
# his submission - the task stays as he left it.
SUBMITTED_STATES = {"submitted", "pending_review"}
VAULT_NAME = "<VAULT_NAME>"   # the obsidian:// vault name, same on every host


@dataclass
class Task:
    path: Path
    title: str
    fm: dict
    cid: str | None = None
    aid: str | None = None
    mtime: float = 0.0


@dataclass
class Action:
    kind: str          # create | set_due | set_done | done_not_submitted |
                       # possible_match | removed | recent
    course: str        # course number, e.g. "GSBGEN 510"
    title: str         # assignment name
    task: Task | None = None
    fields: dict = field(default_factory=dict)
    detail: str = ""
    body: str = ""
    new_title: str = ""
    folder: str = ""   # course folder, for the writing file (create only)
    assignment: dict = field(default_factory=dict)
    due_h: str = ""


def today_pacific() -> date:
    return datetime.now(ZoneInfo(PACIFIC)).date()


def course_number(code: str) -> str:
    """"F26-GSBGEN-510-01" -> "GSBGEN 510"; "F26-CS-351Z-01" -> "CS 351Z"."""
    m = re.match(r"^[A-Za-z]+\d{2}-([A-Z]+)-(\d+[A-Z]?)", code or "")
    return f"{m.group(1)} {m.group(2)}" if m else (code or "").strip()


def _frontmatter(text: str) -> tuple[dict, int, int]:
    """(fields, start, end) of the frontmatter block; flat keys only."""
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, -1, -1
    fm, key = {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+-\s", line) and key:
            fm[key] = (fm.get(key, "") + ", " + line.strip()[1:].strip()).strip(", ")
            continue
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            key = k.strip()
            fm[key] = v.strip().strip('"').strip("'")
    return fm, m.start(1), m.end(1)


def load_tasks(vault: Path) -> list[Task]:
    """Every `type: action` note in Inbox/ and Archive/Inbox/ (done ones are
    swept there after 30 days, and must still block a duplicate)."""
    out = []
    for d in TASK_DIRS:
        base = Path(vault) / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.md")):
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, _, _ = _frontmatter(text)
            if fm.get("type") != "action":
                continue
            # The task's own URL sits on a line by itself; links to related
            # assignments inside a checklist come first in some hand tasks
            # (OB 242 Reflection 1 lists both videos above its own URL).
            m = OWN_URL.search(text) or ASSIGNMENT_URL.search(text)
            out.append(Task(path=p, title=p.stem, fm=fm,
                            cid=m.group(1) if m else None, aid=m.group(2) if m else None,
                            mtime=p.stat().st_mtime))
    return out


def _norm(title: str, number: str) -> str:
    t = title.lower().replace(number.lower(), " ")
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"^\s*(complete|submit|write|do|finish|prep|prepare|turn in)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def similar(task_title: str, assignment: str, number: str) -> float:
    return difflib.SequenceMatcher(None, _norm(task_title, number),
                                   _norm(assignment, number)).ratio()


def is_submitted(a: dict) -> bool:
    sub = a.get("submission") or {}
    return bool(sub.get("submitted_at")) or sub.get("workflow_state") in SUBMITTED_STATES


def _points(p) -> str:
    if p in (None, ""):
        return ""
    return f", {int(p) if float(p).is_integer() else p} points"


def task_title(name: str, number: str) -> str:
    return safe_filename(f"Complete {name} ({number})", maxlen=150)


def plan(courses: list[dict], tasks: list[Task], today: date,
         now: float | None = None) -> list[Action]:
    """What to do. `courses`: [{"id", "code", "folder", "assignments": [...]}]."""
    now = time.time() if now is None else now
    recent = now - RECENT_MINUTES * 60
    horizon = today + timedelta(days=HORIZON_DAYS)
    unlinked = [t for t in tasks if not t.aid]
    actions: list[Action] = []

    def touch(kind, number, name, t, fields, detail=""):
        if t.mtime > recent:
            actions.append(Action("recent", number, name, t, fields,
                                  f"edited {int((now - t.mtime) // 60)} min ago; {kind} deferred"))
        else:
            actions.append(Action(kind, number, name, t, fields, detail))

    for c in courses:
        cid, number = str(c["id"]), course_number(c.get("code", ""))
        mine = {t.aid: t for t in tasks if t.cid == cid}
        live = set()
        for a in c["assignments"]:
            aid, name = str(a["id"]), a.get("name") or f"Assignment {a['id']}"
            live.add(aid)
            due, due_h = pacific(a.get("due_at"))
            submitted = is_submitted(a)
            t = mine.get(aid)
            if t:
                done = t.fm.get("status") in CLOSED
                if due and not done and t.fm.get("due") != due:
                    touch("set_due", number, name, t, {"due": due},
                          f"{t.fm.get('due') or 'none'} -> {due}")
                if submitted and not done:
                    touch("set_done", number, name, t,
                          {"status": "done", "completedDate": today.isoformat()})
                if t.fm.get("status") == "done" and not submitted:  # cancelled is meant
                    actions.append(Action("done_not_submitted", number, name, t))
                continue
            if submitted or not due:
                continue
            if not (today <= date.fromisoformat(due) <= horizon):
                continue
            near = [u for u in unlinked
                    if number.lower() in u.title.lower() and similar(u.title, name, number) >= SIMILAR]
            if near:
                actions.append(Action("possible_match", number, name, near[0],
                                      detail=f"'{near[0].title}' looks like '{name}'; not created"))
                continue
            # Next action first, as a checkbox (SYSTEM.md § Capture note schema, 2026-09-27)
            body = (f"## Next action\n- [ ] Next: complete and submit on Canvas\n\n"
                    f"Canvas assignment, due **{due_h}**{_points(a.get('points_possible'))}.\n\n"
                    f"{a.get('html_url') or ''}\n\n"
                    f"Source: Canvas sync {today.isoformat()} · Course: [[{c['folder']}]]\n")
            actions.append(Action("create", number, name, fields={"due": due}, body=body,
                                  new_title=task_title(name, number), folder=c["folder"],
                                  assignment=a, due_h=due_h))
        for aid, t in mine.items():
            if aid not in live and t.fm.get("status") not in CLOSED:
                actions.append(Action("removed", number, t.title, t,
                                      detail=f"assignment {aid} is no longer on Canvas"))
    return actions


def set_fields(path: Path, fields: dict) -> bool:
    """Replace or add flat frontmatter keys in place. True if the file changed.

    Line-level, never a whole-note rewrite: a lost race with a phone edit costs
    one field, not his note.
    """
    text = path.read_text(encoding="utf-8")
    _, start, end = _frontmatter(text)
    if start < 0:
        return False
    block = text[start:end]
    for k, v in fields.items():
        line = f"{k}: {v}"
        pat = re.compile(rf"^{re.escape(k)}:.*$", re.M)
        block = pat.sub(line, block, count=1) if pat.search(block) else block + "\n" + line
    new = text[:start] + block + text[end:]
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def obsidian_uri(vault: Path, path: Path) -> str:
    rel = str(Path(path).relative_to(vault)).removesuffix(".md")
    return f"obsidian://open?vault={VAULT_NAME}&file={quote(rel, safe='')}"


def find_writing_file(course: Path, aid: str) -> Path | None:
    """An existing writing file for this assignment, by its header `[Canvas](url)` link.

    Only that link: a Canvas prompt quoted below it can link other assignments
    (OB 242's Reflection Assignment 1 links both videos it asks about).
    """
    pat = re.compile(rf"\[Canvas\]\(https?://[^)\s]*/assignments/{re.escape(aid)}\)")
    for f in sorted((course / "_mine" / "Assignments").glob("*/*.md")):
        if f.stem == f.parent.name and pat.search(f.read_text(encoding="utf-8")):
            return f
    return None


def ensure_writing_file(root: Path, a: Action) -> Path:
    """The file he writes this assignment in; created (never overwritten) if missing."""
    course = locate(root, a.folder)
    aid = str(a.assignment["id"])
    found = find_writing_file(course, aid)
    if found:
        return found
    name = safe_filename(a.title, maxlen=150)
    path = course / "_mine" / "Assignments" / name / f"{name}.md"
    if path.exists():
        return path
    url = a.assignment.get("html_url") or ""
    desc = html_to_markdown(a.assignment.get("description") or "")[0].strip()
    now = datetime.now(ZoneInfo(PACIFIC))
    body = (f"---\ntype: note\ncaptured: {now.strftime('%Y-%m-%dT%H:%M')}\n---\n\n"
            f"# {a.title}\n\n**Due:** {a.due_h} · [Canvas]({url})\n\n")
    if desc:
        body += "> [!quote]- Canvas prompt\n> " + desc.replace("\n", "\n> ") + "\n\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "## Notes\n\n", encoding="utf-8")
    return path


# ---- readings ---------------------------------------------------------------

READING_KINDS = ("page", "file", "reading")   # in this order within a session
TEXT_OK = ("ok", "manual")                    # links.TEXT_STATUSES; not imported, it pulls in runner
WIKILINK = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
MDLINK = re.compile(r"\[([^\]\n]*)\]\((https?://[^\s)]+)\)")
GENERIC_LABEL = re.compile(r"^\(?(link|here|link here|this|pc|mac)\)?$", re.I)
NO_MODULE = "_Unfiled"


def _note_meta(path: Path) -> tuple[dict, str]:
    """(frontmatter, H1 title) of an archive note."""
    text = path.read_text(encoding="utf-8")
    fm, _, _ = _frontmatter(text)
    m = re.search(r"^# (.+)$", text, re.M)
    return fm, (m.group(1).strip() if m else path.stem)


def _canvas_index(course: Path) -> dict[str, Path]:
    """Archive notes of a course by Canvas URL or, for readings, original URL.
    `_mine/` is his, not Canvas's."""
    out = {}
    for p in sorted(course.rglob("*.md")):
        if "_mine" in p.relative_to(course).parts:
            continue
        try:
            fm = _note_meta(p)[0]
        except OSError:
            continue
        for url in (fm.get("canvas_url"), fm.get("canvas_external_url")):
            if url:
                out.setdefault(url.rstrip("/"), p)
    return out


def find_assignment_note(course: Path, aid: str) -> Path | None:
    """The assignment's archive note, by the Canvas URL in its frontmatter."""
    for url, p in _canvas_index(course).items():
        if url.endswith(f"/assignments/{aid}"):
            return p
    return None


def reading_line(vault: Path, note: Path, label: str = "") -> str:
    """`- [ ] [title](obsidian://note) · [original](url)`, with a marker when
    the archive copy holds no real text."""
    fm, title = _note_meta(note)
    if title.startswith("http") and label and not label.startswith("http") \
            and not GENERIC_LABEL.match(label):
        title = label
    if title.startswith("http"):
        title = re.sub(r"^https?://(www\.)?", "", title).rstrip("/")
    title = title.replace("[", "(").replace("]", ")")
    line = f"- [ ] [{title}]({obsidian_uri(vault, note)})"
    kind = fm.get("canvas_kind")
    if kind == "file" and fm.get("canvas_file_path"):
        raw = Path(vault) / fm["canvas_file_path"]
        ext = raw.suffix.lstrip(".").upper() or "file"
        uri = f"obsidian://open?vault={VAULT_NAME}&file={quote(fm['canvas_file_path'], safe='')}"
        line += f" · [{ext}]({uri})"
    elif fm.get("canvas_external_url") or fm.get("canvas_url"):
        line += f" · [original]({fm.get('canvas_external_url') or fm.get('canvas_url')})"
    status = fm.get("canvas_fetch_status") or fm.get("canvas_extract_status")
    if status and status not in TEXT_OK:
        line += f" (archive text: {status})"
    return line


def _linked(vault: Path, course: Path, note: Path, index: dict[str, Path]) -> list[tuple[Path | None, str, str]]:
    """What the assignment's own text links to: (archive note or None, label, url)."""
    text = note.read_text(encoding="utf-8")
    _, _, end = _frontmatter(text)
    text = text[end:] if end > 0 else text
    # His own submission is quoted in the note; only the prompt names readings.
    text = text.split("\n## My submission", 1)[0]
    out = []
    for m in WIKILINK.finditer(text):
        target = m.group(1).strip()
        if "/" not in target:          # the course anchor, not a reading
            continue
        p = Path(vault) / (target if target.endswith(".md") else target + ".md")
        if p.is_file() and p != note:
            out.append((p, (m.group(2) or "").strip(), ""))
    for m in MDLINK.finditer(text):
        label, url = m.group(1).strip("*_ "), m.group(2)
        out.append((index.get(url.rstrip("/")), label, url))
    return out


def readings(vault: Path, course: Path, note: Path) -> list[str]:
    """Reading lines for one assignment: its own links first (it names them),
    then the rest of its session. Deduplicated by target."""
    index = _canvas_index(course)
    seen, lines = set(), []
    for p, label, url in _linked(vault, course, note, index):
        if p is not None:
            if p in seen:
                continue
            seen.add(p)
            lines.append(reading_line(vault, p, label))
        elif url not in seen:
            seen.add(url)
            lines.append(f"- [ ] {label or url} · [original]({url})")
    module = note.parent.parent if note.parent.name == "Assignments" else note.parent
    if module.name == NO_MODULE or module == course:
        return lines
    found = []
    for p in module.rglob("*.md"):
        if "Assignments" in p.relative_to(module).parts or p in seen:
            continue
        kind = _note_meta(p)[0].get("canvas_kind")
        if kind in READING_KINDS:
            found.append((READING_KINDS.index(kind), p.name.lower(), p))
    for _, _, p in sorted(found):
        seen.add(p)
        lines.append(reading_line(vault, p))
    return lines


def readings_block(vault: Path, root: Path, folder: str, aid: str) -> str:
    """The readings paragraph for a task, always present: lines, or one line why not."""
    course = locate(root, folder)
    note = find_assignment_note(course, str(aid)) if course.is_dir() else None
    if not note:
        return NOT_FOUND + "\n"
    lines = readings(vault, course, note)
    module = note.parent.parent if note.parent.name == "Assignments" else note.parent
    if module.name == NO_MODULE or module == course:
        tail = "Session readings not mapped: this assignment is in no Canvas module.\n"
        return ("Readings linked from the assignment:\n" + "\n".join(lines) + "\n" + tail) if lines else tail
    if not lines:
        return "No readings in this session on Canvas.\n"
    return f"Readings ({module.name}):\n" + "\n".join(lines) + "\n"


NOT_FOUND = "Readings not found: the assignment is not in the archive yet."
# "Not found" is a placeholder, not a block: a later backfill replaces it.
HAS_READINGS = re.compile(r"^(Readings\b(?! not found)|No readings\b|Session readings\b)", re.M | re.I)


def has_readings(text: str) -> bool:
    """A readings block already there, written by this job or by hand."""
    if HAS_READINGS.search(text):
        return True
    return any("Course%20Archive" in l and "_mine" not in l
               for l in text.splitlines() if l.lstrip().startswith("- [") and "obsidian://" in l)


def _insert_at(body: str, block: str) -> str:
    """The block after the Write-it-here line, else after the Next line, else on top."""
    lines = body.split("\n")
    at = next((i for i, l in enumerate(lines) if l.startswith("**Write it here:**")), None)
    if at is None:
        at = next((i for i, l in enumerate(lines) if l.startswith("- [ ] Next:")), None)
    if at is None:
        return block + "\n" + body
    return "\n".join(lines[:at + 1] + [""] + block.rstrip("\n").split("\n") + lines[at + 1:])


COURSE_LINE = re.compile(r"Course: \[\[([^\]|]+)")


def plan_backfill(tasks: list[Task], now: float | None = None) -> tuple[list, list]:
    """(to_fill, deferred): open Canvas tasks with no readings block yet, as
    (task, course folder). Done ones are left as they are; a task edited in the
    last RECENT_MINUTES waits for the next run."""
    now = time.time() if now is None else now
    fill, wait = [], []
    for t in tasks:
        if not t.aid or t.fm.get("status") in CLOSED:
            continue
        text = t.path.read_text(encoding="utf-8")
        m = COURSE_LINE.search(text)
        if not m or has_readings(text):
            continue
        (wait if t.mtime > now - RECENT_MINUTES * 60 else fill).append((t, m.group(1).strip()))
    return fill, wait


def insert_readings(path: Path, block: str) -> bool:
    """Add a readings block to an existing task, once. True if written.

    Lines are inserted, never rewritten, so a lost race with a phone edit
    costs the block, not his note.
    """
    text = path.read_text(encoding="utf-8")
    if has_readings(text):
        return False
    if block.strip() == NOT_FOUND:
        return False
    text = text.replace(NOT_FOUND + "\n\n", "").replace(NOT_FOUND + "\n", "")
    _, _, end = _frontmatter(text)
    end = text.index("\n---", end) + 5 if end > 0 else 0
    path.write_text(text[:end] + _insert_at(text[end:], block), encoding="utf-8")
    return True


def create_task(vault: Path, a: Action, writing: Path | None = None,
                reading_text: str = "") -> Path | None:
    """Write a new task note. None if a note by that name already exists.

    `writing`: the assignment's writing file, linked under the Next line.
    `reading_text`: the block from `readings_block`, placed below that link.
    """
    path = Path(vault) / "Inbox" / f"{a.new_title}.md"
    if path.exists():
        return None
    body = a.body
    if writing:
        link = f"**Write it here:** [{writing.stem}]({obsidian_uri(vault, writing)})\n"
        body = body.replace("\n\n", f"\n\n{link}\n", 1)
    if reading_text:
        body = _insert_at(body, reading_text)
    now = datetime.now(ZoneInfo(PACIFIC))
    fm = ("---\ntype: action\n"
          f"captured: {now.strftime('%Y-%m-%dT%H:%M')}\n"
          "status: inbox\nprojects:\n  - \"[[MBA]]\"\npriority: 1-none\nwaiting: false\n"
          f"due: {a.fields['due']}\n---\n")
    path.write_text(fm + body, encoding="utf-8")
    return path
