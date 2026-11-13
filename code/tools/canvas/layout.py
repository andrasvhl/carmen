"""Where a course's notes live: one folder per quarter, newest first.

    Course Archive/
      Fall 2026/<Course>/...
      Spring 2026/<Course>/...
      Program/<Program Orientation - Cohort A>/...
      _files/<Course>/...          binaries stay flat, keyed on the course alone

Adopted 2026-09-23 at the owner's request, so courses can be found by term. The
course folder's own name did not change, and it is still the course's identity
everywhere - state, `_files/`, reading notes, aliases, caches. Only the notes
folder gained a parent, and every path to it goes through `course_dir`.

The quarter comes from the term code already in every class's folder name
(`F26-`, `Sp26-`, `W26-`), not from state, because state holds no term and
`vt canvas order` runs on the Mac, which has no state at all. A site with no
term code - the program-wide Canvas sites - goes to `Program`.

Readers must also accept the old flat layout (`Course Archive/<Course>/`).
The move is one `vt move` per course, and a run between two of them must not
mistake a quarter folder for a course or a course for a quarter.

A quarter in which he is waitlisted for any course splits one level further
(2026-09-23): `Fall 2026/Enrolled/<Course>/` and `Fall 2026/Waitlisted/<Course>/`.
A quarter with no waitlisted course stays flat, which is every past quarter.
Whether a quarter splits is decided by the pull, from the Canvas enrolment
role (`archive.homes`), and passed in here as `group`; the path itself then
carries the status, so the Mac, which has no state, can still read it.

A third group, `Dropped/` (2026-09-28, the owner: "only keep the final list of
enrolled classes in Enrolled"), holds a current-quarter course he was enrolled
in or waitlisted for that Canvas no longer lists: a drop, or a waitlist he left.
Its archive is kept whole, only moved. A quarter with a dropped course splits
too, so the courses he is still taking stay under `Enrolled/`.
"""

import re
from pathlib import Path
from typing import Iterator

PROGRAM = "Program"
ENROLLED, WAITLISTED, DROPPED = "Enrolled", "Waitlisted", "Dropped"
# Enrolment status (as stored in state) -> the subfolder it lives in.
STATUS_DIRS = {"enrolled": ENROLLED, "waitlisted": WAITLISTED, "dropped": DROPPED}
_STATUS_RANK = {ENROLLED: 0, WAITLISTED: 1, DROPPED: 2}

# `F26-STRAMGT-394-01`, `Sp26-OIT-278-01-02`, `W26-OIT-108-02-04`. The
# department after the dash is what keeps `25F-MBA-Program` (a program site,
# code first) from reading as a class.
_TERM_CODE = re.compile(r"(?:^|[\s-])(F|W|Sp|Su)(\d{2})-[A-Z]{2,}")
_SEASON = {"W": "Winter", "Sp": "Spring", "Su": "Summer", "F": "Fall"}
# Stanford's academic year runs Fall, Winter, Spring, Summer, but within one
# calendar year the order is Winter, Spring, Summer, Fall - which is the one
# that sorts "Fall 2026" above "Spring 2026".
_SEASON_RANK = {"Winter": 0, "Spring": 1, "Summer": 2, "Fall": 3}
_QUARTER = re.compile(r"^(Winter|Spring|Summer|Fall) (\d{4})$")


def quarter_of(folder: str) -> str:
    """"Fall 2026" for a class folder, `PROGRAM` for anything without a term."""
    m = _TERM_CODE.search(folder or "")
    if not m:
        return PROGRAM
    return f"{_SEASON[m.group(1)]} 20{m.group(2)}"


def is_group(name: str) -> bool:
    """Is this top-level folder a quarter (or Program), rather than a course?"""
    return name == PROGRAM or bool(_QUARTER.match(name or ""))


def is_status(name: str) -> bool:
    """Is this folder inside a quarter an enrolment split, not a course?"""
    return name in _STATUS_RANK


def group_key(name: str) -> tuple:
    """Sort key: newest quarter first, Program after every quarter.

    Inside a split quarter it also orders the status folders: Enrolled, then
    Waitlisted, then Dropped.
    """
    if is_status(name):
        return (0, 0, _STATUS_RANK[name], name)
    m = _QUARTER.match(name or "")
    if not m:
        return (1, 0, 0, name)
    return (0, -int(m.group(2)), -_SEASON_RANK[m.group(1)], name)


def course_dir(root: Path, folder: str, group: str | None) -> Path:
    """The notes folder for a course, in the grouped layout.

    `group` is `ENROLLED`, `WAITLISTED`, `DROPPED`, or None for a quarter that is not
    split. Required, not defaulted: a writer that forgot it would silently
    build the unsplit path and the course would grow a second folder, which is
    the bug `module_dir` takes its alias map as a required argument to avoid.
    Readers that only have a folder name use `locate`.
    """
    if group is not None and not is_status(group):
        raise ValueError(f"not an enrolment group: {group!r}")
    base = Path(root) / quarter_of(folder)
    return (base / group / folder) if group else (base / folder)


def locate(root: Path, folder: str) -> Path:
    """Where a course's notes are now, in whichever layout they are in.

    Tries the quarter folder, then its Enrolled/, Waitlisted/ and Dropped/
    splits, then the old flat layout. Nothing found: the unsplit quarter home.
    """
    root = Path(root)
    q = root / quarter_of(folder)
    for d in (q / folder, q / ENROLLED / folder, q / WAITLISTED / folder,
              q / DROPPED / folder, root / folder):
        if d.is_dir():
            return d
    return q / folder


def group_of(course_path: Path) -> str | None:
    """The enrolment group a course folder sits in, or None if unsplit."""
    parent = Path(course_path).parent.name
    return parent if is_status(parent) else None


def course_of(parts: tuple[str, ...]) -> str | None:
    """The course a path inside the archive belongs to, in any layout."""
    if not parts:
        return None
    if is_group(parts[0]):
        if len(parts) > 1 and is_status(parts[1]):
            return parts[2] if len(parts) > 2 else None
        return parts[1] if len(parts) > 1 else None
    return parts[0]


def _subdirs(d: Path) -> list[Path]:
    return sorted(c for c in d.iterdir()
                  if c.is_dir() and not c.name.startswith((".", "_")))


def course_dirs(root: Path) -> Iterator[Path]:
    """Every course notes folder: flat, grouped, or split by enrolment."""
    root = Path(root)
    if not root.is_dir():
        return
    for d in _subdirs(root):
        if not is_group(d.name):
            yield d
            continue
        for c in _subdirs(d):
            if is_status(c.name):
                yield from _subdirs(c)
            else:
                yield c
