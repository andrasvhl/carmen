"""Stable task IDs: every `type: action` note in Inbox/ carries `task_id: t-xxxxxx`.

Calendar blocks and the check-in app join on this key, never on a task's words, because a
title changes and words overlap (owner decision, Nov 3: "name matching is stupid for tasks we probably
need key matching or identifiers"). A block names its tasks as
`Task: obsidian://open?vault=<VAULT_NAME>&file=<path> (t-xxxxxx)`.

    vt taskids                 give every task note without an ID one (--dry-run: only count)
    vt taskids <note name>...  print "<id>\t<name>" per note, creating a missing ID first;
                               what an agent runs before it books a block for a task

The ID goes on the line after `type: action`. Only that one line is written; nothing else in
the note changes. A copied note that duplicates another's ID gets a new one (the newer file).
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from pathlib import Path

from runner import config

ID_RE = re.compile(r"^t-[0-9a-f]{6}$")
FM_ID = re.compile(r"^task_id:\s*(\S+)\s*$", re.M)


EVENT_KEY_RE = re.compile(r"^ev-[0-9a-f]{8}$")


def event_key(uid: str) -> str:
    """A calendar event's short stable key, `ev-` and 8 hex of its iCalUID (Nov 3). The
    iCalUID survives a move and is the same on every calendar the event shows on, also an
    imported one like Partiful; a task note points at its appointment with `event_key`."""
    import hashlib
    return "ev-" + hashlib.sha1((uid or "").encode()).hexdigest()[:8] if uid else ""


def _head(text: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    return text[4:end] if end != -1 else None


def is_task(text: str) -> bool:
    head = _head(text)
    return head is not None and re.search(r"^type:\s*action\s*$", head, re.M) is not None


def read_id(text: str) -> str:
    head = _head(text) or ""
    m = FM_ID.search(head)
    return m.group(1) if m and ID_RE.match(m.group(1)) else ""


def notes(inbox: Path | None = None) -> list[Path]:
    return sorted((inbox or config.INBOX).glob("*.md"))


def index(inbox: Path | None = None) -> dict[str, str]:
    """{task_id: note name} for every task note that has one."""
    out = {}
    for p in notes(inbox):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        i = read_id(text)
        if i and i not in out:
            out[i] = p.stem
    return out


def new_id(taken: set[str]) -> str:
    while True:
        i = "t-" + secrets.token_hex(3)
        if i not in taken:
            taken.add(i)
            return i


def _write_id(p: Path, text: str, tid: str) -> None:
    head = _head(text)
    assert head is not None
    if FM_ID.search(head):
        new_head = FM_ID.sub(f"task_id: {tid}", head, count=1)
    else:
        new_head = re.sub(r"^(type:\s*action\s*)$", rf"\1\ntask_id: {tid}", head, count=1, flags=re.M)
    tmp = p.with_name(f".{p.name}.taskid.tmp")
    tmp.write_text("---\n" + new_head + text[4 + len(head):], encoding="utf-8")
    os.replace(tmp, p)


def ensure(p: Path, taken: set[str] | None = None, seen: dict[str, Path] | None = None) -> str:
    """The note's ID, written first if it has none (or shares one with an older note)."""
    text = p.read_text(encoding="utf-8", errors="replace")
    if not is_task(text):
        return ""
    taken = taken if taken is not None else set(index().keys())
    tid = read_id(text)
    if tid and (seen is None or seen.get(tid) in (None, p)):
        if seen is not None:
            seen[tid] = p
        return tid
    tid = new_id(taken)
    _write_id(p, text, tid)
    if seen is not None:
        seen[tid] = p
    return tid


def sweep(dry_run: bool = False, inbox: Path | None = None) -> tuple[int, int]:
    """(notes given an ID, task notes in all). Oldest first, so a copy is the one renumbered."""
    paths = [p for p in notes(inbox) if is_task(p.read_text(encoding="utf-8", errors="replace"))]
    paths.sort(key=lambda p: p.stat().st_mtime)
    taken = {read_id(p.read_text(encoding="utf-8", errors="replace")) for p in paths} - {""}
    seen: dict[str, Path] = {}
    added = 0
    for p in paths:
        text = p.read_text(encoding="utf-8", errors="replace")
        tid = read_id(text)
        if tid and tid not in seen:
            seen[tid] = p
            continue
        added += 1
        if not dry_run:
            ensure(p, taken, seen)
    return added, len(paths)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vt taskids", description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="note names (Inbox/<name>.md, no .md)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.names:
        added, total = sweep(a.dry_run)
        print(f"taskids: {added} of {total} task notes {'need' if a.dry_run else 'given'} an ID")
        return 0
    taken, rc = set(index().keys()), 0
    for name in a.names:
        name = name[:-3] if name.endswith(".md") else name
        p = config.INBOX / f"{Path(name).name}.md"
        if not p.is_file():
            print(f"-\t{name}\t(no such note in Inbox)", file=sys.stderr)
            rc = 1
            continue
        tid = ensure(p, taken)
        if not tid:
            print(f"-\t{name}\t(not a type: action note)", file=sys.stderr)
            rc = 1
            continue
        print(f"{tid}\t{p.stem}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
