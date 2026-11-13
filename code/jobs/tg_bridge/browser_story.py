"""What a browser approval shows (Nov 9).

owner decision, Nov 9: "for the approvals of browser interactions, i have no idea whats happening based on
the message, I want to know what data is submitted in browser". The card used to show the tool's
raw input ({"target": "f11e3516", "element": "Next step button"}): no site, no reason, and nothing
of what had been typed, because typing is allowed without a tap (browser_gate.decide).

So every browser call this conversation makes is noted per page (PageLog): what was typed, chosen
or filled, in order, starting fresh when the page changes. An approval then reads as

    On cvs.com/immunizations/schedule
    Click "Next step button"
    Entered on this page:
    - First name: the owner
    - Card number: (hidden)

with the gate's reason in plain words as the line under it. Values of fields that look like
passwords, card numbers, security codes or ID numbers are never shown, only "(hidden)"; a value
that is itself 13 to 19 digits is hidden too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

PREFIX = "mcp__browser__"
MAX_ITEMS = 15
MAX_VALUE = 80

_SECRET_LABEL = re.compile(
    r"pass(word|code|wort)?|kennwort|\bpin\b|cvv|cvc|csc|security code|sicherheitscode|card ?(number|no)|"
    r"kartennummer|credit|kredit|iban|routing|account (number|no)|kontonummer|\bssn\b|social security|"
    r"passport|reisepass|\botp\b|one.time|verification code|2fa|token|secret", re.I)
_DIGITS = re.compile(r"^[\d\s-]{13,23}$")


def _hidden(label: str, value: str) -> bool:
    if _SECRET_LABEL.search(label or ""):
        return True
    v = (value or "").strip()
    return bool(_DIGITS.match(v)) and 13 <= sum(c.isdigit() for c in v) <= 19


def _clip(v: str) -> str:
    v = " ".join(str(v or "").split())
    return v if len(v) <= MAX_VALUE else v[:MAX_VALUE - 1] + "…"


def _show(label: str, value) -> str:
    value = ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else str(value if value is not None else "")
    return "(hidden)" if _hidden(label, value) else f"\"{_clip(value)}\""


def _label(tool_input: dict) -> str:
    return _clip(tool_input.get("element") or tool_input.get("name") or tool_input.get("target")
                 or tool_input.get("ref") or "a field")


def entries(tool_name: str, tool_input: dict) -> list[tuple[str, str]]:
    """(field, shown value) pairs this call puts into the page; [] for calls that enter nothing."""
    name = tool_name[len(PREFIX):] if tool_name.startswith(PREFIX) else tool_name
    ti = tool_input or {}
    if name == "browser_type":
        lab = _label(ti)
        return [(lab, _show(lab, ti.get("text")))]
    if name == "browser_select_option":
        lab = _label(ti)
        return [(lab, _show(lab, ti.get("values") or ti.get("value")))]
    if name == "browser_fill_form":
        out = []
        for f in ti.get("fields") or []:
            if isinstance(f, dict):
                lab = _clip(f.get("name") or f.get("element") or f.get("target") or "a field")
                out.append((lab, _show(lab, f.get("value"))))
        return out
    if name in ("browser_file_upload", "browser_drop"):
        paths = ti.get("paths") or []
        return [("file", ", ".join(str(p).rsplit("/", 1)[-1] for p in paths) or "(none)")]
    return []


def page(url: str) -> str:
    """cvs.com/immunizations/schedule: host without www, the path, no query (it can carry data)."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return ""
    host = (u.hostname or "").removeprefix("www.")
    path = (u.path or "").rstrip("/")
    return _clip(host + path) if host else ""


def action(tool_name: str, tool_input: dict) -> str:
    name = tool_name[len(PREFIX):] if tool_name.startswith(PREFIX) else tool_name
    ti = tool_input or {}
    if name == "browser_click":
        return f"Click \"{_label(ti)}\""
    if name == "browser_type":
        lab = _label(ti)
        return f"Type {_show(lab, ti.get('text'))} into \"{lab}\"" + (" and press Enter" if ti.get("submit") else "")
    if name == "browser_press_key":
        return f"Press {ti.get('key') or 'a key'}"
    if name == "browser_navigate":
        return f"Open {page(ti.get('url')) or ti.get('url')}"
    if name == "browser_handle_dialog":
        return "Accept the page's dialog" + (f" (answer \"{_clip(ti.get('promptText'))}\")" if ti.get("promptText") else "")
    if name in ("browser_file_upload", "browser_drop"):
        return "Upload " + (", ".join(str(p).rsplit("/", 1)[-1] for p in ti.get("paths") or []) or "a file")
    if name == "browser_select_option":
        lab = _label(ti)
        return f"Choose {_show(lab, ti.get('values') or ti.get('value'))} in \"{lab}\""
    if name == "browser_fill_form":
        return "Fill in the form below"
    return name.replace("browser_", "").replace("_", " ").capitalize()


def plain_reason(reason: str) -> str:
    r = reason or ""
    if r.startswith("the click target mentions"):
        return "The button looks like it submits, sends, pays or confirms something."
    if r.startswith("the click target is not a named element"):
        return "Carmen can't tell for sure what this button does."
    if "not on this browser's allowed sites" in r or "outside this browser's allowed sites" in r:
        return "This site isn't on the allowed list yet; Allow adds it."
    if r.startswith("typing with submit") or r.startswith("pressing"):
        return "This presses Enter, which can submit the form."
    if r.startswith("accepting a dialog"):
        return "The page is asking for a confirmation."
    if r.startswith("uploading a file"):
        return "The file comes from outside the vault."
    return r[:1].upper() + r[1:] if r else ""


@dataclass
class PageLog:
    url: str = ""
    items: list[tuple[str, str]] = field(default_factory=list)

    def note(self, tool_name: str, tool_input: dict, page_url: str) -> None:
        if page_url and page(page_url) != page(self.url):
            self.url, self.items = page_url, []
        self.url = self.url or page_url
        self.items = (self.items + entries(tool_name, tool_input))[-MAX_ITEMS:]

    def describe(self, tool_name: str, tool_input: dict, page_url: str, reason: str) -> tuple[str, str, str]:
        """(shown, why, speak): the card's text, the line under it, and the short spoken version."""
        url = page_url or self.url
        items = self.items if not page_url or page(page_url) == page(self.url) else []
        lines = [f"On {page(url)}" if page(url) else "In the browser", action(tool_name, tool_input)]
        new = entries(tool_name, tool_input) if tool_name.endswith("browser_fill_form") else []
        shown_items = items + new
        if shown_items:
            lines.append("Entered on this page:" if not new else "Fields:")
            lines += [f"- {f}: {v}" for f, v in shown_items]
        elif tool_name.endswith("browser_click"):
            lines.append("Nothing was entered on this page.")
        spoken = action(tool_name, tool_input).replace("\"", "")
        if page(url):
            spoken += " on " + page(url).split("/")[0]
        if shown_items:
            spoken += ", after entering " + ", ".join(f for f, _ in shown_items[:4])
        return "\n".join(lines), plain_reason(reason), spoken
