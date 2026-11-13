"""Ask Claude a question from inside a scheduled job, safely.

THE RULE THIS ENFORCES: an AI job reads and proposes. It never writes, and it
never alerts.

Read-only tools are hardcoded here rather than passed in, so no job can widen
them by accident. That closes all three gaps in scheduled-execution.md at once,
structurally rather than by policy:

  gap 3, no non-interactive permission mode
      --permission-mode dontAsk with an explicit allowlist. Verified headless
      with no TTY on 2026-08-28.
  gap 1, two writers on one file
      The model cannot write. It cannot collide with an edit on the owner's phone
      because it has no tool that touches a file.
  gap 2, propose don't perform
      The model returns text. Python decides what, if anything, lands in the
      vault - and only ever as a new dated note.

Never use --bare here: bare mode reads only ANTHROPIC_API_KEY or apiKeyHelper
and never OAuth, so it cannot authenticate with the claude.ai subscription (Max 5x).
"""

import json
import subprocess
from datetime import datetime

from . import config
from .job import log

# Read, search, navigate. Nothing that mutates, and no Bash - a shell is a
# write tool wearing a disguise.
READ_ONLY_TOOLS = ["Read", "Glob", "Grep"]
# Research adds the web, still nothing that writes (checkin_app prep, Oct 30).
RESEARCH_TOOLS = READ_ONLY_TOOLS + ["WebSearch", "WebFetch"]

DEFAULT_TIMEOUT = 600

# An alias, never a pinned id (owner decision, 2026-09-30). `opus` resolves to the newest
# Opus the installed Claude Code knows, so the tier follows new releases without
# a code change. That only holds while Claude Code itself is current: headless
# -p never self-updates, and 2.1.220 still mapped `opus` to Opus 5 when 5.5 was
# out. See maintenance concern `claude-code-current`. A job that needs less can
# pass model="sonnet" or "haiku"; which job needs which is concern
# `ai-job-model-tier`.
DEFAULT_MODEL = "opus"


class AskFailed(Exception):
    pass


def ask(prompt: str, *, timeout: int = DEFAULT_TIMEOUT, cwd=None,
        model: str = DEFAULT_MODEL, data: str | None = None,
        tools: list[str] | None = None) -> str:
    """Run one headless, read-only Claude turn. Returns its text.

    `data` goes in on stdin, next to the prompt, for material too big for argv
    (Linux caps one argument at 128 KB). One turn instead of making the model
    Read a file in chunks: plan_today_prerun's chunked reads cost 2.7M input
    tokens over 25 turns on 2026-10-05 for ~110k tokens of data.

    Raises AskFailed on non-zero exit, timeout, or empty output. The caller
    decides whether that is worth reporting; it is never fatal to the vault,
    because nothing has been written at this point.
    """
    return _run(prompt, timeout=timeout, cwd=cwd, model=model, data=data, tools=tools)[0]


def probe(model: str = DEFAULT_MODEL, *, timeout: int = 120) -> str:
    """One trivial turn; returns the model id that actually answered.

    For `claude_update`: proves headless -p still works after an update, and
    shows what an alias like `opus` resolves to on this install. Raises
    AskFailed like ask().
    """
    text, main = _run("Reply with exactly: ok", timeout=timeout, cwd=None, model=model)
    return main


def _run(prompt: str, *, timeout: int, cwd, model: str,
         data: str | None = None, tools: list[str] | None = None) -> tuple[str, str]:
    claude = config.HOME / ".local" / "bin" / "claude"
    if not claude.exists():
        raise AskFailed(f"{claude} not found")

    cmd = [str(claude), "-p", prompt,
           "--model", model,
           "--output-format", "json",
           "--allowedTools", *(tools or READ_ONLY_TOOLS),
           "--permission-mode", "dontAsk"]
    try:
        # stdin closed explicitly: since 2.1.2xx, -p waits 3 s for piped input
        # when stdin is not a TTY.
        io = {"input": data} if data is not None else {"stdin": subprocess.DEVNULL}
        r = subprocess.run(cmd, capture_output=True, text=True, **io,
                           timeout=timeout, cwd=str(cwd or config.VAULT))
    except subprocess.TimeoutExpired:
        raise AskFailed(f"no answer within {timeout}s")

    raw = (r.stdout or "").strip()
    if r.returncode != 0:
        raise AskFailed(f"exit {r.returncode}: {(r.stderr or raw)[:300]}")
    # JSON rather than text, so the log says which model actually answered. An
    # alias that silently resolved to an old model is otherwise invisible.
    try:
        reply = json.loads(raw)
    except json.JSONDecodeError:
        raise AskFailed(f"not JSON: {raw[:200]}")
    if reply.get("is_error"):
        raise AskFailed(f"error: {str(reply.get('result') or reply.get('terminal_reason'))[:300]}")
    out = (reply.get("result") or "").strip()
    if not out:
        raise AskFailed("empty response")

    # The main model is the one that did the most output; Claude Code also
    # calls Haiku for small background steps.
    usage = reply.get("modelUsage") or {}
    main = max(usage, key=lambda m: usage[m].get("outputTokens", 0), default="unknown")
    log(f"ask: {len(out)} chars returned by {main} (asked for {model})")
    return out, main


def ask_lines(prompt: str, **kw) -> list[str]:
    """Same, for prompts that must answer as one item per line.

    Models add preamble even when told not to. Anything that does not look
    like a list item is dropped rather than parsed hopefully.
    """
    return [l.strip(" -*\t") for l in ask(prompt, **kw).split("\n")
            if l.strip() and not l.strip().endswith(":")]


# --- proposals ------------------------------------------------------------
#
# "The AI needs to write" conflates two things. Producing corrected text is
# judgement and only the model can do it. Deciding that text replaces the live
# file is a separate act, and that is the dangerous one.
#
# So the model produces; Python lands it as a proposal beside the original,
# never over it. The original is untouched until the owner says otherwise, which
# is the review gate gap 2 has always needed.

PROPOSALS = config.VAULT / "_system" / "proposals"


def propose(target: str, content: str, *, why: str, job: str) -> "pathlib.Path | None":
    """Write model-produced content as a reviewable proposal.

    `target` is the vault-relative file this would replace. Nothing is
    overwritten. Returns None if today's proposal for this target already
    exists, so a daily job cannot pile up duplicates.
    """
    PROPOSALS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone()
    stem = target.replace("/", " - ").removesuffix(".md")
    path = PROPOSALS / f"{stem} ({stamp.date()}).md"
    if path.exists():
        return None

    path.write_text(
        "---\n"
        "type: reference\n"
        "stage: draft\n"
        f"proposed_for: \"{target}\"\n"
        f"last_run: {datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')}\n"
        "---\n\n"
        f"# Proposed rewrite of `{target}`\n\n"
        f"**Generated by `{job}` on {stamp.strftime('%Y-%m-%d %H:%M %Z')}. "
        f"Nothing has been changed.** `{target}` is untouched.\n\n"
        f"**Why:** {why}\n\n"
        "To accept, copy the body below over the original yourself. To reject, delete this\n"
        "note. Doing neither is also a decision, and the proposal will not repeat today.\n\n"
        "---\n\n" + content.strip() + "\n",
        encoding="utf-8")
    return path
