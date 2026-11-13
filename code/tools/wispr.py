#!/usr/bin/env python3
"""Read Wispr Flow meetings from Wispr's official remote MCP server.

  vt wispr auth                 one-time browser sign-in (on vault-host: ssh -L 8771:127.0.0.1:8771)
  vt wispr status               is the token present and still refreshable
  vt wispr tools                the tools the server exposes, with their input schemas
  vt wispr call TOOL ['{json}']  call one tool, print the raw result

Cloud only, so it works with the MacBook off: Notetaker records on the Mac, but
once a meeting has uploaded, nothing here touches the Mac. The local flow.sqlite
is an undocumented schema; this server is the supported surface.

Auth is OAuth 2.1 against mcp-auth.wisprflow.com: dynamic client registration
(public client, no secret), then authorization code + PKCE on a loopback
redirect (port 8771; 8765 is the plan-today panel). The device-code grant is
advertised but Wispr's page for it is broken. offline_access
gives a refresh token; refreshes rotate it, so every refresh is written back.
Stdlib only, like the Canvas pull.
"""
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MCP_URL = "https://api.wisprflow.ai/connect/mcp"
AUTH = "https://mcp-auth.wisprflow.com/oauth2"
SCOPE = "openid offline_access"
TOKEN = Path.home() / ".config" / "vt" / "wispr.json"
PROTOCOL = "2025-06-18"
REDIRECT = "http://127.0.0.1:8771/callback"


class AuthError(RuntimeError):
    """The stored token cannot be used or refreshed: a human has to sign in again."""


# ---------------------------------------------------------------- token store

def _load() -> dict:
    if not TOKEN.exists():
        raise AuthError(f"no Wispr token at {TOKEN}: run `vt wispr auth`")
    return json.loads(TOKEN.read_text())


def _save(tok: dict) -> None:
    TOKEN.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN.with_suffix(".tmp")
    tmp.write_text(json.dumps(tok, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(TOKEN)


def _post_form(url: str, data: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return {"_status": e.code, **json.loads(body)}
        except json.JSONDecodeError:
            return {"_status": e.code, "error": body[:300]}


def _register() -> str:
    req = urllib.request.Request(
        f"{AUTH}/register",
        data=json.dumps({
            "client_name": "vt wispr (vault import)",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["client_id"]


def _store_grant(client_id: str, grant: dict) -> dict:
    tok = {
        "client_id": client_id,
        "access_token": grant["access_token"],
        "refresh_token": grant.get("refresh_token"),
        "expires_at": time.time() + int(grant.get("expires_in", 3600)) - 60,
        "refreshed_at": time.time(),
    }
    _save(tok)
    return tok


def access_token() -> str:
    """A valid access token, refreshing (and saving the rotated refresh token) if needed."""
    tok = _load()
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise AuthError("token expired and no refresh token stored: run `vt wispr auth`")
    grant = _post_form(f"{AUTH}/token", {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "resource": MCP_URL,
    })
    if "access_token" not in grant:
        raise AuthError(f"refresh refused ({grant.get('error')}): run `vt wispr auth`")
    if not grant.get("refresh_token"):
        grant["refresh_token"] = tok["refresh_token"]
    return _store_grant(tok["client_id"], grant)["access_token"]


def auth() -> int:
    """Authorization code + PKCE with a loopback redirect on 127.0.0.1:8771.

    Wispr's device-code page is broken (its login page needs an external_auth_id
    that only the redirect flow passes, Oct 16 2026), so this is the only flow
    that works. On vault-host, run it through `ssh -L 8771:127.0.0.1:8771
    vault-host` so the browser's redirect on the laptop reaches the box.
    """
    client_id = _register()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    link = f"{AUTH}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "scope": SCOPE, "state": state, "resource": MCP_URL,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    print(f"Open this link and sign in with your Wispr Flow account:\n\n  {link}\n\nWaiting...",
          flush=True)

    got = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            if "code" in q or "error" in q:
                got.update(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Wispr sign-in received. You can close this tab.".encode())

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 8771), Callback)
    server.timeout = 600
    while not got:
        server.handle_request()
    server.server_close()

    if got.get("state") != state or "code" not in got:
        print(f"sign-in failed: {got.get('error', 'state mismatch')}", file=sys.stderr)
        return 1
    grant = _post_form(f"{AUTH}/token", {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": MCP_URL})
    if "access_token" not in grant:
        print(f"token exchange failed: {grant}", file=sys.stderr)
        return 1
    tok = _store_grant(client_id, grant)
    print(f"Signed in. Token saved to {TOKEN}"
          f"{'' if tok['refresh_token'] else ' (WARNING: no refresh token issued)'}.")
    return 0


# ---------------------------------------------------------------- MCP client

class Session:
    """Minimal streamable-HTTP MCP client: initialize once, then tools/list and tools/call."""

    def __init__(self):
        self.token = access_token()
        self.sid = None
        self.next_id = 0
        init = self._rpc("initialize", {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "vt-wispr", "version": "1"}})
        self.server = init.get("serverInfo", {})
        self._rpc("notifications/initialized", None, notify=True)

    def _rpc(self, method, params, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": f"Bearer {self.token}",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(MCP_URL, data=json.dumps(msg).encode(), headers=headers)
        try:
            r = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AuthError("server rejected the token: run `vt wispr auth`") from e
            raise RuntimeError(f"{method}: HTTP {e.code} {e.read()[:300]!r}") from e
        with r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            if notify:
                return None
            body = r.read().decode()
            if "text/event-stream" in r.headers.get("Content-Type", ""):
                body = _last_sse_message(body, self.next_id)
        reply = json.loads(body)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    def tools(self) -> list[dict]:
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, name: str, args: dict | None = None) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": args or {}})


def _last_sse_message(body: str, want_id: int) -> str:
    for event in body.split("\n\n"):
        data = "\n".join(l[5:].lstrip() for l in event.splitlines() if l.startswith("data:"))
        if data and json.loads(data).get("id") == want_id:
            return data
    raise RuntimeError("no JSON-RPC reply in the event stream")


def text_of(result: dict) -> str:
    """The text content of a tools/call result, joined."""
    return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")


PAGE = 40000  # server-enforced maximum per transcript range
_MORE = re.compile(r"\n\n\(\.\.\.truncated, \d+ chars remaining; "
                   r"continue with view_transcript\.start_char=(\d+)\.\.\.\)\n<<<END TRANSCRIPT>>>$")
_END = "\n<<<END TRANSCRIPT>>>"


def _page(raw: str) -> tuple[str, int | None]:
    """One transcript range -> (exact text of the range, next offset or None).

    Shape, measured 2026-10-16: a `<<<PARTICIPANT NAMES ... >>>` guard line, the
    range itself, then the server appends "\\n" and either the truncation
    marker or `<<<END TRANSCRIPT>>>`. Splitting on lines instead of this exact
    shape cut one word in half at every page boundary.
    """
    if raw.startswith("<<<"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
    m = _MORE.search(raw)
    if m:
        return raw[:m.start()], int(m.group(1))
    if raw.endswith(_END):
        raw = raw[:-len(_END)]
    return raw.removesuffix("\n"), None


def meeting(session: "Session", meeting_id: str) -> dict:
    """get_meeting with the complete transcript, every range fetched and joined."""
    args = {"meeting_id": meeting_id, "view_transcript": {"start_char": 0, "char_limit": PAGE}}
    first = json.loads(text_of(session.call("get_meeting", args)))
    raw = first.get("transcript")
    if not raw:
        first["transcript"] = ""
        return first
    parts, offset = [], 0
    while True:
        text, nxt = _page(raw)
        parts.append(text)
        if nxt is None:
            break
        if nxt != offset + len(text):
            raise RuntimeError(f"{meeting_id}: transcript range at {offset} returned {len(text)} "
                               f"chars but the server continues at {nxt}; page shape changed")
        offset = nxt
        args["view_transcript"]["start_char"] = offset
        raw = json.loads(text_of(session.call("get_meeting", args)))["transcript"] or ""
    first["transcript"] = "".join(parts)
    return first


def meetings(session: "Session", **filters) -> list[dict]:
    """search_meetings, every page."""
    out, cursor = [], None
    while True:
        args = {"limit": 200, **filters, **({"cursor": cursor} if cursor else {})}
        page = json.loads(text_of(session.call("search_meetings", args)))
        out += page.get("meetings") or []
        cursor = page.get("next_cursor")
        if not page.get("has_more") or not cursor:
            return out


# ---------------------------------------------------------------- CLI

def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    try:
        if cmd == "auth":
            return auth()
        if cmd == "status":
            tok = _load()
            left = tok["expires_at"] - time.time()
            print(f"token {TOKEN}: access {'valid ' + str(int(left)) + 's' if left > 0 else 'expired'}, "
                  f"refresh token {'present' if tok.get('refresh_token') else 'MISSING'}")
            s = Session()
            print(f"server: {s.server.get('name')} {s.server.get('version', '')} (reachable, token accepted)")
            return 0
        if cmd == "tools":
            for t in Session().tools():
                print(f"## {t['name']}\n{t.get('description', '').strip()}\n"
                      f"{json.dumps(t.get('inputSchema', {}).get('properties', {}), indent=1)}\n")
            return 0
        if cmd == "call" and len(argv) >= 2:
            res = Session().call(argv[1], json.loads(argv[2]) if len(argv) > 2 else {})
            print(text_of(res) or json.dumps(res, indent=1))
            return 0
    except AuthError as e:
        print(f"wispr: {e}", file=sys.stderr)
        return 3
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
