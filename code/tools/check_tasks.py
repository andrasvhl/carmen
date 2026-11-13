"""The task-note schema, checked where it is written.

_system/SYSTEM.md § Capture note schema is the rule; this is its executable
form for task notes (`Inbox/*.md` with `type: action`).

    vt tasks [FILE...]    check files, or every task in Inbox/
    vt tasks --hook       Claude Code PostToolUse hook: reads the hook JSON on
                          stdin and checks the edited task. Errors exit 2 with
                          the list on stderr so the editing session fixes them;
                          questions only (judgment calls a script cannot make)
                          go back as additionalContext and do not block

Why the hook exists (2026-10-07): a session updated a task by hand, outside
any skill, added `- [ ] Waiting:` and left `waiting: false`, because the only
rule loaded named one half. The full rule sat in SYSTEM.md and plan-today,
neither of which an ad-hoc edit reads. The morning pre-run would have caught
it a day later; the hook catches it in the session that made the edit.

Two tiers, kept apart on purpose:
  errors     structure a script can verify (block layout, both halves of
             waiting, status vocabulary, registered keys, the log's shape)
  questions  state only a person or the session can judge (is it done now,
             is the ball with someone else, did the calendar follow)

Not checked, by decision: writing style (ISO dates, em dashes).

Closed is `done` (the work was finished) or `cancelled` (it was dropped, merged
or superseded). They are kept apart so dropped work never counts as done work,
e.g. in a planned-vs-done review (owner decision, 2026-10-12). Both are closed for every
other rule here, and both carry `completedDate`, because TaskNotes writes it for
any status marked isCompleted.
"""

import json
import re
import sys
from datetime import date
from pathlib import Path

STATUSES = {"inbox", "open", "in-progress", "done", "cancelled"}
ACTIVE = {"open", "in-progress"}
CLOSED = {"done", "cancelled"}

# Keys every capture writes or Obsidian itself owns, beyond the registry tables.
BASE_KEYS = {"type", "status", "captured", "tags", "aliases"}

# A ticked step with one of these verbs usually hands the ball to someone else.
OUTWARD = re.compile(r"\b(send|sent|e-?mail|mail|message|text|whatsapp|ask|asked|reply|"
                     r"replied|request|requested|apply|applied|submit|submitted|book|booked|"
                     r"call|called|invite|invited|pay|paid)\b", re.I)

NEXT_OPEN = re.compile(r"^- \[ \] Next:", re.M)
THEN_OPEN = re.compile(r"^- \[ \] Then:", re.M)
WAIT_OPEN = re.compile(r"^- \[ \] Waiting:", re.M)
LOG_LINE = re.compile(r"^- [A-Z][a-z]{2} \d{1,2} ·")
DOD = re.compile(r"^DoD( \(Claude's guess\))?: \S.*$")
CTX_START, CTX_END = "## Current context", "%% end current context %%"


def find_vault(path: Path | None = None) -> Path | None:
    if path is not None:
        for p in path.resolve().parents:
            if (p / "_system").is_dir() and (p / "Inbox").is_dir():
                return p
    return next((v for v in (Path.home() / "Obsidian/<VAULT_NAME>",
                             Path.home() / "vault") if (v / "_system").is_dir()), None)


def registered_keys(vault: Path | None) -> set[str] | None:
    """Every backticked name in the first column of frontmatter-properties' tables."""
    if vault is None:
        return None
    reg = vault / "_system/frontmatter-properties.md"
    if not reg.exists():
        return None
    keys = set(BASE_KEYS)
    text = reg.read_text(errors="ignore")
    # The `known-properties` fence is the allowlist the inventory job reads.
    for block in re.findall(r"```known-properties\n(.*?)```", text, re.S):
        for line in block.splitlines():
            if not line.lstrip().startswith("#"):
                keys.update(line.split())
    for line in text.splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|", line)
        if m:
            keys.update(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", m.group(1)))
    return keys


def split(text: str) -> tuple[dict, str] | None:
    m = re.match(r"---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        k = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if k:
            fm[k.group(1)] = k.group(2).strip().strip('"\'')
    return fm, text[m.end():]


def next_block(body: str) -> str:
    m = re.search(r"^## Next action.*?$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1) if m else ""


def is_task(text: str) -> bool:
    parts = split(text)
    return bool(parts) and parts[0].get("type") == "action"


def log_tail(body: str) -> str:
    """Text under the real `## Log` heading, not a quoted `> ## Log` inside an excerpt."""
    parts = re.split(r"^## Log[ \t]*$", body, flags=re.M)
    return parts[-1] if len(parts) > 1 else ""


def check(text: str, keys: set[str] | None, today: str | None = None,
          edit: dict | None = None) -> tuple[list[str], list[str]]:
    """Return (errors, questions) for one task note."""
    from runner import clock   # his day, Pacific: the box runs UTC (audit A4)
    today = today or clock.today().isoformat()
    errors, questions = [], []
    parts = split(text)
    if not parts:
        return ["no frontmatter"], []
    fm, body = parts
    status = fm.get("status", "")
    waiting = fm.get("waiting", "").lower() == "true"
    block = next_block(body)

    if status not in STATUSES:
        errors.append(f"status `{status}` is not one of {sorted(STATUSES)}; a dropped, merged "
                      "or superseded task is `cancelled`, finished work is `done`")
    if keys is not None:
        unknown = sorted(k for k in fm if k not in keys)
        if unknown:
            errors.append(f"frontmatter keys not in _system/frontmatter-properties.md: "
                          f"{', '.join(unknown)}. Register them there or remove them")

    if len(re.findall(r"^## Next action", body, re.M)) > 1:
        errors.append("more than one `## Next action` block; merge them into the top one")

    if status in ACTIVE:
        if not body.lstrip().startswith("## Next action"):
            errors.append("`## Next action` must come directly under the frontmatter, "
                          "before anything else")
        if not (NEXT_OPEN.search(block) or WAIT_OPEN.search(block)):
            if THEN_OPEN.search(block):
                errors.append("open `Then:` steps but no open `Next:`; promote the first "
                              "`Then:` to `Next:`")
            else:
                errors.append("no open `- [ ] Next:` or `- [ ] Waiting:` step. Either the task "
                              "is closed (`status: done`, or `cancelled` if dropped) or name "
                              "the next physical step")
        if len(NEXT_OPEN.findall(block)) > 1:
            errors.append("more than one open `Next:`; keep one, make the rest `Then:`")
        # Optional steps (Nov 11) never become the Next on their own: an optional Next with a
        # non-optional step still open is the wrong one on top.
        from tools import steps as S
        nxt = re.search(r"^- \[ \] Next:.*$", block, re.M)
        _opt = lambda l: bool((S.parse_line(l) or S.Step("open")).optional)  # noqa: E731
        if nxt and _opt(nxt.group(0)) and any(not _opt(l) for l in re.findall(r"^- \[ \] Then:.*$", block, re.M)):
            errors.append("the open `Next:` is `(optional)` while a step that is not optional is open; "
                          "make that step the Next and the optional one a `Then:`")

    # The Definition of Done is the block's first line (owner decision, Nov 1): `DoD: …` when he
    # confirmed it, `DoD (Claude's guess): …` until then.
    dods = [l for l in block.splitlines() if re.match(r"^\W*(dod|definition of done)\b", l, re.I)]
    if dods:
        first = next((l for l in block.splitlines() if l.strip()), "")
        if len(dods) > 1:
            errors.append("more than one DoD line in `## Next action`; keep one")
        elif not DOD.match(dods[0]):
            errors.append(f"DoD line must read `DoD: <text>` or `DoD (Claude's guess): <text>`, "
                          f"plain, no bullet; found {dods[0].strip()[:80]!r}")
        elif first != dods[0]:
            errors.append("the DoD line must be the first line of `## Next action`, above the "
                          "open `Next:`")

    # Steps live in `## Next action` only (Nov 11, the HR `## Steps` checklist Carmen half-parsed):
    # the rule is steps.stray_boxes, which also names the one form note that may have boxes elsewhere.
    from tools import steps as S
    stray = S.stray_boxes(text)
    if stray:
        errors.append(f"{len(stray)} open checkbox(es) outside `## Next action`, which Carmen and planning "
                      f"never read as steps. Move them into the block as `- [ ] Then:` lines, or make them "
                      f"plain bullets if they are notes; found {stray[0][:80]!r}")
    # The form of each step line (estimate, deadline, dates in the words, links): one rule each,
    # all in tools/steps.form_problems (Nov 11, the owner: "should there not be a single rule"). The
    # field-form checks on every step line; the rest on open steps of an open task.
    from tools import steps as S
    lines = block.splitlines()
    once = set()
    for n, line in enumerate(lines):
        if not re.match(r"^- (?:\[[ xX]\]|~~)", line):
            continue
        for pr in S.form_problems(line, S.workspace_of(lines, n), date.fromisoformat(today), status not in CLOSED):
            if pr["code"] in ("est_form", "due_old", "due_words"):
                if pr["code"] in once:
                    continue
                once.add(pr["code"])
            errors.append(f"{pr['why']} Found {line.strip()[:80]!r}")
    # A step deadline after the task's own due date (Nov 10): one of the two dates is wrong.
    tdue = fm.get("due", "")
    if status not in CLOSED and re.fullmatch(r"\d{4}-\d{2}-\d{2}", tdue):
        late = [d for d in re.findall(r"^- \[ \] (?:Next|Then):.*?📅\s*(\d{4}-\d{2}-\d{2})", block, re.M)
                if d > tdue]
        if late:
            questions.append(f"a step is due {max(late)}, after the task's `due: {tdue}`: move the "
                             "task's due, or the step's 📅")

    if status not in CLOSED:
        open_wait = bool(WAIT_OPEN.search(block))
        # One rule (Nov 10, waits per step; Nov 11 audit A1): waiting only when a Waiting line is
        # open and no step of his is free. tools/steps.task_waiting decides; steps.write keeps
        # `waiting:` as its cache, so the hook and the writer can no longer undo each other.
        from tools import steps as S
        want = S.task_waiting(S.steps_of(body))
        if waiting and not want:
            errors.append("`waiting: true`, but " + ("no `- [ ] Waiting: <who> · for: <the step>` line is open"
                          if not open_wait else "a step of yours is free (no open `after:` holds it)")
                          + ". Remove `waiting:` (a write sets it), or link the steps that need the answer "
                          "with `(after: ^<waiting id>)`")
        if want and not waiting:
            errors.append("every open step of yours waits on an open `- [ ] Waiting:` line, so the task is "
                          "waiting: set `waiting: true` (a write sets it), or tick the line if the reply came")
        sch = fm.get("scheduled", "")
        if waiting and sch and sch < today:
            questions.append(f"waiting, but `scheduled` ({sch}) is past: set the date you "
                             "will chase if no reply has come")

    heads = re.findall(r"^## .*$", body, re.M)
    if "## Log" in [h.strip() for h in heads]:
        if heads[-1].strip() != "## Log":
            errors.append("`## Log` must be the last section of the body")
        tail = log_tail(body)
        bad = [l for l in tail.splitlines() if l.strip() and not LOG_LINE.match(l)]
        if bad:
            errors.append(f"log lines must read `- Oct 5 · answer · finding`; "
                          f"first bad line: {bad[0][:80]!r}")

    # completedDate: TaskNotes writes it as YYYY-MM-DD when a task reaches any isCompleted
    # status (done and cancelled) and clears it on reopen (fieldMapping.completedDate in
    # its data.json). Sessions may set it, in exactly that form, so a hand close looks the
    # same as a click (owner decision, 2026-10-07).
    cd = fm.get("completedDate", "")
    if status in CLOSED and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cd):
        errors.append(f"`status: {status}` needs `completedDate: YYYY-MM-DD`, the form TaskNotes "
                      "writes: the day it was closed if the note or its log says so, else "
                      "today. Never guess an earlier date" if not cd else
                      f"`completedDate: {cd}` must be plain YYYY-MM-DD, the form TaskNotes writes")
    if status not in CLOSED and cd:
        errors.append("`completedDate` on a task that is not closed; remove it (TaskNotes clears "
                      "it on reopen)")

    if status == "done" and NEXT_OPEN.search(block):
        questions.append("done, but a `Next:` step is still open: tick it, or say it is moot")
    if status == "cancelled" and not re.search(r"^## Log", body, re.M):
        questions.append("cancelled without a `## Log` line saying why (dropped, merged into "
                         "what, superseded by what)")

    # Questions keyed to what this edit changed. Write gives no before-state,
    # so these fire for Edit/MultiEdit only.
    if edit:
        old, new = edit.get("old", ""), edit.get("new", "")
        newly_ticked = [l for l in re.findall(r"^- \[x\] (.*)$", new, re.M | re.I)
                        if f"- [x] {l}" not in old]
        if status not in CLOSED and not waiting and any(OUTWARD.search(l) for l in newly_ticked):
            questions.append("a step that hands the ball to someone was just ticked "
                             f"({newly_ticked[0][:60]!r}). If you are now waiting on them, "
                             "add `- [ ] Waiting: <who> · for: <the step>` and link the steps that need the "
                             "answer with `(after: ^<its id>)`")
        if status in ACTIVE and newly_ticked and not re.search(r"^- \[ \] ", block, re.M):
            questions.append("every step is ticked: is the task done?")
        old_dod = re.findall(r"^DoD: .*$", old, re.M)
        if old_dod and old_dod[0] not in new:
            questions.append("a DoD the owner confirmed was just changed or removed. Only he "
                             "changes a confirmed DoD: did he ask for this?")
        closed = bool(re.search(r"^status:\s*(done|cancelled)\b", new, re.M)) \
            and not re.search(r"^status:\s*(done|cancelled)\b", old, re.M)
        sch_new = re.findall(r"^scheduled:.*$", new, re.M)
        moved = bool(sch_new) and sch_new != re.findall(r"^scheduled:.*$", old, re.M)
        if closed or moved:
            questions.append("status or scheduled changed: find this task's future blocks on the Focus and "
                             "main calendars by its task_id first (`vt taskids \"<name>\"` gives it; blocks carry "
                             "`Task: ... (t-xxxxxx)`), by its encoded filename only for blocks older than Nov 3, "
                             "and delete or move them (SYSTEM.md § Closing a task closes its calendar blocks)")
        # Every session that changes a task's state logs it, not only plan-today
        # (owner decision, 2026-10-07): the next morning's guess starts from the last line.
        added_steps = [l for l in re.findall(r"^- \[[ x]\] .*$", new, re.M | re.I)
                       if l not in old]
        fields = r"^(status|waiting|scheduled|due):.*$"
        changed_fields = re.findall(fields, new, re.M) != re.findall(fields, old, re.M) \
            and bool(re.search(fields, new, re.M))
        if added_steps or changed_fields:
            label = f"{date.fromisoformat(today):%b} {date.fromisoformat(today).day}"
            tail = log_tail(body)
            if not re.search(rf"^- {label} ·", tail, re.M):
                errors.append(f"this edit changed the task's state but `## Log` has no line for "
                              f"today. Add `- {label} · <what changed> · <source>` under "
                              "`## Log` at the very bottom (create the section if missing)")
        if CTX_START in body and new:
            s = body.find(CTX_START)
            e = body.find(CTX_END, s)
            span = body[s:e if e != -1 else len(body)]
            if new.strip() and new.strip() in span or CTX_START in old or CTX_END in old:
                errors.append("this edit touched `## Current context`, which the plan-today "
                              "pre-run owns and replaces each morning. Revert it; durable facts "
                              "go elsewhere in the note")
    return errors, questions


def _edit_parts(tool_input: dict) -> dict | None:
    if "edits" in tool_input:  # MultiEdit
        es = tool_input.get("edits") or []
        return {"old": "\n".join(e.get("old_string", "") for e in es),
                "new": "\n".join(e.get("new_string", "") for e in es)}
    if "old_string" in tool_input:
        return {"old": tool_input.get("old_string", ""), "new": tool_input.get("new_string", "")}
    return None


_STEP_CMDS = ("`vt step add|done|discard|wait|link|date|reword|ws|move <task> ...` (`vt step --help`; "
              "`vt step list <task>` shows the ids)")


def _touches_block(text: str, new: str, old: str) -> bool:
    """Whether an edit lands on a step or link line inside `## Next action`: judged by the file's
    own lines where the new text sits, so an edit of a few words inside a step line counts too."""
    from tools import steps as S
    lines = text.split("\n")
    r = S.block_range(lines)
    if not r:
        return False

    def stepish(line):
        return bool(S.LINE.match(line) or re.match(r"^\s{2,}- ", line))
    if new and new in text:
        start = text[:text.index(new)].count("\n")
        end = start + new.rstrip("\n").count("\n")   # a trailing newline does not reach the next line
        return any(r[0] < k < r[1] and stepish(lines[k]) for k in range(start, end + 1))
    # A deletion: judged by what it removed.
    return any(stepish(l) for l in (old or "").split("\n"))



# D-11
def _raw_block_edit(tool: str, tool_input: dict, text: str, path: Path):
    """(message, the note restored or None) when a Claude edit changed the steps by hand."""
    from tools import steps as S
    if S.is_template(path) or not S.is_task(text) or any(
            re.search(rf"(?m)^\s*-\s*{t}\s*$|^tags:.*\b{t}\b", text.partition("\n---\n")[0]) for t in S.FORM_NOTE_TAGS):
        return None
    msg = (f"{path.name}: steps in `## Next action` change only through {_STEP_CMDS}, so their ids, "
           "waits, dates and links stay right (Nov 11). ")
    if tool in ("Edit", "MultiEdit"):
        edits = tool_input.get("edits") if tool == "MultiEdit" else [tool_input]
        if not any(_touches_block(text, e.get("new_string", ""), e.get("old_string", "")) for e in edits or []):
            return None
        restored = text
        for e in reversed(edits or []):
            new, old = e.get("new_string", ""), e.get("old_string", "")
            if new and restored.count(new) == 1:
                restored = restored.replace(new, old, 1)
            else:
                return msg + "Undo this edit and make the change with the command.", None
        return msg + "Your edit was put back; make the change with the command.", restored
    if tool == "Write":
        snap = (S.load_snap(S._task_key(text, path.stem)) or {}).get("steps")
        if snap is None:
            return None   # a new note: its first steps are written whole
        changed = S.diff(snap, S._snap(S.steps_of(text.partition("\n---\n")[2])))
        if changed:
            return msg + "This Write changed steps by hand: undo that part and use the command.", None
    return None


def _hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never break the session over the hook itself
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or (payload.get("tool_response") or {}).get("filePath")
    if not raw:
        return 0
    path = Path(raw).expanduser()
    if path.suffix != ".md" or path.parent.name != "Inbox" or not path.exists():
        return 0
    text = path.read_text(errors="ignore")
    if not is_task(text):
        return 0
    # Steps change only through `vt step` (Nov 11, item 15): an edit that changes step or link lines
    # inside `## Next action` is put back, with the command to use. The owner's own Obsidian edits never
    # reach this hook; the step writer reads them as "outside" edits.
    refused = _raw_block_edit(payload.get("tool_name", ""), tool_input, text, path)
    if refused:
        msg, restored = refused
        if restored is not None:
            from tools import steps as S
            S.replace_if(path, text, restored)   # D-30: only while the note is still the edit it undoes
        print(msg, file=sys.stderr)
        return 2
    # Every step gets its ^sxxxx id right after any Claude edit, whoever made it (owner decision, Nov 5:
    # "steps should always have IDs, independently from where they are changed from"). Only ids
    # are added (tools/steps.py normalize, the 08:00 sweep's function); a layer on top, so a
    # failure never breaks the session.
    try:
        from tools import steps as S
        fixed = text if S.is_template(path) else S.normalize(text)   # a template gets no ids (Nov 6)
        if fixed != text:
            text = S.write(path, text, "hook", before=text, redo=lambda cur: cur)   # D-30
    except Exception:
        pass
    errors, questions = check(text, registered_keys(find_vault(path)),
                              edit=_edit_parts(tool_input))
    if errors:
        print(f"{path.name} breaks the task schema (SYSTEM.md § Capture note schema). Fix it "
              "now, even if the problem predates this edit:", file=sys.stderr)
        for p in errors:
            print(f"  - {p}", file=sys.stderr)
        if questions:
            print("Also decide:", file=sys.stderr)
            for q in questions:
                print(f"  - {q}", file=sys.stderr)
        return 2
    if questions:
        msg = f"Task check on {path.name}. Decide each, act if it applies, otherwise move on:\n" \
              + "\n".join(f"- {q}" for q in questions)
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg}}))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--hook"]:
        return _hook()
    vault = find_vault()
    if argv:
        paths = [Path(a).expanduser().resolve() for a in argv]
    elif vault is None:
        print("vt tasks: no vault found", file=sys.stderr)
        return 2
    else:
        paths = sorted((vault / "Inbox").glob("*.md"))
    keys = registered_keys(find_vault(paths[0]) if paths else vault)
    bad = 0
    for path in paths:
        text = path.read_text(errors="ignore")
        if not is_task(text):
            continue
        errors, questions = check(text, keys)
        if errors or questions:
            bad += bool(errors)
            print(path.name)
            for p in errors:
                print(f"  ✗ {p}")
            for q in questions:
                print(f"  ? {q}")
    if not argv:
        print(f"\n{bad} task(s) with errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
