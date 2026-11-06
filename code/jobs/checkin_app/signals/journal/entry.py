"""Signals, journal: the meditation entry as the app shows it. The questions come from the template
(the journal's only copy of them, per Meditation Journal.md); an entry is read section by section.
"""
from __future__ import annotations

import datetime as dt
import re

from runner import config


# --- the meditation journal in the app ---------------------------------------------
# owner decision, Oct 30: "move daily meditation to this app as well, it should still fill out
# obsidian page as it does right now - everything stays same, just interface changes".
# The questions are read from the template (the journal's only copy of them, per
# Meditation Journal.md), and the entry is written exactly as QuickAdd would.


JOURNAL = config.VAULT / "Areas" / "Personal" / "Meditation Journal"

TEMPLATE = JOURNAL / "_Daily Meditation Template.md"


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) for every ## section, in order."""
    parts = re.split(r"^## (.+)$", text, flags=re.M)
    return [(parts[i].strip(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def template_questions() -> list[dict]:
    out = []
    for head, body in _sections(TEMPLATE.read_text(encoding="utf-8")):
        checks = re.findall(r"^- \[[ x]\] (.+)$", body, re.M)
        kind = ("checks" if checks else "numbered" if re.search(r"^\d+\.\s*$", body, re.M)
                else "line" if "(one line)" in head else "list")
        out.append({"heading": head, "kind": kind, "items": checks,
                    "label": re.sub(r"\s*#\w+", "", head).strip()})
    return out


def _body_to_value(kind: str, body: str):
    lines = [l.rstrip() for l in body.strip().splitlines() if l.strip()]
    if kind == "checks":
        return {m.group(2): m.group(1) == "x" for m in (re.match(r"^- \[([ x])\] (.+)$", l) for l in lines) if m}
    if kind == "numbered":
        return "\n".join(re.sub(r"^\d+\.\s*", "", l) for l in lines if re.sub(r"^\d+\.\s*", "", l))
    if kind == "list":
        return "\n".join(re.sub(r"^[-*]\s*", "", l) for l in lines if re.sub(r"^[-*]\s*", "", l))
    return " ".join(lines)


def _value_to_body(q: dict, value) -> str:
    if q["kind"] == "checks":
        value = value if isinstance(value, dict) else {}
        return "\n" + "\n".join(f"- [{'x' if value.get(i) else ' '}] {i}" for i in q["items"]) + "\n"
    rows = [r.strip() for r in str(value or "").splitlines() if r.strip()]
    if q["kind"] == "numbered":
        rows = rows or ["", ""]
        return "\n" + "\n".join(f"{n}. {r}" for n, r in enumerate(rows, 1)) + "\n"
    if q["kind"] == "list":
        return "\n" + "\n".join(f"- {r}" for r in rows) + "\n" if rows else "\n\n"
    return ("\n" + " ".join(rows) + "\n") if rows else "\n\n"


def journal_entry(day: dt.date) -> dict:
    qs = template_questions()
    p = JOURNAL / f"{day.isoformat()}.md"
    have = dict(_sections(p.read_text(encoding="utf-8", errors="replace"))) if p.exists() else {}
    return {"date": day.isoformat(), "exists": p.exists(),
            "questions": [{**q, "value": _body_to_value(q["kind"], have.get(q["heading"], ""))} for q in qs]}
