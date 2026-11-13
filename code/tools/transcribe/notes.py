"""Write a transcript into the `> [!quote]- Transkript` callout of a vault note.

The callout is the format the Push Up Yourself notes already use, created by
the August crawl and left empty. This module is the thing that fills them, and
it is deliberately narrow: it finds one callout under one heading and replaces
its body. It never creates headings, never reorders, never touches anything
else in the file.

Refusing to overwrite a callout that already has content is the whole safety
model. A transcript is cheap to regenerate; a paragraph the owner typed into the
wrong callout by hand is not.
"""

import re
from dataclasses import dataclass
from pathlib import Path

CALLOUT = re.compile(r"^>\s*\[!quote\]-?\s*Transkript\s*$", re.IGNORECASE)
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

# Obsidian strips these from a heading's rendered text; matching on the raw
# line would fail on `## Lektion 1 — Herzlich Willkommen  ⬜` when the caller
# reasonably wrote the heading without its status box.
NOISE = re.compile(r"[⬜✅☑️✔️\s]+")


class NoteError(RuntimeError):
    pass


class Ambiguous(NoteError):
    """A heading fragment that matches more than one heading in the note.

    Its own class because the run output should distinguish "you named a
    heading that is not there" from "you named one too loosely" — the fixes
    are different and the second is the one that silently fills the wrong
    lesson if it is ever allowed to guess.
    """ 


@dataclass
class Fill:
    note: Path
    heading: str
    status: str          # written | already-filled | no-callout | no-heading | ambiguous
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "written"


def _norm(text: str) -> str:
    return NOISE.sub(" ", text).strip().casefold()


def _find_heading(lines: list[str], wanted: str) -> int:
    """Exact-ish match first, substring second. Ambiguity is an error, not a guess."""
    target = _norm(wanted)
    exact, partial = [], []
    for i, line in enumerate(lines):
        found = HEADING.match(line)
        if not found:
            continue
        text = _norm(found.group(2))
        if text == target:
            exact.append(i)
        elif target and target in text:
            partial.append(i)
    hits = exact or partial
    if not hits:
        raise NoteError(f"heading not found: {wanted!r}")
    if len(hits) > 1:
        raise Ambiguous(f"heading {wanted!r} matches {len(hits)} headings; be more specific")
    return hits[0]


def _section_end(lines: list[str], start: int) -> int:
    """Where this heading's section stops: the next heading of equal or higher rank."""
    level = len(HEADING.match(lines[start]).group(1))
    for i in range(start + 1, len(lines)):
        found = HEADING.match(lines[i])
        if found and len(found.group(1)) <= level:
            return i
    return len(lines)


def _callout_body(lines: list[str], head: int) -> tuple[int, int]:
    """Half-open range of the callout's body lines, i.e. the `>` lines after it."""
    end = head + 1
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    return head + 1, end


def _body_text(lines: list[str]) -> str:
    return "\n".join(re.sub(r"^>\s?", "", ln) for ln in lines).strip()


def quote(text: str) -> list[str]:
    """Blockquote the transcript. A blank line inside a callout must still be `>`."""
    return [f"> {ln}" if ln.strip() else ">" for ln in text.splitlines()]


def fill(note: Path, heading: str, text: str, *, force: bool = False,
         dry_run: bool = False) -> Fill:
    if not note.is_file():
        raise NoteError(f"no such note: {note}")
    lines = note.read_text(encoding="utf-8").splitlines()

    try:
        start = _find_heading(lines, heading)
    except Ambiguous as exc:
        return Fill(note, heading, "ambiguous", str(exc))
    except NoteError as exc:
        return Fill(note, heading, "no-heading", str(exc))

    stop = _section_end(lines, start)
    head = next((i for i in range(start, stop) if CALLOUT.match(lines[i].strip())), None)
    if head is None:
        return Fill(note, heading, "no-callout",
                    "no `> [!quote]- Transkript` callout under this heading")

    body_start, body_end = _callout_body(lines, head)
    existing = _body_text(lines[body_start:body_end])
    if existing and not force:
        return Fill(note, heading, "already-filled",
                    f"{len(existing)} chars already there; --force to replace")

    if dry_run:
        return Fill(note, heading, "written", f"would write {len(text)} chars (dry run)")

    lines[body_start:body_end] = quote(text)
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Fill(note, heading, "written", f"{len(text)} chars")


def empty_callouts(note: Path) -> list[str]:
    """Headings in this note whose Transkript callout is still empty."""
    lines = note.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines):
        if not CALLOUT.match(line.strip()):
            continue
        body_start, body_end = _callout_body(lines, i)
        if _body_text(lines[body_start:body_end]):
            continue
        for j in range(i, -1, -1):
            found = HEADING.match(lines[j])
            if found:
                out.append(found.group(2))
                break
    return out
