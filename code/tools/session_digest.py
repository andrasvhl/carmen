"""Turn Claude Code transcripts into compact per-session digests.

A raw transcript is hundreds of KB, almost all of it attachments, tool results
and system prompts. What a later reader needs is small: what the owner asked, what
files changed, what was sent or scheduled, and how the session ended. This module
extracts exactly that, deterministically, so `jobs/session_log.py` hands the
model a few KB per session instead of the transcript.

Used on both machines. vault-host digests its own transcripts in place (the
Telegram bridge and Remote Control write there). The Mac has no way to reach the
box except SSH, so `--push` digests the Mac's transcripts and copies them to the
box's state dir; a launchd agent runs it every 30 minutes. Mac sessions only
exist while the Mac is on, so the box job never depends on it being on.

    python -m tools.session_digest --since 2026-10-01      # print digests
    python -m tools.session_digest --push                  # Mac: ship to the box
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"

# Headless `claude -p` runs from scheduled jobs (runner/ask.py). They are the
# machine talking to itself, not work anyone did.
EXCLUDED_ENTRYPOINTS = {"sdk-cli"}
KINDS = {"claude-desktop": "desktop", "cli": "terminal", "sdk-py": "telegram",
         "remote-control": "remote-control"}

PROMPT_CHARS = 500
FINAL_CHARS = 1500
MAX_PROMPTS = 12

# Tools that only look. Anything else (a Gmail draft, a calendar event, a push)
# is an action worth naming in the log.
PASSIVE_TOOLS = {"Read", "Glob", "Grep", "Bash", "ToolSearch", "Agent", "Task",
                 "TodoWrite", "WebSearch", "WebFetch", "Skill", "AskUserQuestion",
                 "EnterPlanMode", "ExitPlanMode", "Monitor", "SendMessage", "ListAgents",
                 "Edit", "Write", "MultiEdit", "NotebookEdit"}
PASSIVE_PREFIXES = ("mcp__Claude_Browser__", "mcp__claude-in-chrome__", "mcp__ccd_",
                    "mcp__visualize__", "mcp__terminal__read")
PASSIVE_VERBS = ("get_", "list_", "search_", "read_", "find", "navigate", "screenshot")

TAG = re.compile(r"<(system-reminder|command-[a-z]+|local-command-[a-z]+|"
                 r"task-notification|pasted_content[^>]*)>.*?</\1>", re.S)
COMMIT_MSG = re.compile(r"git commit[^\n]*?-m\s+[\"']([^\"'\n]{3,160})")
HEREDOC_MSG = re.compile(r"git commit[^\n]*\$\(cat <<'?EOF'?\n([^\n]{3,160})")


def machine() -> str:
    return "vault-host" if socket.gethostname().startswith(("vault", "ubuntu")) \
        or Path("/home/<user>/vault").exists() else "mac"


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean(text: str) -> str:
    text = TAG.sub("", text).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def _prompt_text(content) -> str:
    if isinstance(content, str):
        return _clean(content)
    parts = [c.get("text", "") for c in content
             if isinstance(c, dict) and c.get("type") == "text"]
    return _clean("\n".join(parts))


def _is_action(name: str) -> bool:
    if name in PASSIVE_TOOLS or name.startswith(PASSIVE_PREFIXES):
        return False
    verb = name.rsplit("__", 1)[-1]
    return not verb.startswith(PASSIVE_VERBS)


def digest(path: Path, mach: str | None = None) -> dict | None:
    """One transcript -> one digest dict, or None for a session to skip."""
    entry = title = cwd = None
    start = end = None
    prompts, files, commits, actions = [], [], [], []
    final = ""
    try:
        lines = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with lines:
        for line in lines:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("isSidechain"):
                continue
            entry = entry or d.get("entrypoint")
            cwd = cwd or d.get("cwd")
            t = d.get("type")
            if t == "custom-title":
                title = d.get("customTitle") or title
                continue
            ts = _ts(d.get("timestamp"))
            if ts:
                start = start or ts
                end = ts
            msg = d.get("message") or {}
            if t == "user" and not d.get("isMeta"):
                text = _prompt_text(msg.get("content", ""))
                if text and not text.startswith(("[Request interrupted", "Caveat:")):
                    prompts.append((ts, text[:PROMPT_CHARS]))
            elif t == "assistant":
                for c in msg.get("content") or []:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text" and c.get("text", "").strip():
                        final = c["text"].strip()
                    elif c.get("type") == "tool_use":
                        name, inp = c.get("name", ""), c.get("input") or {}
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and fp:
                            if fp not in files:
                                files.append(fp)
                        elif name == "Bash":
                            cmd = inp.get("command", "")
                            for m in COMMIT_MSG.findall(cmd) + HEREDOC_MSG.findall(cmd):
                                commits.append(m.strip())
                        elif _is_action(name):
                            actions.append(name.rsplit("__", 1)[-1])

    if entry in EXCLUDED_ENTRYPOINTS or not prompts or start is None:
        return None
    if len(prompts) > MAX_PROMPTS:
        # Keep the opening (what the session was for) and the end (where it got to).
        prompts = prompts[:6] + prompts[-(MAX_PROMPTS - 6):]
    home = str(Path.home())
    rel = lambda p: p.replace(home + "/Obsidian/<VAULT_NAME>/", "") \
                     .replace("/home/<user>/vault/", "").replace(home, "~")
    return {
        "id": path.stem,
        "machine": mach or machine(),
        "kind": KINDS.get(entry or "", entry or "unknown"),
        "cwd": rel(cwd or ""),
        "title": title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "turns": len(prompts),
        "prompts": [{"at": p[0].isoformat() if p[0] else None, "text": p[1]} for p in prompts],
        "files": [rel(f) for f in files][:40],
        "commits": commits[:15],
        "actions": sorted(set(actions)),
        "final": final[:FINAL_CHARS],
    }


def transcripts(since: datetime):
    """Top-level transcripts touched since `since`. Subagent files live in
    subfolders and are skipped by the glob."""
    cutoff = since.timestamp()
    for p in PROJECTS.glob("*/*.jsonl"):
        try:
            if p.stat().st_mtime >= cutoff:
                yield p
        except OSError:
            continue


def collect(since: datetime, mach: str | None = None) -> list[dict]:
    out = [d for p in transcripts(since) if (d := digest(p, mach))]
    return sorted(out, key=lambda d: d["start"])


# --- Mac -> box -----------------------------------------------------------

STATE = Path.home() / ".local" / "state" / "automation"
PUSH_MARK = STATE / "session-digest-push.json"
REMOTE_DIR = ".local/state/automation/session-digests/mac"


def push(host: str = "vault-host", days: int = 8) -> int:
    """Digest transcripts changed since the last good push and copy them to the
    box, one JSON file per session (overwritten, so a session that grew is
    simply re-sent). Returns the number sent. Raises on SSH failure, leaving the
    mark untouched so the next run retries the same window."""
    mark = {}
    if PUSH_MARK.exists():
        mark = json.loads(PUSH_MARK.read_text())
    floor = datetime.now(timezone.utc) - timedelta(days=days)
    since = max(floor, _ts(mark.get("last")) or floor)
    started = datetime.now(timezone.utc)
    batch = {d["id"]: d for d in collect(since - timedelta(minutes=5), "mac")}
    if batch:
        payload = json.dumps(batch)
        script = ("import json,sys,pathlib;b=json.load(sys.stdin);"
                  f"d=pathlib.Path.home()/'{REMOTE_DIR}';d.mkdir(parents=True,exist_ok=True);"
                  "[(d/f'{k}.json').write_text(json.dumps(v)) for k,v in b.items()]")
        subprocess.run(["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", host,
                        f"python3 -c \"{script}\""],
                       input=payload, text=True, check=True, capture_output=True, timeout=60)
    STATE.mkdir(parents=True, exist_ok=True)
    PUSH_MARK.write_text(json.dumps({"last": started.isoformat(), "sent": len(batch)}))
    return len(batch)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD, local")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--host", default="vault-host")
    a = ap.parse_args(argv)
    if a.push:
        try:
            n = push(a.host)
        except (subprocess.SubprocessError, OSError) as exc:
            print(f"session_digest: push failed: {exc}", file=sys.stderr)
            return 1
        print(f"session_digest: pushed {n}")
        return 0
    since = (datetime.fromisoformat(a.since).astimezone() if a.since
             else datetime.now().astimezone() - timedelta(days=a.days))
    json.dump(collect(since), sys.stdout, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
