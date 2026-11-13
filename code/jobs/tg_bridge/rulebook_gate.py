"""The rulebook lock: edits to the files every session loads ask first (Nov 8).

The risk is persistent prompt injection (OpenClaw's memory poisoning): a page or an email talks
the agent into adding a line to CLAUDE.md or a skill, and every later session on every machine
follows it. owner decision, Nov 8: "ok build", after asking for a balance where he does not "constantly
allow for things". Replayed that day: in 1,080 bridge sessions the agent edited these files zero
times, while it edited Inbox/ task notes 146 times. So only the rulebook asks; everything else
runs as before.

  ask    Edit, Write, MultiEdit, NotebookEdit on a PROTECTED path (resolved, so a symlink or
         `..` does not get around it); a Bash command that names a protected file and could
         write (redirect, tee, sed -i, mv, cp, rm, a script interpreter ...)
  allow  every other edit; a Bash command that only reads a protected file (cat, grep ...),
         unless it also matches one of the bridge's other ask rules or sends

Edits and Write are ask rules (agent.ASK_RULES), so each one reaches can_use_tool even under
bypassPermissions; ask rules win over the allow list (verified on the box, CLI 2.1.319). One
gap found the same night: the CLI leaves redirect targets out of rule matching, so
`echo x >> CLAUDE.md` never reached can_use_tool (`sed -i`, `cat`, `tee` did). pre_tool_hook()
closes it: a PreToolUse hook sees every Bash command in full and answers "ask" for a write into
the rulebook, which the SDK forwards to can_use_tool (verified: the line did not land). What
still slips past is caught the next morning by jobs/rulebook_watch.py.

The code lock (Nov 11, audit A13; the owner chose "ask with one tap"): Carmen's live code tree,
~/automation, asks the same way. A Carmen task chat had edited jobs/checkin_app/talk.py in the
live tree, uncommitted and outside the deploy gate, and every timer runs from that tree. Reads,
`vt` (which writes the vault, not code) and worktrees (~/wip-*) stay free; an Edit or Write under
~/automation, and a Bash command there that writes (sed -i, mv, cp, rm, a redirect or tee, a
writing script, git commit/checkout/reset/..., checkin-restart) ask for his tap.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

HOME = Path.home()
VAULT = HOME / "vault"

PROTECTED_FILES = [VAULT / "CLAUDE.md", VAULT / "Claude Master Router.md",
                   VAULT / "_system" / "claude" / "global-CLAUDE.md",
                   HOME / ".claude" / "CLAUDE.md", HOME / ".claude" / "settings.json",
                   HOME / ".claude" / "settings.local.json"]
PROTECTED_DIRS = [VAULT / "_system" / "claude" / "memory", VAULT / "_system" / "skills",
                  VAULT / ".claude", HOME / ".claude" / "agents", HOME / ".claude" / "skills",
                  HOME / ".claude" / "hooks"]

CODE_DIR = HOME / "automation"   # the live tree every timer and service runs from (audit A13)

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Names that put a Bash command in front of the check (the Bash ask rules below match on these).
NAMES = ["CLAUDE.md", "Master Router", "global-CLAUDE", "MEMORY.md", "claude/memory",
         "_system/skills", ".claude/settings", ".claude/agents", ".claude/skills", ".claude/hooks",
         "vault/.claude"]
BASH_RULES = [f"Bash(*{n}*)" for n in NAMES] + ["Bash(*/automation*)", "Bash(*automation/*)"]
# Everything a Bash ask rule above can match on, for the bridge's quick allow (shell_gate.routed_by_us).
ROUTE_NAMES = NAMES + ["/automation", "automation/"]
# The live code tree named in a command: ~/automation, $HOME/automation, /home/<me>/automation.
_CODE = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/home/[\w.-]+)/automation(?![\w.-])|(?:^|[\s;&|(])cd\s+automation\b")
# `vt` and `run-job` are tools that live in the tree; running them changes no code.
_CODE_TOOLS = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/home/[\w.-]+)/automation/bin/(?:vt|run-job)\b")
_CODE_WRITES = re.compile(r"(\bsed\b[^|;&]*\s-i|\bperl\b[^|;&]*\s-[a-z]*i|\bmv\b|\bcp\b|\brm\b|\bln\b|"
                          r"\btruncate\b|\bdd\b|\binstall\b|\bchmod\b|\btouch\b|\bawk\b[^|;&]*-i\b|\bpatch\b|"
                          r"\brsync\b|\bscp\b|\bcurl\b[^|;&]*\s-o|\bwget\b|checkin-restart|"
                          r"\bgit\s+(?:commit|checkout|restore|apply|mv|rm|reset|stash|merge|pull|rebase|cherry-pick|am|switch)\b)", re.I)

# Commands that change files whatever their arguments. Redirects, tee and scripts are judged
# by what they write to (below), because `2>/dev/null` and read-only python are everywhere:
# the first draft asked on 23 of 1,159 past commands, all of them reads.
_WRITES = re.compile(r"(\bsed\b[^|;&]*\s-i|\bperl\b[^|;&]*\s-[a-z]*i|\bmv\b|\bcp\b|\brm\b|\bln\b|"
                     r"\btruncate\b|\bdd\b|\binstall\b|\bchmod\b|\btouch\b|\bawk\b[^|;&]*-i\b|"
                     r"\bvt\s+move\b|\bgit\s+(checkout|restore|apply|mv|rm|reset)\b|\bpatch\b|"
                     r"\brsync\b|\bscp\b|\bcurl\b[^|;&]*\s-o|\bwget\b)", re.I)
_REDIRECT = re.compile(r"(?<![0-9&])(?:&?>>?|>\|)\s*(?!&|/dev/null\b)(\"[^\"]+\"|'[^']+'|\S+)")
_TEE = re.compile(r"\btee\b((?:\s+(?:-\w+|\"[^\"]+\"|'[^']+'|[^\s|;&]+))+)")
_SCRIPT = re.compile(r"\b(python3?|node|ruby|perl)\b")
_SCRIPT_WRITES = re.compile(r"(write_text|write_bytes|\.write\(|open\([^)]*['\"][wax+]|os\.replace|"
                            r"os\.rename|shutil\.|unlink\(|writeFile|appendFile)")


def _names_protected(text: str) -> bool:
    return any(n in text for n in NAMES)


def _real(path: str) -> Path | None:
    try:
        p = Path(os.path.expanduser(str(path or "")))
        if not p.is_absolute():
            return None
        return Path(os.path.realpath(p))
    except (OSError, ValueError):
        return None


def _roots():
    files = {Path(os.path.realpath(f)) for f in PROTECTED_FILES}
    dirs = {Path(os.path.realpath(d)) for d in PROTECTED_DIRS}
    return files, dirs


def protected(path: str) -> bool:
    real = _real(path)
    if real is None:
        return False
    files, dirs = _roots()
    return real in files or any(real == d or d in real.parents for d in dirs)


def in_code(path: str) -> bool:
    """A path inside the live code tree (resolved, so a symlink or `..` does not get around it)."""
    real = _real(path)
    code = Path(os.path.realpath(CODE_DIR))
    return real is not None and (real == code or code in real.parents)


def edit_reason(tool_name: str, tool_input: dict) -> str:
    """"" if the edit may run; otherwise why it asks."""
    inp = tool_input or {}
    path = inp.get("file_path") or inp.get("notebook_path") or ""
    if protected(path):
        return f"{Path(path).name} is part of the rulebook every session loads"
    if in_code(path):
        return (f"{Path(path).name} is Carmen's live code (~/automation), which every job runs from; "
                "code changes go through a build and checkin-restart")
    return ""


# D-19
def code_reason(command: str) -> str | None:
    """The code lock for a Bash command: None when it does not touch ~/automation, "" when it
    only reads there or runs vt / run-job, otherwise why it asks."""
    cmd = _CODE_TOOLS.sub("VT", str(command or ""))
    if not _CODE.search(cmd):
        return None
    m = _CODE_WRITES.search(cmd)
    if m:
        return f"it may change Carmen's live code ('{m.group(0).strip()}')"
    for r in _REDIRECT.finditer(cmd):
        t = r.group(1).strip("\"'")
        if not t.startswith(("/tmp/", "/dev/")):
            return f"it writes into {t}, with the live code tree in the command"
    for t in _TEE.finditer(cmd):
        if not all(x.startswith(("/tmp/", "-")) for x in t.group(1).split()):
            return "it tees into a file, with the live code tree in the command"
    if _SCRIPT.search(cmd) and _SCRIPT_WRITES.search(cmd):
        return "a script in it writes files, with the live code tree in the command"
    return ""


def bash_reason(command: str, other_ask_rules: list[str] | None = None) -> str | None:
    """None: the command names no protected file, the normal path decides. "": it only reads
    one, run it. Otherwise why it asks."""
    # The Carmen kit's template vault (~/carmen/.../vault-template/) has its own _system/skills,
    # which is product source, not a rulebook any session loads.
    cmd = re.sub(r"vault-template/_system/skills", "vault-template/_kit_skills", str(command or ""))
    if not any(n in cmd for n in NAMES):
        code = code_reason(cmd)
        if code is None:
            return None
        from .shell_gate import matches_any
        if other_ask_rules and matches_any(cmd, other_ask_rules):
            return "it matches one of the bridge's other ask rules"
        return code
    from .shell_gate import matches_any
    if other_ask_rules and matches_any(cmd, other_ask_rules):
        return "it matches one of the bridge's other ask rules"
    m = _WRITES.search(cmd)
    if m:
        return f"it may change a rulebook file ('{m.group(0).strip()}')"
    for r in _REDIRECT.finditer(cmd):
        if _names_protected(r.group(1)):
            return f"it writes into {r.group(1).strip(chr(34) + chr(39))}"
    for t in _TEE.finditer(cmd):
        if _names_protected(t.group(1)):
            return "it tees into a rulebook file"
    if _SCRIPT.search(cmd) and _SCRIPT_WRITES.search(cmd):
        return "a script in it writes files"
    return ""


async def pre_tool_hook(hook_input: dict, tool_use_id, context) -> dict:
    """PreToolUse hook for Bash: "ask" when the command writes into the rulebook, so redirects
    (invisible to ask rules) still reach can_use_tool and the Allow card. Otherwise no opinion."""
    cmd = str(((hook_input or {}).get("tool_input") or {}).get("command", ""))
    why = bash_reason(cmd, [])
    if why:
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                       "permissionDecisionReason": f"rulebook: {why}"}}
    return {}
