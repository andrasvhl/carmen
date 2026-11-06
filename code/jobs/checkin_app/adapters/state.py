"""Carmen's day state: one way to read and change its JSON files (Nov 6, refactor Phase 1).

Before this, about fifteen files under STATE_DIR/checkin_app were each written by
load-modify-save with no lock, from the server's tap thread, its view thread, the chat's event
loop and the prep, Refresh and pre-run processes, all through the same `<file>.tmp`. Two writers
at once lost one change, or left a torn file.

Now every change goes through update(): an exclusive fcntl lock per file (held across
processes), the file read fresh under the lock, the caller's function applied, the result
written through a tmp name unique to this process and thread, then os.replace. read() never
locks and never writes. Files stay plain JSON, so the pre-run, the Mac panel and a person can
still read them.

PERSIST: the view is a read. Writes it used to make while drawing (the day's roll-over, the
"dated" list, resolved chunk ids, frozen rank keys, the step history's outside edits) happen only
when the caller sets persist(), which the server does for taps and for its minute tick, never
for GET /api/view or for prep and Refresh building rows.
"""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import os
import threading
from pathlib import Path

PERSIST = contextvars.ContextVar("carmen_persist", default=False)


@contextlib.contextmanager
def persist(on: bool = True):
    """Let view building write its bookkeeping (taps and the server's tick only)."""
    tok = PERSIST.set(on)
    try:
        yield
    finally:
        PERSIST.reset(tok)


def persisting() -> bool:
    return PERSIST.get()


def _lock_path(p: Path) -> Path:
    return p.with_name(p.name + ".lock")


@contextlib.contextmanager
def locked(p: Path):
    """The file's exclusive lock, across processes. Reentrant per thread is NOT supported:
    never call update() on the same file from inside its own fn."""
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(p), "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def read(p: Path, empty):
    """The file's JSON, or a fresh `empty` (same type) when missing, torn or the wrong type."""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return type(empty)() if isinstance(empty, (dict, list)) else empty
    if isinstance(empty, (dict, list)) and not isinstance(d, type(empty)):
        return type(empty)()
    return d


def _write(p: Path, d, indent=1) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=indent), encoding="utf-8")
    os.replace(tmp, p)


def update(p: Path, fn, empty, indent=1):
    """Change the file under its lock: fn(d) mutates d in place or returns the new value.
    Returns the value written."""
    with locked(p):
        d = read(p, empty)
        out = fn(d)
        d = d if out is None else out
        _write(p, d, indent)
        return d


def write(p: Path, d, indent=1) -> None:
    """Replace the whole file under its lock."""
    with locked(p):
        _write(p, d, indent)


def _load_json(p: Path, empty):
    return read(p, empty)


def _save_json(p: Path, d) -> None:
    write(p, d)
