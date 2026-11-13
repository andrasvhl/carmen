"""Per-conversation browser state on disk, and the open / reuse / close logic.

STATE_DIR/browser/<key>.json        {"profile", "session_id", "cdp_ws_url", "live_view_url", ...}
STATE_DIR/browser/<key>.refs.json   ref -> [role, name] from the latest page snapshot
STATE_DIR/browser/out/<key>/        Playwright MCP's snapshots and screenshots (never the vault:
                                    Obsidian Sync on vault-host carries unsupported file types)

<key> is BROWSER_SESSION, which the bridge sets to the Claude session id, so the Bash side
(`vt browser use`) and the MCP side (proxy.py) of one conversation find the same file. The
gate in jobs/tg_bridge/browser_gate.py reads the refs file to learn what a click really hits.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from runner import config

from . import kernel as K

ROOT = config.STATE_DIR / "browser"
ENV_KEY = "BROWSER_SESSION"
LOGIN_PREFIX = "login-"


def safe_key(key: str) -> str:
    key = re.sub(r"[^A-Za-z0-9._-]", "", key or "")[:80]
    if not key:
        raise ValueError(f"no {ENV_KEY}: this only runs inside a bridge conversation")
    return key


def state_path(key: str, root: Path | None = None) -> Path:
    return (root or ROOT) / f"{safe_key(key)}.json"


def refs_path(key: str, root: Path | None = None) -> Path:
    return (root or ROOT) / f"{safe_key(key)}.refs.json"


def out_dir(key: str, root: Path | None = None) -> Path:
    return (root or ROOT) / "out" / safe_key(key)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)            # holds a CDP URL with a jwt in it
    os.replace(tmp, path)


def load(key: str, root: Path | None = None) -> dict:
    try:
        return json.loads(state_path(key, root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save(key: str, data: dict, root: Path | None = None) -> None:
    _write(state_path(key, root), data)


def save_refs(key: str, refs: dict, url: str = "", root: Path | None = None) -> None:
    _write(refs_path(key, root), {"at": time.time(), "url": url, "refs": refs})


def load_refs(key: str, root: Path | None = None) -> dict:
    try:
        return json.loads(refs_path(key, root).read_text(encoding="utf-8")).get("refs", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def load_page_url(key: str, root: Path | None = None) -> str:
    """The page the latest snapshot came from ("" if none yet): the gate's allowed-sites check."""
    try:
        return str(json.loads(refs_path(key, root).read_text(encoding="utf-8")).get("url") or "")
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        return ""


def used_path(key: str, root: Path | None = None) -> Path:
    return (root or ROOT) / f"{safe_key(key)}.used"


def mark_used(key: str, root: Path | None = None) -> None:
    """The browser touched this conversation. Never cleared: /new starts a new key."""
    try:
        used_path(key, root).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        used_path(key, root).touch()
    except (OSError, ValueError):
        pass


def was_used(key: str, root: Path | None = None) -> bool:
    try:
        return used_path(key, root).exists() or state_path(key, root).exists()
    except ValueError:
        return False


class InUse(Exception):
    """The profile is open in another conversation; two writers would clobber each other."""


class Browsers:
    """Open, reuse and close the one Kernel browser a conversation has."""

    def __init__(self, api: K.Kernel, root: Path | None = None):
        self.api = api
        self.root = root

    def _alive(self, st: dict) -> bool:
        return bool(st.get("session_id")) and self.api.get_browser(st["session_id"]) is not None

    def _holder(self, profile: str, mine: str | None) -> str | None:
        """Another live session id already holding this profile, from Kernel's own list."""
        for b in self.api.active_browsers(profile):
            p = (b.get("profile") or {}).get("name")
            if p == profile and b.get("session_id") != mine:
                return b.get("session_id")
        return None

    def use(self, key: str, site: str | None, *, login: bool = False) -> dict:
        """Point this conversation at a profile (None = a throwaway browser with no profile),
        opening its Kernel browser now so a live-view link exists to send."""
        profile = K.profile_name(site) if site else None
        st = load(key, self.root)
        if st.get("session_id") and st.get("profile") == profile and self._alive(st):
            st["reused"] = True
            return st
        if st.get("session_id"):
            self.close(key)                     # saves the old profile before switching
        if profile:
            self.api.ensure_profile(profile)
            holder = self._holder(profile, None)
            if holder:
                raise InUse(f"profile {profile} is open in another browser ({holder}); close it "
                            f"there (`vt browser close`) or wait for it to time out")
        b = self.api.create_browser(profile, timeout_s=K.LOGIN_TIMEOUT_S if login else
                                    K.AGENT_TIMEOUT_S)
        st = {"profile": profile, "session_id": b["session_id"], "cdp_ws_url": b["cdp_ws_url"],
              "live_view_url": b.get("browser_live_view_url") or "", "created": time.time(),
              "login": login}
        save(key, st, self.root)
        return st

    def ensure(self, key: str) -> dict | None:
        """What the proxy's /json/version asks: a live browser for this conversation, recreated
        with the same profile if Kernel timed the old one out. None if `use` never ran."""
        st = load(key, self.root)
        if "profile" not in st:
            return None
        if self._alive(st):
            return st
        return self.use(key, st["profile"])

    def close(self, key: str) -> dict:
        """Delete the browser (which saves its profile). The state file goes too."""
        st = load(key, self.root)
        if st.get("session_id"):
            self.api.delete_browser(st["session_id"])
        for p in (state_path(key, self.root), refs_path(key, self.root)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        return st
