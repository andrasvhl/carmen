#!/usr/bin/env python3
"""The real SDK and httpx, without a network or a CLI: do our shapes still fit theirs?

    .venv/bin/python -m tests.tg_bridge.sdk_shapes

Skipped (and says so) on an interpreter without claude-agent-sdk or httpx,
which is the point of every other test here. Under the repo .venv it checks:

  1. SdkClient builds ClaudeAgentOptions the SDK accepts: session_id on a
     first turn, resume after, setting_sources untouched, permission_mode
     "bypassPermissions" with the ask list in settings, the claude_code
     preset with our append.
  2. _convert maps real SDK message objects to our Events, including the
     interrupt result (terminal_reason "aborted_streaming", spike 6).
  3. The can_use_tool adapter returns allow-once with no permission updates
     (never the CLI's suggestions: they write settings.local.json, spike 3).
  4. HttpxTransport constructs, and the httpx errors it maps exist.
"""
import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import TOKEN, Checks


def run() -> list[str]:
    check = Checks("sdk shapes: real claude-agent-sdk and httpx objects")
    try:
        import claude_agent_sdk as sdk
        import httpx  # noqa: F401
    except ImportError as exc:
        print(f"  skip  {exc.name} is not installed in this interpreter")
        return []
    from jobs.tg_bridge import agent
    from jobs.tg_bridge.telegram import HttpxTransport

    async def approve(name, inp):
        return name == "Bash", "no"

    def spec(resume):
        return agent.ClientSpec(claude_id="0b9c6a3e-1111-4222-8333-444455556666", resume=resume,
                                cwd="/tmp", cli_path="/usr/bin/false", append="STYLE",
                                allowed=agent.allowed_tools(), disallowed=agent.disallowed_tools(),
                                env={"TELEGRAM_BOT_TOKEN": ""}, model=None, can_use_tool=approve,
                                permission_mode=agent.permission_mode(),
                                settings=agent.settings_json())

    first = agent.SdkClient(spec(False))._options()
    later = agent.SdkClient(spec(True))._options()
    check("first turn: session_id set, resume unset", first.session_id and not first.resume)
    check("later turns: resume set, session_id unset", later.resume and not later.session_id)
    check("setting_sources is left at the default (spike 2)", first.setting_sources is None)
    check("permission_mode is bypassPermissions", first.permission_mode == "bypassPermissions")
    check("system prompt is the claude_code preset plus our append",
          first.system_prompt == {"type": "preset", "preset": "claude_code", "append": "STYLE"})
    check("the scheduling denies reach the options", "CronCreate" in first.disallowed_tools)
    ask = json.loads(first.settings or "{}").get("permissions", {}).get("ask", [])
    check("the ask list reaches the options' settings",
          "Bash(rm -rf *)" in ask and "mcp__gws__send_gmail_message" in ask, first.settings)
    check("the scrubbed env reaches the options", first.env.get("TELEGRAM_BOT_TOKEN") == "")

    events = agent._convert(sdk.AssistantMessage(content=[
        sdk.TextBlock(text="hi"), sdk.ToolUseBlock(id="t1", name="Read", input={"file_path": "x"})],
        model="m"))
    check("assistant text and tool_use convert", [e.kind for e in events] == ["text", "tool"])
    events = agent._convert(sdk.UserMessage(content=[sdk.ToolResultBlock(tool_use_id="t1")]))
    check("tool results convert", events and events[0].kind == "tool_result" and events[0].id == "t1")
    result = sdk.ResultMessage(subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
                               is_error=True, num_turns=1, session_id="s")
    try:
        result.terminal_reason = "aborted_streaming"
    except Exception:
        pass
    events = agent._convert(result)
    check("the interrupt result carries terminal_reason",
          events[0].kind == "result" and events[0].data["terminal_reason"] == "aborted_streaming")

    client = agent.SdkClient(spec(False))
    ctx = sdk.ToolPermissionContext(suggestions=[])
    allow = asyncio.run(client._can_use_tool("Bash", {"command": "ls"}, ctx))
    deny = asyncio.run(client._can_use_tool("Write", {}, ctx))
    check("allow is allow-once, no permission updates",
          isinstance(allow, sdk.PermissionResultAllow) and allow.updated_permissions is None)
    check("deny carries the message", isinstance(deny, sdk.PermissionResultDeny) and deny.message == "no")

    # The owner-task actor, against a stand-in ClaudeSDKClient built from real
    # SDK message types: a turn streams, a second turn reuses the client, and
    # a forced close from another task unwinds it in the task that connected.
    class StandIn:
        instances = []

        def __init__(self, options):
            self.options, self.prompts, self.closed_in, self.task = options, [], None, None
            StandIn.instances.append(self)

        async def connect(self):
            self.task = asyncio.current_task()

        async def query(self, prompt):
            self.prompts.append(prompt)

        async def receive_response(self):
            if self.prompts[-1] == "hang":
                await asyncio.sleep(30)
            yield sdk.AssistantMessage(content=[sdk.TextBlock(text="hello")], model="m")
            yield sdk.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                    is_error=False, num_turns=1, session_id="s", result="hello")

        async def interrupt(self):
            pass

        async def disconnect(self):
            self.closed_in = asyncio.current_task()

    async def drain(c):
        return [e.kind async for e in c.events()]

    async def actor():
        real = sdk.ClaudeSDKClient
        sdk.ClaudeSDKClient = StandIn
        try:
            c = agent.SdkClient(spec(False))
            await c.connect()
            out = []
            for prompt in ("one", "two"):
                await c.query(prompt)
                out.append(await drain(c))
            await c.query("hang")
            consumer = asyncio.create_task(drain(c))
            await asyncio.sleep(0.05)
            await c.disconnect(force=True)
            drained = await asyncio.wait_for(consumer, 5)
            inst = StandIn.instances[-1]
            return out, drained, inst.closed_in is inst.task and inst.task is not None
        finally:
            sdk.ClaudeSDKClient = real

    out, drained, same_task = asyncio.run(actor())
    check("a turn streams text then result", out[0] == ["text", "result"], out)
    check("the same client serves a second turn", out[1] == ["text", "result"], out)
    check("a forced close ends a hung turn's event stream", drained == [], drained)
    check("disconnect ran in the task that connected (SDK requirement)", same_task)

    async def transport():
        t = HttpxTransport(TOKEN)
        await t.close()
        return True

    check("HttpxTransport constructs and closes", asyncio.run(transport()))
    return check.fails


if __name__ == "__main__":
    fails = run()
    print(f"\n{'FAILED: ' + ', '.join(fails) if fails else 'all passed'}")
    sys.exit(1 if fails else 0)
