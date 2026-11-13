#!/usr/bin/env python3
"""End to end without Kernel: the real proxy and Playwright MCP, driving a local headless Chromium
through the same /json/version shim the Kernel browser uses. Manual, not in the offline suite.

    BROWSER_SMOKE_LIBS=/tmp/bsmoke/libs/usr/lib/x86_64-linux-gnu \\
        ~/automation/.venv/bin/python -m tests.browser_smoke

vault-host lacks Chromium's GUI libraries (libatk, libgbm, ...); for this test they were
unpacked from Ubuntu debs with `apt-get download` + `dpkg-deb -x`, no sudo. Kernel needs none of
this: the browser runs in Kernel's cloud. ~300 MB RAM for under a minute, then everything closes.

Checks: tools/list has no code tools; navigate, snapshot, type, file upload (the remote-browser
path: Playwright reads the file locally and sends its bytes, since connectOverCDP is not
"local"), the refs file the gate reads, a gated click on Submit, evaluate refused by the proxy,
reconnection after the browser goes away, and the close callback when the client hangs up.
"""
import http.server
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobs.tg_bridge import browser_gate as G
from tools.browser import state as S

CHROME = sorted(pathlib.Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
PAGE = """<html><head><title>Smoke form</title></head><body><h1>Opt-out form</h1>
<form action="/done.html" method="get" enctype="multipart/form-data">
<label>Email <input name="email" type="email"></label>
<label>Upload <input id="f" name="f" type="file"></label>
<a href="/next.html">Next page</a><button type="submit">Submit request</button></form>
<p id="picked"></p><script>document.getElementById('f').onchange=e=>{document.getElementById('picked')
.textContent='picked '+e.target.files[0].name+' '+e.target.files[0].size}</script></body></html>"""

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({str(detail)[:300]})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_chrome(port, profile):
    env = dict(os.environ)
    if os.environ.get("BROWSER_SMOKE_LIBS"):
        env["LD_LIBRARY_PATH"] = os.environ["BROWSER_SMOKE_LIBS"]
    p = subprocess.Popen([str(CHROME[-1]), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                          "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", f"--user-data-dir={profile}",
                          "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                return p, json.loads(r.read())["webSocketDebuggerUrl"]
        except OSError:
            time.sleep(0.2)
    p.kill()
    raise SystemExit("chromium did not start (missing libraries? see the docstring)")


class Client:
    def __init__(self, cmd, env):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env,
                                  cwd=str(ROOT))
        self.n = 0
        self.call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "smoke", "version": "0"}})
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.p.stdin.flush()

    def call(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("proxy closed")
            m = json.loads(line)
            if "method" in m and "id" in m:           # roots/list from the server
                self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"roots": []}}) + "\n")
                self.p.stdin.flush()
            elif m.get("id") == self.n:
                return m

    def tool(self, name, args=None):
        r = self.call("tools/call", {"name": name, "arguments": args or {}})["result"]
        return "\n".join(c.get("text", "") for c in r.get("content", [])), bool(r.get("isError"))


RUNNER = """
import json, sys
sys.path.insert(0, {root!r})
from tools.browser import proxy, state as S
S.ROOT = __import__('pathlib').Path({state!r})
cdp = {cdp!r}
def ensure(k):
    return {{"cdp_ws_url": open(cdp).read().strip()}}
def close(k):
    open({closed!r}, "w").write(k)
sys.exit(proxy.run({key!r}, ensure, close, workspace=__import__('pathlib').Path({ws!r})))
"""


def main():
    if not CHROME:
        print("no chromium headless shell in ~/.cache/ms-playwright; skipped")
        return 0
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        site = tmp / "site"
        site.mkdir()
        (site / "index.html").write_text(PAGE)
        (site / "done.html").write_text("<html><title>Done</title><body>Thanks</body></html>")
        vault_file = tmp / "upload-me.txt"
        vault_file.write_text("hello from vault-host\n")
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(site), **k)
        http.server.SimpleHTTPRequestHandler.log_message = lambda *a: None
        web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{web.server_address[1]}"

        port = free_port()
        chrome, ws = start_chrome(port, tmp / "ud")
        cdp_file = tmp / "cdp"
        cdp_file.write_text(ws)
        key = "smoke-conv"
        runner = RUNNER.format(root=str(ROOT), state=str(tmp / "state"), cdp=str(cdp_file),
                               closed=str(tmp / "closed"), key=key, ws=str(tmp))
        S.ROOT = tmp / "state"
        client = Client([sys.executable, "-c", runner], dict(os.environ))
        try:
            names = [t["name"] for t in client.call("tools/list", {})["result"]["tools"]]
            check("tools/list served through the proxy", "browser_navigate" in names)
            check("no evaluate or run_code in the list", not any("evaluate" in n or "run_code" in n for n in names))

            text, err = client.tool("browser_navigate", {"url": base + "/index.html"})
            check("navigate over CDP via the shim", not err and "Smoke form" in text, text)
            text, err = client.tool("browser_snapshot")
            refs = S.load_refs(key)
            check("refs file written from the snapshot", any(v == ["button", "Submit request"] for v in refs.values()), refs)
            ref = {tuple(v): k for k, v in refs.items()}
            email, upload, submit = ref.get(("textbox", "Email")), ref.get(("button", "Upload")), ref.get(("button", "Submit request"))
            nxt = ref.get(("link", "Next page"))

            d = G.decide(G.PREFIX + "browser_type", {"target": email, "text": "a@b.c"}, active=True, refs=refs)
            check("gate: typing the email is allowed", d.kind == "allow")
            text, err = client.tool("browser_type", {"element": "Email", "target": email, "text": "owner@example.com"})
            check("type", not err, text)

            text, err = client.tool("browser_click", {"element": "Upload", "target": upload})
            check("click Upload opens the file chooser", not err and "File chooser" in text, text)
            d = G.decide(G.PREFIX + "browser_file_upload", {"paths": [str(vault_file)]}, active=True, refs=refs,
                         upload_roots=[tmp])
            check("gate: upload of a file inside the allowed root is allowed", d.kind == "allow")
            text, err = client.tool("browser_file_upload", {"paths": [str(vault_file)]})
            check("file upload over CDP (bytes sent, browser not local)", not err, text)
            text, err = client.tool("browser_find", {"text": "picked upload-me.txt"})
            check("the page received the file", "picked upload-me.txt 22" in text, text)

            print("    refs now:", S.load_refs(key))
            d = G.decide(G.PREFIX + "browser_click", {"element": "the next button", "target": submit},
                         active=True, refs=S.load_refs(key))
            check("gate: click on the real Submit asks", d.kind == "ask", d)
            d = G.decide(G.PREFIX + "browser_click", {"element": "Next page", "target": nxt},
                         active=True, refs=S.load_refs(key))
            check("gate: click on Next page is allowed", d.kind == "allow", d)

            text, err = client.tool("browser_evaluate", {"function": "() => document.cookie"})
            check("evaluate refused by the proxy", err and "blocked" in text, text)

            text, err = client.tool("browser_click", {"element": "Submit request", "target": submit})
            check("click Submit (after the tap it would need)", not err and "Done" in text, text)
            check("no jwt or ws URL leaks", "jwt=" not in text.replace("jwt=<redacted>", ""))

            chrome.kill()
            chrome.wait()
            chrome, ws = start_chrome(port, tmp / "ud2")
            cdp_file.write_text(ws)
            text, err = client.tool("browser_navigate", {"url": base + "/index.html"})
            if err:
                text, err = client.tool("browser_navigate", {"url": base + "/index.html"})
                check("after the browser went away, the next call reconnects (second try)", not err, text)
            else:
                check("after the browser went away, the next call reconnects", True)
        finally:
            client.p.stdin.close()
            client.p.wait(20)
            chrome.kill()
            web.shutdown()
        check("the close callback ran when the client hung up", (tmp / "closed").read_text() == key
              if (tmp / "closed").exists() else False)
        check("snapshots went to the state dir, not the cwd", (tmp / "state" / "out" / key).exists())
    print("\nall passed" if not fails else "\nFAILED:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
