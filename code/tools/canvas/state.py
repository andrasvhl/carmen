"""What has already been archived, so a second run is nearly free.

Lives in STATE_DIR, not the vault: it is JSON, Obsidian Sync does not carry
JSON, and a state file two machines disagree about is worse than none. The
human-readable half is `_sync-log.md`, written into each course folder by
`render`, because markdown does sync.

Writes are atomic (temp file + os.replace). GOTCHAS.md records why: the Strava
sync corrupted its own credentials by dying mid-write, and the failure only
surfaced days later.
"""

import datetime as dt
import json
import os
from typing import Any

from runner import config

STATE_FILE = config.STATE_DIR / "canvas.json"
VERSION = 1


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"version": VERSION, "courses": {}}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Never let unreadable state stop a run: the worst case is a full
        # re-pull, which is correct-but-slow rather than wrong.
        return {"version": VERSION, "courses": {}}
    data.setdefault("courses", {})
    return data


def save(data: dict[str, Any]) -> None:
    config.ensure_dirs()
    data["version"] = VERSION
    data["saved"] = now()
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def course(data: dict[str, Any], course_id: int | str) -> dict[str, Any]:
    """The record for one course, created empty on first sight."""
    return data["courses"].setdefault(str(course_id), {
        "name": "",
        "folder": "",
        "last_pull": "",
        "sections": {},   # pages -> "ok" | "unavailable:403" | "error: ..."
        "items": {},      # "page:intro" -> {"updated_at", "hash", "path"}
        "files": {},      # "16933876"  -> {"updated_at", "size", "sha256", "path"}
    })


def file_is_current(record: dict[str, Any], file_id: int, remote: dict) -> bool:
    """True when the file on disk already matches what Canvas is offering.

    Keyed on `updated_at` plus size rather than a hash of the remote, because
    knowing the answer must not require downloading the thing. The sha256 in
    state is recorded after the fact so a corrupted local file can be caught
    by `vt canvas status --verify`.
    """
    known = record["files"].get(str(file_id))
    if not known:
        return False
    if known.get("updated_at") != remote.get("updated_at"):
        return False
    if remote.get("size") is not None and known.get("size") != remote.get("size"):
        return False
    path = known.get("path")
    return bool(path) and os.path.exists(path)
