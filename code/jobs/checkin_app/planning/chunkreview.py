"""Planning, the chunk review (Nov 6, the owner: "evaluate chunk titles as well ... and group substeps
below chunk titles"): today's chunks in the Claude tab's review list, each with its outcome (the
Do card's headline), its estimate and its steps, for feedback the way steps get it.

A chunk is Planning's: the steps of one task picked for one day (do.json). Per chunk he says Right or
Wrong on the outcome and on the chunk estimate, may write the outcome it should be (which becomes
today's outcome, as Edit on the card does) and his minutes, and a note. Each feedback is a row in
`chunk_feedback.jsonl`, the golden set for the outcome phrase (shortsteps.outcome, a small model)
and for chunk estimates; `export` writes `tests/fixtures/chunk_golden.json`. A chunk with feedback
leaves the list until its outcome or steps change. Never writes a note. Shown with the contract flag.

    .venv/bin/python -m jobs.checkin_app.planning.chunkreview export
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

from runner import config

from ..adapters import state

STORE = config.STATE_DIR / "checkin_app" / "chunk_review.json"
FEEDBACK = config.STATE_DIR / "checkin_app" / "chunk_feedback.jsonl"
GOLDEN = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chunk_golden.json"
VERDICTS = ("save", "fine", "notatask")   # notatask: not worth a chunk at all (Nov 6)


def _sig(outcome: str, steps: list[str]) -> str:
    return outcome.strip() + "\n" + "\n".join(s.strip() for s in steps)


def pending(do: dict | None, day: dt.date) -> list[dict]:
    """[{task, title, outcome, his, min, unest, steps: [{id, text, short, state}]}] for today's Do
    chunks without feedback since their outcome or steps last changed."""
    seen = (state.read(STORE, {}).get(day.isoformat()) or {})
    out = []
    for r in (do or {}).get("rows") or []:
        c = r.get("chunk") or {}
        plan = c.get("plan") or []
        if not plan:
            continue
        outcome = c.get("outcome") or ""
        steps = [x.get("text") or "" for x in plan]
        if seen.get(r.get("id")) == _sig(outcome, steps):
            continue
        out.append({"task": r.get("id"), "title": r.get("title") or r.get("id"), "outcome": outcome,
                    "his": bool(c.get("outcome_his")), "min": c.get("planned_min") or c.get("min") or 0,
                    "unest": c.get("unest") or 0,
                    "steps": [{"id": x.get("id", ""), "text": x.get("text", ""), "short": x.get("short", ""),
                               "state": x.get("state", "open")} for x in plan]})
    return out


def feedback(day: dt.date, task: str, shown: dict, votes: dict | None = None, his_outcome: str = "",
             his_min=None, note: str = "", verdict: str = "save") -> dict | None:
    """One chunk's feedback; the row written, or None when it says nothing. `shown` is what the
    card showed: {outcome, his, min, steps}."""
    votes = {k: v for k, v in (votes or {}).items() if k in ("outcome", "est") and v in ("right", "wrong")}
    his_outcome = " ".join(str(his_outcome or "").split())[:140]
    note = " ".join(str(note or "").split())[:1000]
    his_min = int(his_min) if isinstance(his_min, (int, float)) and 0 < his_min <= 1440 else None
    if verdict == "fine":
        votes = {"outcome": "right", "est": "right"}
    if verdict != "notatask" and not votes and not his_outcome and not his_min and not note:
        return None
    steps = [str(s)[:300] for s in (shown.get("steps") or []) if isinstance(s, str)][:12]
    outcome = str(shown.get("outcome") or "")[:200]
    row = {"at": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "day": day.isoformat(), "task": task,
           "outcome": outcome, "outcome_his": bool(shown.get("his")), "steps": steps,
           "min": shown.get("min") if isinstance(shown.get("min"), int) else None,
           "votes": votes, "his_outcome": his_outcome, "his_min": his_min, "note": note,
           "verdict": verdict if verdict in VERDICTS else "save"}
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    new_outcome = his_outcome or outcome

    def f(d):
        d.setdefault(day.isoformat(), {})[task] = _sig(new_outcome, steps)
        return d
    state.update(STORE, f, {})
    return row


def rows() -> list[dict]:
    try:
        return [json.loads(x) for x in FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def golden(feedback_rows=None) -> list[dict]:
    """Per chunk his last feedback: the steps, the outcome shown and his verdict on it, the outcome
    he wrote, the chunk estimate and his minutes, his note."""
    last = {}
    for r in feedback_rows if feedback_rows is not None else rows():
        last[(r.get("day"), r.get("task"))] = r
    out = []
    for r in last.values():
        v = r.get("votes") or {}
        out.append({"task": r.get("task"), "steps": r.get("steps") or [], "shown": r.get("outcome", ""),
                    "shown_his": r.get("outcome_his", False), "outcome_ok": v.get("outcome", ""),
                    "his_outcome": r.get("his_outcome", ""), "min": r.get("min"), "est_ok": v.get("est", ""),
                    "his_min": r.get("his_min"), "note": r.get("note", ""),
                    "not_a_task": r.get("verdict") == "notatask"})
    return out


def export(path=GOLDEN) -> dict:
    ex = golden()
    from runner import clock
    d = {"made": clock.today().isoformat(), "examples": ex}
    pathlib.Path(path).write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"examples": len(ex), "outcomes_wrong": sum(1 for e in ex if e["outcome_ok"] == "wrong"),
            "his_outcomes": sum(1 for e in ex if e["his_outcome"])}


if __name__ == "__main__":
    if sys.argv[1:] == ["export"]:
        print(json.dumps(export()))
    else:
        print(__doc__)
