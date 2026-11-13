"""A renamed task keeps everything Carmen knows about it (Nov 6, refactor Phase 2).

Carmen keys its day state by the note's file name: Do's list and chunks, the selection, frozen
ranks, part ticks, undo, dismissals, prep state, the notes it made, and each task's chat. A rename
(`vt move`, Obsidian, `add_task` shortening a title) used to orphan all of it: the task fell off
Do, lost its place and its chat. The note's `task_id` (t-xxxxxx) never changes, so the server's
tick compares {task_id: file name} with the last one it saw and moves every key from the old name
to the new one (migrate). Nothing here runs on a page load.
"""
from __future__ import annotations

import hashlib
import json

from runner import config

from .adapters import state

INDEX = config.STATE_DIR / "checkin_app" / "task_index.json"


def _swap_list(xs, old, new):
    return [new if x == old else x for x in xs] if isinstance(xs, list) else xs


def _swap_keys(d, old, new):
    if isinstance(d, dict) and old in d:
        if new not in d:
            d[new] = d[old]
        d.pop(old, None)
    return d


def migrate(old: str, new: str, log=None) -> list[str]:
    """Move every state key from note name `old` to `new`. Returns what changed."""
    from .direction.read import NORAISE
    from .planning.read import OVERRIDES, PICK_FROZEN
    from .tasks.actions import STEP_UNDO, SUBTICKS
    from .tasks.read import PROP_IGNORED
    from . import chat, prep
    from .planning import do
    from .tasks import cardnotes
    done = []

    def do_fn(d):
        for e in (d.get("days") or {}).values():
            if not isinstance(e, dict):
                continue
            for k in ("ids", "dropped", "dismissed", "dated", "part_closed"):
                e[k] = _swap_list(e.get(k), old, new)
            for k in ("carried", "kept", "chunks", "chunk_ids", "claude_ticks", "claude_moved", "moved_off", "extra", "his_ticks", "claude_changes"):
                _swap_keys(e.get(k), old, new)
    state.update(do.STATE, do_fn, {})
    done.append("do")

    def ov_fn(ov):
        for kind, days in ov.items():
            if not isinstance(days, dict):
                continue
            for day, v in list(days.items()):
                days[day] = new if v == old else _swap_list(v, old, new)
    state.update(OVERRIDES, ov_fn, {})

    def frozen_fn(d):
        for v in d.values():
            _swap_keys(v, old, new)
    state.update(PICK_FROZEN, frozen_fn, {}, indent=None)
    for p in (SUBTICKS, STEP_UNDO, NORAISE, PROP_IGNORED):
        state.update(p, lambda d: _swap_keys(d, old, new), {})
    done.append("state files")

    with cardnotes._locked():   # the notes Carmen made, keyed by file name
        m = cardnotes.load()
        if old in m and new not in m:
            m[new] = m.pop(old)
            cardnotes._save(m)
            done.append("made")

    st = prep.load_state(old)   # prep state: a file per task, named by a hash of its name
    if st and not prep.load_state(new):
        prep.save_state({"id": new}, {**st, "id": new})
        try:
            (prep.STATE / f"{prep._key({'id': old})}.json").unlink()
        except OSError:
            pass
        done.append("prep")

    old_key, new_key = f"task:{old}", f"task:{new}"   # the task's chat: history file and Claude session
    src, dst = chat.conv_path(old_key), chat.conv_path(new_key)
    if src.exists() and not dst.exists():
        conv = chat.load(old_key)
        conv["key"] = new_key
        chat.save(conv)
        src.unlink()
        done.append("chat history")
    try:
        import sqlite3
        with sqlite3.connect(chat.DB) as db:
            n = db.execute("UPDATE sessions SET name=? WHERE chat_id=? AND name=? AND NOT EXISTS "
                           "(SELECT 1 FROM sessions WHERE chat_id=? AND name=?)",
                           (new_key, chat.APP_CHAT, old_key, chat.APP_CHAT, new_key)).rowcount
            db.execute("UPDATE lanes SET current_name=? WHERE chat_id=? AND current_name=?",
                       (new_key, chat.APP_CHAT, old_key))
        if n:
            done.append("chat session")
    except Exception as exc:   # the history file moved; a lost session only starts a new transcript
        if log:
            log("error", where="rename chat session", detail=repr(exc)[:200])
    if log:
        log("task_renamed", old=old, new=new, moved=done)
    return done


def check(log=None) -> list[tuple[str, str]]:
    """Compare {task_id: file name} with the last pass and migrate every rename. Called by the
    server's tick (persisting). The first pass only records the index."""
    from tools import taskids
    now = taskids.index(config.INBOX)
    if not now:   # an empty or unreadable Inbox is never evidence of renames
        return []
    before = state.read(INDEX, {})
    moved = []
    names = set(now.values())
    for tid, stem in now.items():
        was = before.get(tid)
        if was and was != stem and was not in names:
            migrate(was, stem, log)
            moved.append((was, stem))
    if now != before:
        state.write(INDEX, now, indent=None)
    return moved
