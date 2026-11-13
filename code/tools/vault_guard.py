"""Vault guards run as Claude Code hooks on both machines (Nov 11, the owner: "i agree with all hooks
besides the upload thing"). Rules a session used to have to remember from CLAUDE.md, enforced at
the moment of the action instead:

  vt guard mv           PreToolUse Bash: refuse `mv` / `git mv` of anything inside the vault;
                        `vt move` rewrites inbound links, a shell move breaks them silently.
  vt guard frontmatter  PostToolUse Edit|Write|MultiEdit on a vault note: a frontmatter key the
                        edit adds that _system/frontmatter-properties.md does not register is an
                        error (keys already in the note are not judged, so old notes stay editable).
  vt guard memory       PreToolUse Edit|Write on Claude's memory folder: the checklist from
                        CLAUDE.md § Before writing to Claude's own auto-memory, shown right then.

Hook protocol: JSON on stdin; a refusal prints JSON with permissionDecision "deny" (PreToolUse) or
exits 2 with the reason on stderr (PostToolUse); anything else exits 0 silently.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

from tools.check_tasks import find_vault, registered_keys

MEMORY_CHECKLIST = (
    "Before writing to Claude's own auto-memory (vault CLAUDE.md § Before writing to Claude's own "
    "auto-memory): 1. Search the vault first for an existing home: the Claude Master Router, project "
    "notes, roadmap docs, _system/. 2. Behavioural feedback about how Claude should work is not "
    "automatically memory: vault-wide and binding every session goes in CLAUDE.md § Always true; "
    "routing in the Claude Master Router; the productivity stack in _system/SYSTEM.md § How Claude "
    "operates; a vault fact or plan in its project or profile note. 3. Memory is right only for what "
    "is true in any project, codebase or vault, or a pointer to an external system with no vault "
    "presence. If this passes, write it: one fact per file with frontmatter, plus one line in "
    "MEMORY.md."
)


def _vaults() -> list[Path]:
    out = []
    for v in (Path.home() / "Obsidian/<VAULT_NAME>", Path.home() / "vault"):
        try:
            if (v / "_system").is_dir():
                out.append(v.resolve())
        except OSError:
            pass
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        p = path.expanduser().resolve()
    except OSError:
        return False
    return any(p == r or r in p.parents for r in roots)


def _mv_targets(command: str) -> list[list[str]]:
    """The argument lists of every `mv` or `git mv` in a shell command (split on ; && || |)."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return []
    out, cur = [], []
    for t in toks + [";"]:
        if t in (";", "&&", "||", "|", "&"):
            if cur:
                if cur[0] == "mv":
                    out.append(cur[1:])
                elif cur[:2] == ["git", "mv"]:
                    out.append(cur[2:])
            cur = []
        else:
            cur.append(t)
    return out


def guard_mv(data: dict) -> int:
    cmd = (data.get("tool_input") or {}).get("command") or ""
    if "mv" not in cmd:
        return 0
    roots = _vaults()
    cwd = Path(data.get("cwd") or Path.cwd())
    for args in _mv_targets(cmd):
        paths = [a for a in args if not a.startswith("-")]
        if any(_inside(p if p.is_absolute() else cwd / p, roots) for p in map(Path, (os_expand(a) for a in paths))):
            reason = ("Moving or renaming a vault file with mv breaks every wikilink to it (Obsidian repairs "
                      "links only for moves made inside Obsidian). Use `~/automation/bin/vt move <old> <new>`, "
                      "which rewrites inbound links and refuses Wiki sources; `--dry-run` first for a large move.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
            return 0
    return 0


def os_expand(a: str) -> str:
    return str(Path(a).expanduser()) if a.startswith("~") else a


FM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):", re.M)


def _added_keys(tool: str, ti: dict, before_fm: str | None) -> set[str]:
    """Frontmatter keys this edit introduces."""
    if tool == "Write":
        text = ti.get("content") or ""
        m = re.match(r"^---\n(.*?)\n---", text, re.S)
        new = set(FM_KEY.findall(m.group(1))) if m else set()
        return new
    chunks = []
    if tool == "Edit":
        chunks = [(ti.get("old_string") or "", ti.get("new_string") or "")]
    elif tool == "MultiEdit":
        chunks = [(e.get("old_string") or "", e.get("new_string") or "") for e in ti.get("edits") or []]
    added = set()
    for old, new in chunks:
        added |= set(FM_KEY.findall(new)) - set(FM_KEY.findall(old))
    return added


def guard_frontmatter(data: dict) -> int:
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    fp = ti.get("file_path") or ""
    if not fp.endswith(".md"):
        return 0
    path = Path(fp)
    roots = _vaults()
    if not _inside(path, roots) or any(part.startswith(".") for part in path.parts):
        return 0
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return 0
    m = re.match(r"^---\n(.*?)\n---", text, re.S)
    if not m:
        return 0
    in_fm = set(FM_KEY.findall(m.group(1)))
    keys = registered_keys(find_vault(path))
    if keys is None:
        return 0
    added = _added_keys(tool, ti, m.group(1)) & in_fm
    bad = sorted(k for k in added if k not in keys)
    if bad:
        print(f"{path.name}: frontmatter key(s) not registered in _system/frontmatter-properties.md: "
              f"{', '.join(bad)}. Obsidian accepts any key silently, so that page is the only thing stopping "
              f"two systems claiming one name. Use a registered key, or register this one there first "
              f"(snake_case; `status:` is tasks only, documents use `stage:`; `revisit:` enrols the note in "
              f"a weekly email sweep).", file=sys.stderr)
        return 2
    return 0


def guard_memory(data: dict) -> int:
    fp = (data.get("tool_input") or {}).get("file_path") or ""
    if "/_system/claude/memory/" not in fp:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": MEMORY_CHECKLIST}}))
    return 0


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else ""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    return {"mv": guard_mv, "frontmatter": guard_frontmatter, "memory": guard_memory}.get(mode, lambda d: 0)(data)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
