"""The alert log: where every finding goes instead of a mail.

the owner does not read the maintenance mails (2026-10-03), so a finding that only
mails reaches nobody. Since the maintenance brief, a job's findings land here,
one block each, and the Monday brief turns the week's blocks into decisions.
Only CRITICAL still interrupts, by push. See _system/maintenance-brief.md.

    _system/maintenance/alert-log/<YYYY-MM>.md

APPEND-ONLY, by construction. Nothing here can rewrite or remove a block: the
file is opened for append and each block goes down in one write. That is the
answer to scheduled-execution.md gap 1 for this file: if Obsidian Sync loses a
race against an edit on the phone, the cost is one block, never the document.

A block is plain markdown a person can read, with one `- field: value` line per
field so the brief can parse it back. Values are single-line by construction.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

# Months and headings are the owner's time, as everywhere else he reads.
TZ = ZoneInfo("America/Los_Angeles")

# What a block records. `event` separates the kinds of thing that happen to a
# finding; `tier` stays the runner's tier vocabulary.
FAILING = "failing"      # a check failed and is new, escalated, or critical
RESOLVED = "resolved"    # a check that failed last run passes now
CRASHED = "crashed"      # the job itself broke (runner/alert.py)
REPAIR = "repair"        # the maintenance brief tried a safe repair
DRIFT = "drift"          # reserved for findings logged outside a check

FIELDS = ("time", "job", "check", "key", "tier", "owner", "event", "detail")
MAX_DETAIL = 600


class AlertLogUnavailable(Exception):
    """The log could not be written. Callers fall back to mail."""


@dataclass
class Entry:
    job: str
    check: str
    tier: str
    owner: str = ""
    detail: str = ""
    event: str = FAILING
    key: str = ""
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        self.key = self.key or self.check


def log_dir() -> Path:
    # Resolved on every call, not at import: tests and VAULT_PATH move the vault.
    return config.VAULT / "_system" / "maintenance" / "alert-log"


def path_for(when: datetime) -> Path:
    return log_dir() / f"{when.astimezone(TZ).strftime('%Y-%m')}.md"


def one_line(value: str, limit: int = MAX_DETAIL) -> str:
    text = re.sub(r"\s*\n\s*", " / ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def format_block(e: Entry) -> str:
    local = e.time.astimezone(TZ)
    head = f"### {local.strftime('%Y-%m-%d %H:%M %Z')} · {e.job} · {e.event} · {e.tier}"
    values = {"time": e.time.astimezone(timezone.utc).isoformat(timespec="seconds"),
              "job": e.job, "check": e.check, "key": e.key, "tier": e.tier,
              "owner": e.owner, "event": e.event, "detail": e.detail}
    lines = [head] + [f"- {k}: {one_line(values[k])}" for k in FIELDS]
    return "\n".join(lines) + "\n\n"


def _header(when: datetime) -> str:
    month = when.astimezone(TZ).strftime("%Y-%m")
    return ("---\n"
            "type: reference\n"
            "component: maintenance-brief\n"
            "---\n\n"
            f"# Alert log, {month}\n\n"
            "**Append-only, written by `~/automation` on vault-host. Do not edit.** One block per "
            "finding, repair or crash. Read by the Monday maintenance brief, which is where "
            "these reach the owner; see [[maintenance-brief]].\n\n")


def append(entries: list[Entry]) -> Path | None:
    """Append one block per entry. Raises AlertLogUnavailable on any failure.

    Refuses rather than creating a vault: if `_system/` is missing the vault is
    not mounted (obsidian-sync down, wrong path), and writing into a fresh
    directory there would be a log nobody ever sees.
    """
    if not entries:
        return None
    if not (config.VAULT / "_system").is_dir():
        raise AlertLogUnavailable(f"{config.VAULT}/_system is not a directory")
    path = path_for(entries[0].time)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # "x" so two jobs starting a month at once cannot both write a header.
            with path.open("x", encoding="utf-8") as fh:
                fh.write(_header(entries[0].time))
        except FileExistsError:
            pass
        data = "".join(format_block(e) for e in entries).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except OSError as exc:
        raise AlertLogUnavailable(f"{path}: {type(exc).__name__}: {exc}") from exc
    return path


_HEAD = re.compile(r"^### ", re.M)
_FIELD = re.compile(r"^- ([a-z]+): ?(.*)$")


def parse(text: str) -> list[Entry]:
    """Every well-formed block in one file. A damaged block is skipped, not fatal."""
    out = []
    for chunk in _HEAD.split(text)[1:]:
        values = {}
        for line in chunk.splitlines()[1:]:
            m = _FIELD.match(line.strip())
            if m and m.group(1) in FIELDS:
                values[m.group(1)] = m.group(2).strip()
        try:
            when = datetime.fromisoformat(values["time"])
        except (KeyError, ValueError):
            continue
        if not values.get("job") or not values.get("check"):
            continue
        out.append(Entry(job=values["job"], check=values["check"], tier=values.get("tier", ""),
                         owner=values.get("owner", ""), detail=values.get("detail", ""),
                         event=values.get("event", FAILING), key=values.get("key", ""),
                         time=when))
    return out


def read(since: datetime, until: datetime | None = None) -> list[Entry]:
    """Every block in [since, until), oldest first, across month files."""
    until = until or datetime.now(timezone.utc)
    files = []
    month = since.astimezone(TZ).replace(day=1, hour=12)
    while month <= until.astimezone(TZ) + timedelta(days=31):
        files.append(path_for(month))
        month = (month + timedelta(days=32)).replace(day=1)
    entries = []
    for f in dict.fromkeys(files):
        try:
            entries += parse(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return sorted((e for e in entries if since <= e.time < until), key=lambda e: e.time)
