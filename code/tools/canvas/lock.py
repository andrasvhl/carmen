"""One lock for everything that writes Canvas archive state.

`canvas.json` is rewritten whole by every command and has no lock of its own,
so two writers silently lose whichever saved first - a `pull` in one process
discards an `adopt` in another. That was a convention ("check nothing else is
running") until the daily `canvas_tasks` job made a second, unattended writer
a certainty (2026-09-23). Every writing `vt canvas` command and the job take
this lock; whoever finds it held refuses rather than waits, and says who has it.

Re-entrant within a process, because `vt canvas refresh` runs pull, extract and
links by calling `main()` again, and the job calls refresh.
"""

import contextlib
import fcntl
import os
from typing import Iterator

from runner import config

LOCK_FILE = config.LOCK_DIR / "canvas.lock"

_depth = 0
_handle = None


class Busy(Exception):
    """Another process holds the Canvas lock."""


def holder() -> str:
    """Who holds the lock, as recorded when it was taken ("" if unknown)."""
    try:
        return LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@contextlib.contextmanager
def canvas_lock(who: str) -> Iterator[None]:
    """Hold the Canvas lock for the duration. Raises Busy if it is held elsewhere."""
    global _depth, _handle
    if _depth:
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    h = LOCK_FILE.open("a+")
    try:
        fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        h.close()
        raise Busy(holder() or "another process")
    h.seek(0)
    h.truncate()
    h.write(f"{who} (pid {os.getpid()})\n")
    h.flush()
    _handle, _depth = h, 1
    try:
        yield
    finally:
        _depth = 0
        _handle = None
        fcntl.flock(h, fcntl.LOCK_UN)
        h.close()
