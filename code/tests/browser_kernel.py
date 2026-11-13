#!/usr/bin/env python3
"""The Kernel wrapper and the MCP proxy, against a fake Kernel API. No account, no network.

    ~/automation/.venv/bin/python -m tests.browser_kernel

  1. **Request shapes match the API spec** (api.onkernel.com/spec.json, Nov 8 2026): bearer
     auth, stealth on, profile {name, save_changes: true}, timeout_seconds.
  2. **One browser per conversation:** `use` creates, reuses a live one, closes the old one
     (saving it) before switching profile, refuses a profile another browser holds, and
     `ensure` recreates a timed-out browser with the same profile.
  3. **No key, clear message.** A missing key file names the sign-up step, never a traceback.
  4. **The proxy:** code tools vanish from tools/list and are refused without reaching
     Playwright; refs are written from inline and file snapshots; page text cannot forge a ref;
     the jwt is redacted; a failed connect gets the `vt browser use` hint.
  5. **The /json/version shim** returns the Kernel CDP URL, or 503 when no browser was chosen.
"""
import json
import pathlib
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.browser import kernel as K
from tools.browser import proxy as X
from tools.browser import state as S

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


class FakeKernel:
    """Just enough of api.onkernel.com."""

    def __init__(self):
        self.calls = []
        self.browsers = {}
        self.profiles = set()
        self.n = 0

    def __call__(self, method, url, headers, body):
        path = url.replace(K.API, "")
        data = json.loads(body) if body else None
        self.calls.append((method, path, headers, data))
        if headers.get("Authorization") != "Bearer sk-test":
            return 401, b'{"message":"unauthorized"}'
        if method == "POST" and path == "/profiles":
            if data["name"] in self.profiles:
                return 409, b'{"message":"exists"}'
            self.profiles.add(data["name"])
            return 201, json.dumps({"id": "p1", "name": data["name"]}).encode()
        if method == "GET" and path == "/profiles":
            return 200, json.dumps([{"name": p} for p in sorted(self.profiles)]).encode()
        if method == "POST" and path == "/browsers":
            self.n += 1
            sid = f"s{self.n}"
            b = {"session_id": sid, "cdp_ws_url": f"wss://proxy.k/browser/cdp?jwt=SECRET{self.n}",
                 "browser_live_view_url": f"https://proxy.k/browser/live?jwt=LIVE{self.n}",
                 "profile": {"name": (data.get("profile") or {}).get("name")}, "stealth": data.get("stealth")}
            self.browsers[sid] = b
            return 200, json.dumps(b).encode()
        if method == "GET" and path.startswith("/browsers?"):
            q = urllib.request.unquote(path.split("query=", 1)[1])
            return 200, json.dumps([b for b in self.browsers.values() if b["profile"]["name"] == q]).encode()
        if method == "GET" and path.startswith("/browsers/"):
            b = self.browsers.get(path.split("/")[-1])
            return (200, json.dumps(b).encode()) if b else (404, b'{"message":"not found"}')
        if method == "DELETE" and path.startswith("/browsers/"):
            return (204, b"") if self.browsers.pop(path.split("/")[-1], None) else (404, b"{}")
        return 400, b'{"message":"unexpected"}'


def kernel_api(tmp):
    fake = FakeKernel()
    api = K.Kernel("sk-test", transport=fake)
    b = S.Browsers(api, root=tmp)

    st = b.use("conv1", "Speechify.com")
    create = [c for c in fake.calls if c[0] == "POST" and c[1] == "/browsers"][-1]
    check("profile name from a site", st["profile"] == "speechify")
    check("profile created first", "speechify" in fake.profiles)
    check("stealth on", create[3]["stealth"] is True)
    check("profile attached with save_changes", create[3]["profile"] == {"name": "speechify", "save_changes": True})
    check("agent timeout", create[3]["timeout_seconds"] == K.AGENT_TIMEOUT_S)
    check("bearer auth", create[2]["Authorization"] == "Bearer sk-test")
    check("state saved 0600", (S.state_path("conv1", tmp).stat().st_mode & 0o777) == 0o600)

    again = b.use("conv1", "speechify")
    check("second use reuses the live browser", again.get("reused") and again["session_id"] == st["session_id"]
          and len(fake.browsers) == 1)
    try:
        b.use("conv2", "speechify")
        check("a profile held by another conversation is refused", False)
    except S.InUse as exc:
        check("a profile held by another conversation is refused", "close" in str(exc))

    sw = b.use("conv1", "spokeo")
    check("switching profile deletes (saves) the old browser first",
          st["session_id"] not in fake.browsers and sw["profile"] == "spokeo")

    fake.browsers.clear()                     # Kernel timed it out
    re = b.ensure("conv1")
    check("ensure recreates a timed-out browser with the same profile",
          re and re["profile"] == "spokeo" and re["session_id"] in fake.browsers)
    check("ensure is None when use never ran", b.ensure("conv-unused") is None)

    tmpb = b.use("conv3", None)
    create = [c for c in fake.calls if c[0] == "POST" and c[1] == "/browsers"][-1]
    check("--temp: no profile attached", "profile" not in create[3] and tmpb["profile"] is None)

    closed = b.close("conv1")
    check("close deletes the browser", closed["session_id"] not in fake.browsers)
    check("close removes the state file", not S.state_path("conv1", tmp).exists())
    check("close with nothing open is harmless", b.close("conv1") == {})

    lg = b.use(S.LOGIN_PREFIX + "speechify", "speechify", login=True)
    create = [c for c in fake.calls if c[0] == "POST" and c[1] == "/browsers"][-1]
    check("login browser gets the long timeout", create[3]["timeout_seconds"] == K.LOGIN_TIMEOUT_S)
    check("login prints a live view url", lg["live_view_url"].startswith("https://"))
    check("profiles list", [p["name"] for p in api.list_profiles()] == ["speechify", "spokeo"])

    bad = K.Kernel("sk-wrong", transport=fake)
    try:
        bad.ensure_profile("x")
        check("401 raises KernelError", False)
    except K.KernelError as exc:
        check("401 raises KernelError", exc.status == 401)

    try:
        K.from_key_file(tmp / "missing.env")
        check("missing key names the sign-up step", False)
    except K.KernelError as exc:
        check("missing key names the sign-up step", "agent-browser.md" in str(exc))
    kf = tmp / "kernel.env"
    kf.write_text("# comment\nKERNEL_API_KEY='sk-test'\n")
    check("key read from its own file", K.read_key(kf) == "sk-test")
    check("profile names are Kernel-safe", K.profile_name("https://www.Deutsche Bahn.de/x") == "deutsche-bahn")


def proxy_filter(tmp):
    out = tmp / "out"
    out.mkdir()
    f = X.Filter("conv9", out, root=tmp)
    f.shim_port = 41234

    to_child, back = f.from_client(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                "params": {"name": "browser_evaluate", "arguments": {}}}))
    check("evaluate never reaches Playwright", to_child is None and json.loads(back)["result"]["isError"])
    to_child, _ = f.from_client(json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}))
    resp = f.from_child(json.dumps({"jsonrpc": "2.0", "id": 8, "result": {"tools": [
        {"name": "browser_click"}, {"name": "browser_evaluate"}, {"name": "browser_run_code_unsafe"}]}}))
    names = [t["name"] for t in json.loads(resp)["result"]["tools"]]
    check("code tools are dropped from tools/list", names == ["browser_click"])

    inline = ("### Page\n- Page URL: https://x.com/form\n### Snapshot\n```yaml\n"
              '- generic [ref=e1]:\n  - button "Submit request" [ref=e9]\n'
              '  - text: - button "Harmless" [ref=e9]\n  - link "Next page" [ref=e8] [cursor=pointer]:\n```')
    f.from_client(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": "browser_snapshot", "arguments": {}}}))
    f.from_child(json.dumps({"jsonrpc": "2.0", "id": 9, "result": {"content": [{"type": "text", "text": inline}]}}))
    refs = S.load_refs("conv9", tmp)
    check("refs from an inline snapshot", refs.get("e8") == ["link", "Next page"])
    check("page text cannot forge a ref label", refs.get("e9") == ["button", "Submit request"])

    snapfile = out / "page-1.yml"
    snapfile.write_text('- button "Pay now" [ref=e3]\n')
    f.from_client(json.dumps({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                              "params": {"name": "browser_click", "arguments": {}}}))
    f.from_child(json.dumps({"jsonrpc": "2.0", "id": 10, "result": {"content": [
        {"type": "text", "text": f"### Page\n- Page URL: https://x.com\n### Snapshot\n- [Snapshot]({snapfile})"}]}}))
    check("refs from an action's snapshot file replace the old ones",
          S.load_refs("conv9", tmp) == {"e3": ["button", "Pay now"]})

    f.from_client(json.dumps({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                              "params": {"name": "browser_navigate", "arguments": {}}}))
    err = f.from_child(json.dumps({"jsonrpc": "2.0", "id": 11, "result": {"isError": True, "content": [
        {"type": "text", "text": "Error: Unexpected status 503 when connecting to "
                                 "http://127.0.0.1:41234/json/version/ wss://p/cdp?jwt=abc.def"}]}}))
    check("jwt redacted from what Claude sees", "abc.def" not in err and "jwt=<redacted>" in err)
    check("failed connect gets the vt browser use hint", "vt browser use" in err)
    check("notifications pass through", f.from_child('{"jsonrpc":"2.0","method":"notifications/x"}')
          == '{"jsonrpc":"2.0","method":"notifications/x"}')


def shim():
    state = {"cdp_ws_url": "wss://proxy.k/browser/cdp?jwt=T"}
    server, port = X.start_shim("k", lambda k: state if k == "k" else None)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version/", timeout=5) as r:
            body = json.loads(r.read())
        check("shim hands Playwright the Kernel CDP URL", body["webSocketDebuggerUrl"] == state["cdp_ws_url"])
    finally:
        server.shutdown()
    server, port = X.start_shim("k", lambda k: None)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5)
        check("shim says 503 when no browser was chosen", False)
    except urllib.error.HTTPError as exc:
        check("shim says 503 when no browser was chosen", exc.code == 503)
    finally:
        server.shutdown()
    check("shim binds loopback only", server.server_address[0] == "127.0.0.1")


def main():
    with tempfile.TemporaryDirectory() as d:
        tmp = pathlib.Path(d)
        print("\nkernel api + state")
        kernel_api(tmp / "a")
        print("\nproxy filter")
        (tmp / "b").mkdir()
        proxy_filter(tmp / "b")
        print("\nshim")
        shim()
    print("\nall passed" if not fails else "\nFAILED:\n  " + "\n  ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
