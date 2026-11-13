"""Kernel's REST API, the four calls this needs. Stdlib only, so the MCP wrapper starts fast.

Checked against https://api.onkernel.com/spec.json and kernel.sh/docs on Nov 8 2026:

    POST   /browsers               {stealth, profile: {name, save_changes}, timeout_seconds, name}
                                   -> session_id, cdp_ws_url (jwt in the query), browser_live_view_url
    GET    /browsers/{id}          404 once deleted
    DELETE /browsers/{id}          204; with save_changes this is what writes the profile back
    GET    /browsers?query=<name>  active sessions, searchable by profile name
    POST   /profiles {name}        201, or 409 when it already exists

Auth is `Authorization: Bearer <KERNEL_API_KEY>`. The key lives in ~/.config/kernel/kernel.env
(credentials.md); it is read from there, never from the environment, because the bridge scrubs
every *_API_KEY from the agent's environment.

Lifecycle facts that shape the wrapper (docs: introduction/create, browsers/termination): a
browser stays alive while a CDP or live-view client is connected; 5 s after the last one leaves
it goes to standby (not billed); `timeout_seconds` after that it is deleted, and a deleted or
timed-out browser with save_changes writes its whole state into the profile. Only one browser
may write a profile at a time: saving replaces, it never merges.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

API = "https://api.onkernel.com"
KEY_FILE = Path.home() / ".config" / "kernel" / "kernel.env"
KEY_NAME = "KERNEL_API_KEY"

# Inactivity before Kernel deletes a browser nobody is connected to. Long enough for the owner to
# open a live-view link he was just sent; short enough that a forgotten one costs nothing.
AGENT_TIMEOUT_S = 300
LOGIN_TIMEOUT_S = 900


class KernelError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kernel API {status}: {message}")
        self.status = status


# (method, url, headers, body bytes or None) -> (status, body bytes)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def read_key(path: Path = KEY_FILE) -> str:
    """The API key from its own 0600 file. Empty when the owner has not signed up yet."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{KEY_NAME}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def profile_name(site: str) -> str:
    """`Speechify.com` -> `speechify`. Kernel allows letters, digits, dot, underscore, hyphen."""
    import re
    s = site.strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    s = re.sub(r"^www\.", "", s)
    s = re.sub(r"\.(com|net|org|io|co|app|de|ai)$", "", s)
    s = re.sub(r"[^a-z0-9._-]+", "-", s).strip("-.")
    if not s:
        raise ValueError(f"no usable profile name in {site!r}")
    return s[:64]


class Kernel:
    def __init__(self, api_key: str, *, transport: Transport = urllib_transport, base: str = API):
        if not api_key:
            raise KernelError(0, f"no {KEY_NAME} in {KEY_FILE}: the owner's sign-up step "
                                 "(see _system/agent-browser.md)")
        self.key = api_key
        self.transport = transport
        self.base = base.rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None,
              ok: tuple[int, ...] = (200, 201, 204)) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        status, raw = self.transport(method, self.base + path, headers, data)
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"message": raw[:200].decode("utf-8", "replace")}
        if status not in ok:
            msg = payload.get("message") or payload.get("error") or str(payload)[:200]
            raise KernelError(status, str(msg))
        return status, payload if isinstance(payload, dict) else {"items": payload}

    # ---- profiles ---------------------------------------------------------------------

    def ensure_profile(self, name: str) -> bool:
        """Create the profile if it is missing. True when it was created now."""
        status, _ = self._call("POST", "/profiles", {"name": name}, ok=(200, 201, 409))
        return status != 409

    def list_profiles(self) -> list[dict]:
        _, payload = self._call("GET", "/profiles")
        return payload.get("items", [])          # the API returns a bare array

    # ---- browsers ---------------------------------------------------------------------

    def create_browser(self, profile: str | None, *, timeout_s: int = AGENT_TIMEOUT_S,
                       name: str | None = None) -> dict:
        body: dict = {"stealth": True, "timeout_seconds": timeout_s}
        if profile:
            body["profile"] = {"name": profile, "save_changes": True}
        if name:
            body["name"] = name
        _, payload = self._call("POST", "/browsers", body)
        if not payload.get("session_id") or not payload.get("cdp_ws_url"):
            raise KernelError(200, "create returned no session_id / cdp_ws_url")
        return payload

    def get_browser(self, session_id: str) -> dict | None:
        """None once it is gone (deleted, timed out)."""
        try:
            _, payload = self._call("GET", f"/browsers/{session_id}")
        except KernelError as exc:
            if exc.status == 404:
                return None
            raise
        return None if payload.get("deleted_at") else payload

    def delete_browser(self, session_id: str) -> None:
        """Ends the session; with save_changes this is what saves the profile."""
        self._call("DELETE", f"/browsers/{session_id}", ok=(200, 204, 404))

    def active_browsers(self, query: str = "") -> list[dict]:
        path = "/browsers" + (f"?query={urllib.request.quote(query)}" if query else "")
        _, payload = self._call("GET", path)
        return payload.get("items", [])


def from_key_file(path: Path = KEY_FILE, **kw) -> Kernel:
    return Kernel(read_key(path), **kw)
