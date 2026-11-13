"""Teach flexplorer the order Canvas puts a course in.

The vault's convention is that file order is set in the plugin, never by
renaming files with numeric prefixes (`_system/flexplorer.md`). That rules out
the obvious approach, so the order has to be written into
`.obsidian/plugins/flexplorer/data.json` directly.

Two consequences worth knowing:

  - Obsidian must be reloaded (Cmd+R) afterwards, or the running plugin
    overwrites the file from memory. That is the plugin's own gotcha.
  - `.obsidian` does not sync to vault-host, but the "Installed community
    plugins" toggle DOES carry plugin `data.json` between the Mac and the
    iPhone, so both get the order. The box has no UI and does not care.

The order is read from each course's `Modules.md`, not from the tool's state
file: `Modules.md` is markdown and therefore syncs, so this runs on whichever
machine has Obsidian rather than only on the one that ran the pull.
"""

import datetime as dt
import json
import re
import shutil
from pathlib import Path

from typing import Any

from runner.config import VAULT

from .layout import course_dirs, group_key, is_group, is_status

DATA = VAULT / ".obsidian" / "plugins" / "flexplorer" / "data.json"

# Order the kind folders inside a session the way the session runs: read the
# page, look at the deck, do the assignment.
KIND_ORDER = ["Pages", "Readings", "Materials", "Assignments", "Quizzes",
              "Discussions", "Announcements"]

TERMS = {"w": 0, "sp": 1, "su": 2, "f": 3}       # within an academic year
UNFILED_DIR = "_Unfiled"

# A module folder that starts with a number is a session; anything else is
# admin (Course Information, Canvas Help for Students, Teaching Team
# Resources). Canvas lists admin first, which pushes the actual sessions down
# the sidebar, so it goes last here.
NUMBERED = re.compile(r"^\d+\b")
HEADING = re.compile(r"^## (.+?)\s*$", re.M)
BULLET = re.compile(r"^\s*-\s+\[\[([^\]|]+)\|", re.M)
# A reading link, as the pull writes it: [[<course>/<module>/Readings/Name|text]]
READING_LINK = re.compile(r"\[\[[^\]|]*?/Readings/([^\]|#]+)")


def _reading_sequence(module_dir: Path, kind_order: list[str]) -> list[str]:
    """Readings in the order the session's own text assigns them.

    Alphabetical put "How to kickstart" second when the page lists it third.
    The folder note is read first because it is the session's main page, then
    the other kinds in the order they are shown.
    """
    seq: list[str] = []
    notes: list[Path] = []
    folder_note = module_dir / f"{module_dir.name}.md"
    if folder_note.exists():
        notes.append(folder_note)
    for kind in kind_order:
        sub = module_dir / kind
        if kind != "Readings" and sub.is_dir():
            notes.extend(sorted(sub.glob("*.md")))
    for note in notes:
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in READING_LINK.findall(text):
            fn = f"{name}.md"
            if fn not in seq:
                seq.append(fn)
    return seq


def term_key(folder: str) -> tuple:
    """Sort F25 before W26 before Sp26, i.e. the order they were taken."""
    m = re.match(r"^(F|W|Sp|Su)(\d{2})-", folder, re.I)
    if not m:
        return (99, 99, folder.lower())
    term, yy = m.group(1).lower(), int(m.group(2))
    # An autumn term belongs to the academic year that ENDS the following June.
    year = yy + 1 if term == "f" else yy
    return (year, TERMS.get(term, 9), folder.lower())


def natural_key(name: str) -> list:
    """Sort "2 - X" before "10 - Y". Plain string order is the fallback for
    anything `Modules.md` does not place, and it put session 10 after 1 and
    before 2 - which is what a folder looks like whenever that fallback runs.
    """
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
            for t in re.findall(r"\d+|\D+", name)]


def _children(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if not p.name.startswith("."))


def _ordered(actual: list[str], preferred: list[str]) -> list[str]:
    """`preferred` first, in order; anything else appended alphabetically."""
    known = [n for n in preferred if n in actual]
    return known + sorted((n for n in actual if n not in known), key=natural_key)


def plan_orders(archive: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Returns (path -> customOrder, anchor notes to pin)."""
    orders: dict[str, list[str]] = {}
    pins: list[str] = []
    if not archive.is_dir():
        return orders, pins

    courses = list(course_dirs(archive))
    # Top level: quarters newest first, Program after them, then any course
    # still sitting flat from before the regroup, then the binaries.
    top = [d.name for d in archive.iterdir() if d.is_dir() and not d.name.startswith(".")]
    groups = sorted((n for n in top if is_group(n)), key=group_key)
    flat = sorted((n for n in top if not is_group(n) and n != "_files"), key=term_key)
    orders[str(archive.relative_to(VAULT))] = \
        groups + flat + (["_files"] if "_files" in top else [])
    for g in groups:
        # A quarter split by enrolment shows Enrolled, then Waitlisted, each
        # holding its courses; any course not (yet) moved into one sorts after.
        kids = _children(archive / g)
        splits = sorted((k for k in kids if is_status(k)), key=group_key)
        orders[str((archive / g).relative_to(VAULT))] = splits + sorted(
            (k for k in kids if not is_status(k)), key=term_key)
        for st in splits:
            orders[str((archive / g / st).relative_to(VAULT))] = sorted(
                _children(archive / g / st), key=term_key)

    for course in courses:
        modules_md = course / "Modules.md"
        module_names: list[str] = []
        item_order: dict[str, list[str]] = {}
        if modules_md.exists():
            text = modules_md.read_text(encoding="utf-8", errors="replace")
            module_names = HEADING.findall(text)
            # Bullets carry vault-relative note paths in Canvas order, which is
            # exactly the order the files should sit in inside their folder.
            for link in BULLET.findall(text):
                parent, _, name = link.rpartition("/")
                item_order.setdefault(parent, []).append(name + ".md")

        # The headings ARE the folder names - `_write_modules` writes them
        # through the same `module_dir` the pull uses to create the folder. This
        # used to re-derive them here with `module_dir(heading)`, which was
        # wrong twice over: it applied the stripping a second time to an
        # already-stripped name, and it needs the course's alias map, which
        # lives in state on the box while this command runs on the Mac. Reading
        # the folder name straight out of the spine is both correct and the only
        # thing that works from a machine with no state.
        wanted = list(module_names)
        actual = _children(course)
        head = [f"{course.name}.md", "Modules.md", "Syllabus.md"]
        tail = ["_Unfiled", "_sync-log.md"]
        present = [w for w in wanted if w in actual]
        sessions = [w for w in present if NUMBERED.match(w)]
        admin = [w for w in present if not NUMBERED.match(w)]
        # Folders that are in no module at all: files Canvas filed under one of
        # its own Files folders rather than a module. They belong with the
        # admin material, not after `_sync-log.md`.
        placed = set(head + present + tail)
        extras = sorted((n for n in actual if n not in placed), key=natural_key)
        orders[str(course.relative_to(VAULT))] = _ordered(
            actual, [h for h in head if h in actual]
            + sessions + admin + extras
            + [t for t in tail if t in actual])
        pins.append(str((course / f"{course.name}.md").relative_to(VAULT)))

        for module_dir in (d for d in course.iterdir() if d.is_dir()):
            note = f"{module_dir.name}.md"
            orders[str(module_dir.relative_to(VAULT))] = _ordered(
                _children(module_dir), [note] + KIND_ORDER)
            if (module_dir / note).exists():
                pins.append(str((module_dir / note).relative_to(VAULT)))
            for kind_dir in (d for d in module_dir.iterdir() if d.is_dir()):
                rel = str(kind_dir.relative_to(VAULT))
                preferred = (_reading_sequence(module_dir, KIND_ORDER)
                             if kind_dir.name == "Readings"
                             else item_order.get(rel, []))
                orders[rel] = _ordered(_children(kind_dir), preferred)

        # The binaries mirror the same module structure, so give it the same
        # order. Without this it sits in whatever sequence Sync delivered.
        binaries = archive / "_files" / course.name
        if binaries.is_dir():
            orders[str(binaries.relative_to(VAULT))] = _ordered(
                _children(binaries), sessions + admin + extras + [UNFILED_DIR])
    return orders, pins


def apply(orders: dict[str, list[str]], pins: list[str], *,
          dry_run: bool = False) -> dict[str, Any]:
    """Merge into flexplorer's data.json. Never replaces what it did not write."""
    if not DATA.exists():
        return {"error": f"no flexplorer config at {DATA}. "
                         "Run this on a machine with Obsidian, not vault-host."}
    data = json.loads(DATA.read_text(encoding="utf-8"))
    items = data.setdefault("items", {})
    pinned = data.setdefault("pinnedFiles", [])

    changed = 0
    for path, order in orders.items():
        entry = items.setdefault(path, {"isPinned": False, "isHidden": False})
        if entry.get("customOrder") != order or entry.get("sortOrder") != "custom":
            changed += 1
        entry["sortOrder"] = "custom"
        entry["customOrder"] = order
    for pin in pins:
        entry = items.setdefault(pin, {"isPinned": False, "isHidden": False})
        entry["isPinned"] = True
        if pin not in pinned:
            pinned.append(pin)

    result = {"folders": len(orders), "changed": changed, "pinned": len(pins),
              "total_entries": len(items), "backup": None}
    if dry_run:
        return result

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = DATA.with_name(f"data.json.bak-{stamp}")
    shutil.copy2(DATA, backup)
    tmp = DATA.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(DATA)
    result["backup"] = str(backup)
    return result
