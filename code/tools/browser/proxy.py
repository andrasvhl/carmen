"""The `browser` MCP server the bridge starts: Playwright MCP, filtered, pointed at Kernel.

    Claude CLI  <-stdio->  this proxy  <-stdio->  node @playwright/mcp --cdp-endpoint http://127.0.0.1:<p>
                                                      |
                              /json/version shim on 127.0.0.1:<p>  ->  Kernel browser (wss, jwt)

Why a proxy and not Playwright MCP alone:

  * **The browser is chosen late.** Playwright MCP connects on its first tool call and fetches
    `<endpoint>/json/version/` to learn the websocket URL (playwright-core urlToWSEndpoint). The
    shim answers that from this conversation's state file, so `vt browser use <site>` picks the
    profile and a timed-out Kernel browser is recreated with the same one.
  * **The gate needs to know what a ref really is.** Every tool result passes through here
    before Claude sees it, so the refs file is written before the model can name a ref from it:
    no race with the bridge's can_use_tool, which reads that file. A page cannot rename the
    button it is about to have clicked.
  * **Defence in depth for code execution.** browser_evaluate and browser_run_code_unsafe are
    removed from tools/list and refused here, as well as in the bridge's gate and the CLI's
    disallowed tools.
  * **The jwt never reaches the transcript.** `jwt=...` is redacted from everything returned.

MCP stdio is newline-delimited JSON-RPC, so this is a line filter, nothing more.
"""
from __future__ import annotations

import http.server
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import state as S

PLAYWRIGHT_MCP = Path.home() / ".local" / "share" / "browser-mcp" / "node_modules" / "@playwright" \
    / "mcp" / "cli.js"
PLAYWRIGHT_MCP_VERSION = "0.0.84"

# Refused here whatever the bridge says. Matched on the bare tool name.
BLOCKED_TOOLS = {"browser_evaluate", "browser_run_code_unsafe", "browser_run_code",
                 "browser_install"}
_BLOCKED_WORDS = ("evaluate", "run_code", "install")

_JWT = re.compile(r"(jwt=)[^&\s\"'\\]+")
# One line of an aria snapshot: `- button "Submit request" [ref=e9]`. Anchored at the start of
# the line, so page text (which appears after `- text:` or inside quotes) cannot forge one.
_REF_LINE = re.compile(r'^[ \t]*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\\n]|\\.)*)")?'
                       r'(?: \[[^\]\n]*\])*? \[ref=(?P<ref>[A-Za-z0-9]+)\]', re.M)
_SNAPSHOT_LINK = re.compile(r"^- \[Snapshot\]\((?P<path>[^)]+)\)", re.M)
_YAML_BLOCK = re.compile(r"### Snapshot\n```yaml\n(?P<body>.*?)\n```", re.S)
_PAGE_URL = re.compile(r"^- Page URL: (?P<url>\S+)", re.M)


def is_blocked(name: str) -> bool:
    bare = name.rsplit("__", 1)[-1]
    return bare in BLOCKED_TOOLS or any(w in bare for w in _BLOCKED_WORDS)


def redact(text: str) -> str:
    return _JWT.sub(r"\1<redacted>", text)


def parse_refs(snapshot: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in _REF_LINE.finditer(snapshot):
        name = (m.group("name") or "").replace('\\"', '"')
        out[m.group("ref")] = [m.group("role"), name]
    return out


def snapshot_text(result_text: str, base: Path) -> str | None:
    """The snapshot a tool result carries: inline yaml (browser_snapshot), or the file an
    action's result links to. None when the result has no snapshot."""
    m = _YAML_BLOCK.search(result_text)
    if m:
        return m.group("body")
    m = _SNAPSHOT_LINK.search(result_text)
    if m:
        p = Path(m.group("path"))
        p = p if p.is_absolute() else base / p
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


NO_BROWSER_HINT = ("No browser is open for this conversation. Run `~/automation/bin/vt browser use "
                   "<site>` first (or `vt browser use --temp` for one with no saved login), then "
                   "retry.")


class Filter:
    """The two line pumps' shared logic, separate from the pipes so it can be tested alone."""

    def __init__(self, key: str, out_base: Path, root: Path | None = None):
        self.key = key
        self.out_base = out_base
        self.root = root
        self.pending: dict = {}                      # request id -> method
        self.shim_port = 0

    def from_client(self, line: str) -> tuple[str | None, str | None]:
        """(line for the child, line straight back to the client)."""
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return line, None
        if not isinstance(msg, dict):
            return line, None
        if "id" in msg and "method" in msg:
            self.pending[msg["id"]] = msg["method"]
            if msg["method"] == "tools/call":
                name = (msg.get("params") or {}).get("name", "")
                if is_blocked(name):
                    self.pending.pop(msg["id"], None)
                    return None, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "content": [{"type": "text", "text": f"{name} is blocked on vault-host: "
                                     "running page JavaScript is not available to the agent."}],
                        "isError": True}})
        return line, None

    def from_child(self, line: str) -> str:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return redact(line)
        if not isinstance(msg, dict) or "method" in msg or "id" not in msg:
            return redact(line)
        method = self.pending.pop(msg["id"], None)
        result = msg.get("result")
        if method == "tools/list" and isinstance(result, dict):
            result["tools"] = [t for t in result.get("tools", []) if not is_blocked(t.get("name", ""))]
        elif method == "tools/call" and isinstance(result, dict):
            texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
            whole = "\n".join(texts)
            snap = snapshot_text(whole, self.out_base)
            if snap is not None:
                m = _PAGE_URL.search(whole)
                url = m.group("url") if m else ""
                S.save_refs(self.key, parse_refs(snap), url, self.root)
            if result.get("isError") and self.shim_port and f"127.0.0.1:{self.shim_port}" in whole:
                result["content"].append({"type": "text", "text": NO_BROWSER_HINT})
        return redact(json.dumps(msg))


def start_shim(key: str, ensure) -> tuple[http.server.ThreadingHTTPServer, int]:
    """127.0.0.1 only, random port. `ensure(key)` -> state dict with cdp_ws_url, or None."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.rstrip("/") != "/json/version":
                self.send_error(404)
                return
            try:
                st = ensure(key)
            except Exception as exc:                 # Kernel down, key missing, profile in use
                body, code = json.dumps({"error": redact(str(exc))}).encode(), 503
            else:
                if not st:
                    body, code = json.dumps({"error": "no browser chosen"}).encode(), 503
                else:
                    body, code = json.dumps({"Browser": "Kernel", "Protocol-Version": "1.3",
                                             "webSocketDebuggerUrl": st["cdp_ws_url"]}).encode(), 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


VAULT = Path.home() / "vault"


def child_command(port: int, out: Path) -> list[str]:
    return ["node", str(PLAYWRIGHT_MCP),
            "--cdp-endpoint", f"http://127.0.0.1:{port}",
            # The workspace root is the vault, fixed: Playwright MCP only uploads files under it
            # (the gate asks for anything else anyway). Without this it would take whatever
            # roots the client advertises, or fall back to its cwd.
            "--ignore-roots",
            "--no-webmcp",                     # pages may not register tools of their own
            "--output-dir", str(out),          # snapshots and screenshots, outside the vault
            "--file-paths", "absolute",        # so the agent can Read the snapshot files
            "--redact-url-params", "jwt",
            "--codegen", "none",
            "--timeout-action", "10000"]


def run(key: str, ensure, close, *, command: list[str] | None = None,
        stdin=None, stdout=None, root: Path | None = None, workspace: Path = VAULT) -> int:
    """Pump until the client hangs up, then close the Kernel browser (which saves the profile)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    out = S.out_dir(key, root)
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    for d in (out.parent.parent, out.parent, out):   # mkdir's mode skips existing parents
        os.chmod(d, 0o700)
    server, port = start_shim(key, ensure)
    filt = Filter(key, out, root)
    filt.shim_port = port
    cmd = command or child_command(port, out)
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             bufsize=1, cwd=str(workspace))
    lock = threading.Lock()

    def emit(line: str) -> None:
        with lock:
            stdout.write(line.rstrip("\n") + "\n")
            stdout.flush()

    def pump_child() -> None:
        for line in child.stdout:
            if line.strip():
                emit(filt.from_child(line))

    reader = threading.Thread(target=pump_child, daemon=True)
    reader.start()

    def stop(*_):
        try:
            child.stdin.close()
        except Exception:
            pass

    signal.signal(signal.SIGTERM, lambda *a: (stop(), sys.exit(0)))
    try:
        for line in stdin:
            if not line.strip():
                continue
            to_child, back = filt.from_client(line)
            if back:
                emit(back)
            if to_child:
                try:
                    child.stdin.write(to_child.rstrip("\n") + "\n")
                    child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    break
    finally:
        stop()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()
        reader.join(2)
        server.shutdown()
        try:
            close(key)
        except Exception as exc:
            print(f"browser proxy: close failed: {redact(str(exc))}", file=sys.stderr)
    return child.returncode or 0
