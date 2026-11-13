"""Does vault-host's secrets layout still match _system/credentials.md?

Compares the doc's vault-host table against the box: which files exist, which
key names each env file holds, and their modes. NAMES ONLY, NEVER VALUES.

Key names come from exactly the command the doc's Gotchas prescribes,

    grep -o '^[A-Za-z0-9_]*=' <file>

which prints only well-formed key names. `sed 's/=.*/=/'` prints whole lines
that have no `=`, and that is how a stray pasted Canvas token reached a Claude
session on 2026-10-03. Nothing in this module reads a secret file any other
way, and nothing it returns holds more than a path, a key name or a mode.

The MacBook rows are not checked: this runs on the box, and the Mac is off
half the time. The doc says those rows are manual.
"""

import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import config

CREDENTIALS = "_system/credentials.md"
KEY = re.compile(r"`([A-Z][A-Z0-9_]+)`")
PATH = re.compile(r"`(~/[^`]+)`")

# Where secrets are found on this box, beyond what the doc names. Same shapes the
# status job's "never considered" scan uses.
CANDIDATE_GLOBS = ("*.env", ".*.env", ".config/*/*.env")


@dataclass
class Row:
    path: str                       # as written in the doc, `~/...`
    keys: set[str] = field(default_factory=set)
    subset: bool = False            # the doc lists some keys "plus" others
    symlink_to: str = ""

    @property
    def is_env(self) -> bool:
        return self.path.endswith(".env") and not self.symlink_to


def documented(text: str) -> list[Row]:
    """The vault-host table, one Row per file. Continuation rows (empty first
    cell) add keys to the file above them."""
    m = re.search(r"^### vault-host\s*$(.*?)(?=^### |^## |\Z)", text, re.M | re.S)
    if not m:
        return []
    rows: list[Row] = []
    for line in m.group(1).splitlines():
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] == "File":
            continue
        first, holds = cells[0], cells[1]
        p = PATH.search(first)
        if p:
            row = Row(path=p.group(1))
            link = re.search(r"symlink to\s+`(~/[^`]+)`", holds)
            if link:
                row.symlink_to = link.group(1)
            rows.append(row)
        elif first or not rows:
            continue
        row = rows[-1]
        if row.symlink_to:
            continue
        row.keys |= set(KEY.findall(holds))
        # Anything left after the backticked names, commas and "and" is prose
        # ("plus non-secret settings", "B2 key"): the doc lists only some keys.
        rest = re.sub(r"`[^`]*`|,|\band\b", "", holds).strip()
        if rest:
            row.subset = True
    return rows


def expand(path: str, home: Path) -> list[Path]:
    rel = path[2:] if path.startswith("~/") else path
    if any(ch in rel for ch in "*?["):
        return sorted(home.glob(rel.rstrip("/")))
    return [home / rel.rstrip("/")]


def key_names(path: Path) -> set[str]:
    """Key names in an env file. The doc's command, verbatim; values never leave grep."""
    r = subprocess.run(["grep", "-o", "^[A-Za-z0-9_]*=", str(path)],
                       capture_output=True, text=True, timeout=15)
    return {line[:-1] for line in r.stdout.splitlines() if line.endswith("=") and len(line) > 1}


def _mode_problem(p: Path) -> str:
    try:
        st = p.lstat()
    except OSError:
        return ""
    if stat.S_ISLNK(st.st_mode):
        return ""
    mode = stat.S_IMODE(st.st_mode)
    want = 0o700 if stat.S_ISDIR(st.st_mode) else 0o600
    if mode & 0o077:
        return f"is mode {mode:04o}, the doc says {want:04o}"
    return ""


def check(home: Path | None = None, vault: Path | None = None) -> list[tuple[str, str]]:
    """Every difference, as (stable key, sentence). Sentences hold paths, key
    names and modes only. An empty list means the box matches the doc."""
    home = home or config.HOME
    vault = vault or config.VAULT
    doc = vault / CREDENTIALS
    try:
        rows = documented(doc.read_text(encoding="utf-8"))
    except OSError as exc:
        return [("credentials-doc", f"{CREDENTIALS} unreadable ({type(exc).__name__})")]
    if not rows:
        return [("credentials-doc", f"{CREDENTIALS} has no vault-host table to compare against")]

    out: list[tuple[str, str]] = []
    known: set[Path] = set()
    for row in rows:
        paths = expand(row.path, home)
        known |= set(paths)
        if not paths or not any(p.exists() or p.is_symlink() for p in paths):
            out.append((f"missing|{row.path}", f"`{row.path}` is in the doc but not on the box"))
            continue
        if row.symlink_to:
            target = expand(row.symlink_to, home)[0]
            p = paths[0]
            if not p.is_symlink() or p.resolve() != target.resolve():
                out.append((f"symlink|{row.path}",
                            f"`{row.path}` should be a symlink to `{row.symlink_to}` and is not"))
            continue
        for p in paths:
            bad = _mode_problem(p)
            if bad:
                out.append((f"mode|{row.path}", f"`{row.path}` {bad}"))
        if row.is_env and row.keys:
            actual = key_names(paths[0])
            missing = sorted(row.keys - actual)
            extra = [] if row.subset else sorted(actual - row.keys)
            if missing:
                out.append((f"keys-missing|{row.path}",
                            f"`{row.path}` lacks key(s) the doc lists: {', '.join(missing)}"))
            if extra:
                out.append((f"keys-extra|{row.path}",
                            f"`{row.path}` holds key(s) the doc does not list: {', '.join(extra)}"))

    for pattern in CANDIDATE_GLOBS:
        for p in sorted(home.glob(pattern)):
            if p.is_file() and p not in known:
                known.add(p)
                rel = "~/" + str(p.relative_to(home))
                out.append((f"undocumented|{rel}",
                            f"`{rel}` looks like a secrets file and is not in the doc"))
    return out
