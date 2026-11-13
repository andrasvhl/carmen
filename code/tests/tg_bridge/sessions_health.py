#!/usr/bin/env python3
"""Resume or start fresh: the transcript health check and everything around it.

    python3 -m tests.tg_bridge.sessions_health

This is where most of OpenClaw's session bugs lived, so it is tested before
anything else is trusted:

  1. **check_transcript** on sample files shaped like the box's (spike 5): a
     complete one ending in a `cost-state` line, one ending in a tool_use with
     no tool_result, a missing one, a sidechain, a half-written last line.
  2. **A dangling or missing transcript starts fresh** under a new uuid, and
     both the agent and the owner are told in one line.
  3. **"No conversation found" retries once, fresh.**
  4. **Login expired is one clear message and no retry.**
  5. **A stall is killed and reported**, the client closed, nothing hanging.
  6. **The security boundary**: credential paths denied for every file tool
     and Bash in both path forms, both `vt` allow forms present, and the
     bridge's own secrets scrubbed from the Claude subprocess environment.
"""
import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import Checks, make_bridge, update
from runner import config
from jobs.tg_bridge import agent
from jobs.tg_bridge.__main__ import AUTH_MESSAGE
from jobs.tg_bridge.agent import (Event, check_transcript, decide_resume, project_slug,
                                  transcript_path)


def entry(kind, content, **kw):
    return json.dumps({"type": kind, "isSidechain": False,
                       "message": {"role": kind, "content": content}, **kw})


COST = json.dumps({"type": "cost-state", "sessionId": "x", "totalCostUSD": 0.01})
USER = entry("user", "what is due today?")
TEXT = entry("assistant", [{"type": "text", "text": "Nothing."}])
USE = entry("assistant", [{"type": "tool_use", "id": "toolu_1", "name": "Bash",
                           "input": {"command": "vt todos"}}])
RESULT = entry("user", [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}])

SAMPLES = {
    "complete": [USER, json.dumps({"type": "attachment"}), USE, RESULT, TEXT,
                 json.dumps({"type": "last-prompt"}), COST],
    "dangling": [USER, USE, json.dumps({"type": "mode"}), COST],
    "sidechain_dangling": [USER, TEXT, json.dumps({
        "type": "assistant", "isSidechain": True, "message": {"content": [
            {"type": "tool_use", "id": "toolu_side", "name": "Read", "input": {}}]}}), COST],
    "no_answer": [USER, COST],
    "truncated": [USER, TEXT, '{"type": "assistant", "message": {"content": [{"ty'],
}


def transcripts(check):
    tmp = pathlib.Path(tempfile.mkdtemp())
    for name, lines in SAMPLES.items():
        (tmp / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    h = {name: check_transcript(tmp / f"{name}.jsonl") for name in SAMPLES}
    check("a complete transcript ending in cost-state is healthy", h["complete"].ok, h["complete"])
    check("a tool_use with no tool_result is dangling", not h["dangling"].ok
          and "tool call" in h["dangling"].reason)
    check("a sidechain's open tool_use does not count", h["sidechain_dangling"].ok)
    check("a transcript with no answer is not resumable", not h["no_answer"].ok)
    check("a half-written last line is not resumable", not h["truncated"].ok)
    missing = check_transcript(tmp / "nope.jsonl")
    check("a missing transcript is not resumable", not missing.ok and "missing" in missing.reason)
    check("the project slug matches Claude Code's (spike 5)",
          project_slug("/home/<user>/vault") in ("-home-<user>-vault",
                                                 project_slug(pathlib.Path("/home/<user>/vault"))))
    check("…and replaces every non-alphanumeric", project_slug("/tmp/a b.c") .count("-") >= 4)


async def fresh_on_dangling(check):
    bridge, transport, fake, tmp = make_bridge()
    s = bridge.sessions.current(4242, 0)
    bridge.sessions.set_started(s.id, True)
    path = transcript_path(tmp / "projects", tmp / "vault", s.claude_session_id)
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(SAMPLES["dangling"]) + "\n")
    mode, why = decide_resume(bridge.sessions.get(s.id), path)
    check("decide_resume says fresh for a dangling transcript", mode == "fresh", (mode, why))
    await bridge.on_updates([update("continue please")])
    await bridge.settle()
    spec = fake.specs[-1]
    check("a fresh uuid was minted", spec.claude_id != s.claude_session_id)
    check("and opened with session_id=, not resume=", not spec.resume)
    check("the new id is persisted", bridge.sessions.get(s.id).claude_session_id == spec.claude_id)
    check("the agent is told it is a fresh session", "Started a fresh session" in fake.prompts[-1])
    check("the owner is told in one line", any("Started a fresh session" in t for t in transport.texts()),
          transport.texts())

    path2 = transcript_path(tmp / "projects", tmp / "vault", spec.claude_id)
    path2.write_text("\n".join(SAMPLES["complete"]) + "\n")
    await bridge.pool.close_all()
    await bridge.on_updates([update("and now?")])
    await bridge.settle()
    check("a healthy transcript is resumed with resume=", fake.specs[-1].resume
          and fake.specs[-1].claude_id == spec.claude_id)


async def no_conversation_retry(check):
    bridge, transport, fake, tmp = make_bridge()
    s = bridge.sessions.current(4242, 0)
    bridge.sessions.set_started(s.id, True)
    path = transcript_path(tmp / "projects", tmp / "vault", s.claude_session_id)
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(SAMPLES["complete"]) + "\n")
    fake.connect_error = lambda spec: (Exception(f"No conversation found with session ID: "
                                                 f"{spec.claude_id}") if spec.resume else None)
    await bridge.on_updates([update("hello again")])
    await bridge.settle()
    check("resume was tried first", fake.specs[0].resume)
    check("then exactly one fresh retry", len(fake.specs) == 2 and not fake.specs[1].resume,
          [(sp.resume, sp.claude_id[:8]) for sp in fake.specs])
    check("and the turn was answered", any("answer to" in t for t in transport.texts()))


async def auth_failure(check):
    bridge, transport, fake, tmp = make_bridge()

    async def logged_out(client, prompt):
        yield Event("result", text="Not logged in · Please run /login",
                    data={"is_error": True, "subtype": "success"})

    fake.behaviour = logged_out
    await bridge.on_updates([update("anything")])
    await bridge.settle()
    check("login expired produces the one clear message",
          any("claude auth login" in t for t in transport.texts()), transport.texts())
    check("with no retry", len(fake.specs) == 1, len(fake.specs))


async def stall(check):
    bridge, transport, fake, tmp = make_bridge(limits={"fresh": 0.1, "between": 0.1})

    async def silent(client, prompt):
        await asyncio.sleep(5)
        yield Event("result", text="too late", data={"is_error": False})

    fake.behaviour = silent
    await bridge.on_updates([update("think hard")])
    await asyncio.wait_for(bridge.settle(), 3)
    check("a silent turn is killed and reported",
          any("stopped producing output" in t for t in transport.texts()), transport.texts())
    check("its client was closed", fake.clients[0].closed)
    check("nothing is left warm", bridge.pool.warm_count() == 0)
    check("the next turn will be told", bridge.sessions.current(4242, 0).aborted_last_run)


def security(check):
    home = str(config.HOME)
    allowed = agent.allowed_tools()
    check("both vt allow forms are present (spike 4)",
          f"Bash({home}/automation/bin/vt *)" in allowed and "Bash(~/automation/bin/vt *)" in allowed,
          allowed)
    deny = agent.disallowed_tools()
    for rule in ("CronCreate", "ScheduleWakeup", "Monitor", "Bash(run_in_background:true)"):
        check(f"deny rule present: {rule}", rule in deny)
    check("no secret-file or env-dump denies remain (owner decision, 2026-10-03)",
          not any(".ssh" in r or ".env" in r or "printenv" in r or r == "Bash(env)"
                  for r in deny), deny)
    for gone in ("deny_rules", "secret_paths", "SECRET_FILES", "ENV_DUMP_DENY"):
        check(f"{gone} is gone from the module", not hasattr(agent, gone))

    check("the permission mode defaults to bypassPermissions",
          agent.permission_mode() == "bypassPermissions")
    ask = json.loads(agent.settings_json())["permissions"]["ask"]
    for rule in ("Bash(rm -rf *)", "Bash(rm -r *)", "Bash(sudo *)", "Bash(git push *)",
                 "Bash(systemctl stop *)", "Bash(systemctl disable *)",
                 "mcp__gws__send_gmail_message"):
        check(f"ask rule present: {rule}", rule in ask)

    environ = {"PATH": "/usr/bin", "TELEGRAM_BOT_TOKEN": "t", "GROQ_API_KEY": "g",
               "OPENAI_API_KEY": "o", "GMAIL_APP_PASSWORD": "p", "SOME_SERVICE_TOKEN": "s",
               "CLAUDE_CODE_OAUTH_TOKEN": "keep", "HOME": "/home/<user>"}
    sub = agent.subprocess_env(environ)
    check("the SDK env blanks the bridge's secrets",
          all(sub.get(k) == "" for k in ("TELEGRAM_BOT_TOKEN", "GROQ_API_KEY", "OPENAI_API_KEY",
                                          "GMAIL_APP_PASSWORD", "SOME_SERVICE_TOKEN")), sub)
    check("but leaves the CLI's own login and ordinary variables alone",
          "CLAUDE_CODE_OAUTH_TOKEN" not in sub and "PATH" not in sub)
    removed = agent.scrub_process_env(environ)
    check("the process env scrub removes the secrets",
          "TELEGRAM_BOT_TOKEN" not in environ and "GROQ_API_KEY" not in environ
          and "OPENAI_API_KEY" not in environ, removed)
    check("and keeps PATH, HOME and the OAuth token",
          environ.get("PATH") and environ.get("HOME") and environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    async def spec_applied():
        bridge, transport, fake, tmp = make_bridge()
        bridge.pool.env = agent.subprocess_env({"TELEGRAM_BOT_TOKEN": "t"})
        bridge.pool.disallowed = agent.disallowed_tools()
        await bridge.on_updates([update("hi")])
        await bridge.settle()
        spec = fake.specs[0]
        check("the client spec carries the deny list", "CronCreate" in spec.disallowed)
        check("the client spec runs bypassPermissions", spec.permission_mode == "bypassPermissions")
        check("the client spec carries the ask list",
              "Bash(sudo *)" in json.loads(spec.settings)["permissions"]["ask"])
        check("the client spec carries the scrubbed env", spec.env.get("TELEGRAM_BOT_TOKEN") == "")
        check("the system prompt carries the channel context, marked untrusted",
              "untrusted metadata" in spec.append)
        check("the turn prompt opens with the untrusted metadata block",
              fake.prompts[0].startswith("Conversation info (untrusted metadata):"))

    asyncio.run(spec_applied())


def run() -> list[str]:
    check = Checks("sessions: transcript health, fresh/resume, auth, stall, security")
    transcripts(check)
    for case in (fresh_on_dangling, no_conversation_retry, auth_failure, stall):
        asyncio.run(case(check))
    security(check)
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
