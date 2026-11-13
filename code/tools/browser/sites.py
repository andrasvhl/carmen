"""Which sites each browser profile may use without a tap (Nov 8).

The gate (jobs/tg_bridge/browser_gate.py) asks before the browser opens a site outside the
profile's list, and before any click, typing or upload on a page that has wandered off it. That
closes the quiet way out for injected instructions: loading `evil.com/?data=...`. When the owner
taps Allow on such a site, remember() adds it, so each new site costs one tap, ever (the owner,
Nov 8: "Im really worried that I have to allow things all the time").

  DEFAULTS                       seeded per profile, in code
  STATE_DIR/browser/sites.json   {profile: [host, ...]}, what his Allow taps added

A host on the list also covers its subdomains: "speechify.com" allows app.speechify.com.
A throwaway browser (`vt browser use --temp`) has no profile; its sites live in the
conversation's state file and go with it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from . import state as S

DEFAULTS: dict[str, list[str]] = {
    "speechify": ["speechify.com"],
}


def host_of(url: str) -> str:
    try:
        host = (urlparse(str(url or "")).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def on_list(host: str, hosts) -> bool:
    host = (host or "").lower()
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def _file(root: Path | None = None) -> Path:
    return (root or S.ROOT) / "sites.json"


def _saved(root: Path | None = None) -> dict:
    try:
        data = json.loads(_file(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def allowed(profile: str | None, key: str | None = None, root: Path | None = None) -> list[str]:
    """The hosts this conversation's browser may use without a tap."""
    if profile:
        return sorted(set(DEFAULTS.get(profile, [])) | set(_saved(root).get(profile, [])))
    if key:
        return list(S.load(key, root).get("extra_hosts", []))
    return []


def remember(profile: str | None, host: str, key: str | None = None, root: Path | None = None) -> None:
    """The owner allowed `host`: add it to the profile (or, for a throwaway browser, the conversation)."""
    host = host_of("https://" + host) if "://" not in host else host_of(host)
    if not host:
        return
    if profile:
        data = _saved(root)
        hosts = set(data.get(profile, []))
        if host in hosts:
            return
        data[profile] = sorted(hosts | {host})
        path = _file(root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    elif key:
        st = S.load(key, root)
        if st:
            st["extra_hosts"] = sorted(set(st.get("extra_hosts", [])) | {host})
            S.save(key, st, root)
