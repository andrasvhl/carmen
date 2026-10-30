#!/usr/bin/env python3
"""The digest keeps what a later reader needs and drops the machine talking to itself.

    ~/automation/.venv/bin/python -m tests.session_digest
"""
import json, pathlib, sys, tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from tools import session_digest as sd

TMP = pathlib.Path(tempfile.mkdtemp())


def transcript(name, entry, records):
    p = TMP / f"{name}.jsonl"
    base = {"entrypoint": entry, "cwd": "/home/<user>/vault", "sessionId": name}
    p.write_text("\n".join(json.dumps({**base, **r}) for r in records), encoding="utf-8")
    return p


def user(text, ts="2026-10-05T17:00:00Z", **kw):
    return {"type": "user", "timestamp": ts, "message": {"content": text}, **kw}


def tool(name, inp, ts="2026-10-05T17:01:00Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def said(text, ts="2026-10-05T17:02:00Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"content": [{"type": "text", "text": text}]}}


fails = []

desk = transcript("desk", "claude-desktop", [
    {"type": "custom-title", "customTitle": "Fix the thing"},
    user("Fix the canvas sync<system-reminder>secret harness text</system-reminder>"),
    user([{"type": "tool_result", "content": "huge output"}]),
    user("meta noise", isMeta=True),
    tool("Edit", {"file_path": "/home/<user>/vault/Inbox/A task.md"}),
    tool("Edit", {"file_path": "/home/<user>/vault/Inbox/A task.md"}),
    tool("Bash", {"command": "git commit -q -m 'canvas: fix sync'"}),
    tool("Read", {"file_path": "/x"}),
    tool("mcp__gws__draft_gmail_message", {"to": "x"}),
    tool("mcp__gws__search_gmail_messages", {"query": "x"}),
    said("Done. Shipped the fix."),
])
d = sd.digest(desk, "mac")
if d is None:
    fails.append("desktop session skipped")
else:
    if d["kind"] != "desktop" or d["title"] != "Fix the thing":
        fails.append(f"kind/title wrong: {d['kind']}, {d['title']}")
    if [p["text"] for p in d["prompts"]] != ["Fix the canvas sync"]:
        fails.append(f"prompts not cleaned: {d['prompts']}")
    if d["files"] != ["Inbox/A task.md"]:
        fails.append(f"files not deduplicated/relativised: {d['files']}")
    if d["commits"] != ["canvas: fix sync"]:
        fails.append(f"commit message missed: {d['commits']}")
    if d["actions"] != ["draft_gmail_message"]:
        fails.append(f"actions wrong (search is passive, draft is not): {d['actions']}")
    if d["final"] != "Done. Shipped the fix.":
        fails.append("final message missed")

job = transcript("job", "sdk-cli", [user("Reply with exactly: ok"), said("ok")])
if sd.digest(job) is not None:
    fails.append("headless job run (sdk-cli) was not excluded")

tg = transcript("tg", "sdk-py", [user("hi from telegram"), said("hello")])
if (sd.digest(tg) or {}).get("kind") != "telegram":
    fails.append("sdk-py not labelled telegram")

side = transcript("side", "claude-desktop", [user("subagent prompt", isSidechain=True)])
if sd.digest(side) is not None:
    fails.append("sidechain-only transcript produced a digest")

many = transcript("many", "claude-desktop",
                  [user(f"prompt {i} " + "x" * 2000, ts=f"2026-10-05T17:{i:02d}:00Z")
                   for i in range(30)])
m = sd.digest(many)
if len(m["prompts"]) != sd.MAX_PROMPTS or len(m["prompts"][0]["text"]) > sd.PROMPT_CHARS:
    fails.append("prompt count or length not capped")
if not m["prompts"][-1]["text"].startswith("prompt 29"):
    fails.append("capping dropped the end of the session")
if len(json.dumps(m)) > 8000:
    fails.append(f"digest over 8 KB: {len(json.dumps(m))}")

print("\n".join(fails) if fails else "session_digest: all checks pass")
sys.exit(1 if fails else 0)
