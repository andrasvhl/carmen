"""Tasks, the review list (Tasks v2, Nov 6, the owner: "a feedback page ... instead of having that on
my one usable page"): every open task whose Next step breaks the step contract, in the Claude tab.
The Do card shows none of it.

Per step he sees why, in words that quote the step (contract.explain), and gives feedback: Right or
Wrong per reason, Right or Wrong on the estimate with his own minutes, a free note, then Save,
Rewrite with Claude (the task chat, his note sent along) or Fine as is. Any feedback hides the step
until it is reworded. Each one is a row in `step_feedback.jsonl`: the golden set (owner decision, Nov 6:
"This could become a golden or example dataset"). `export` turns it into
`tests/fixtures/step_golden.json`, and `tests/checkin_tasks_v2` holds the rules and the estimator to
at least the agreement it records, so a rule change that argues with his labels fails the gate.
The rules never change on their own: the weekly tuning reads the Wrongs and proposes changes for
his OK. Never writes a note. Shown only with the contract flag.

    .venv/bin/python -m jobs.checkin_app.tasks.review export   # rewrite the golden fixture
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import sys

from runner import config

from ..adapters import state

STORE = config.STATE_DIR / "checkin_app" / "step_review.json"
FEEDBACK = config.STATE_DIR / "checkin_app" / "step_feedback.jsonl"
GOLDEN = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "step_golden.json"
OPEN = ("inbox", "open", "in-progress")
STALE_DAYS = 14   # a step dated only by a task captured longer ago is the backfill's, not his to rate
VERDICTS = ("save", "rewrite", "fine", "notatask")   # notatask: not worth a step at all (Nov 6)


def _step(p, sid):
    from tools import steps as S
    body = p.read_text(encoding="utf-8").partition("\n---\n")[2]
    return next((s for s in S.steps_of(body) if s.id == sid), None)


_DATES: dict = {}   # (history mtime, size) -> {(task, step id): date}


def _step_dates() -> dict:
    """{(task, step id): the date of its last added, reworded or kind_changed event}. The history
    starts Nov 4, so an older step has none."""
    from tools import steps as S
    try:
        st = S.HISTORY.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    if _DATES.get("sig") != sig:
        out = {}
        for line in S.HISTORY.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("event") in ("added", "reworded", "kind_changed") and d.get("step"):
                out[(d.get("task"), d["step"])] = str(d.get("at", ""))[:10]
        _DATES.clear()
        _DATES.update(sig=sig, d=out)
    return _DATES["d"]


def _is_task(p) -> bool:
    from tools import steps as S
    try:
        return S.is_task(p.read_text(encoding="utf-8"))
    except OSError:
        return False


def _captured(p) -> str:
    try:
        head = p.read_text(encoding="utf-8").partition("\n---\n")[0]
    except OSError:
        return ""
    m = re.search(r"^captured:\s*(\d{4}-\d{2}-\d{2})", head, re.M)
    return m.group(1) if m else ""


def pending(paths=None) -> list[dict]:
    """[{task, sid, next, why, explain, est, since, since_by}] for every open task whose Next step is
    unclear and has no feedback since it was last worded, newest first (Nov 6, the owner: "i need to
    see recent ones its hard to judge old stuff"): since is the step's last added or reworded date
    ("step"), else its task's captured date ("task")."""
    from .read import read_note
    seen = state.read(STORE, {}).get("fine") or {}
    dates = _step_dates()
    out = []
    for p in paths if paths is not None else sorted(config.INBOX.glob("*.md")):
        try:
            live = read_note(p)
        except Exception:   # noqa: BLE001 - one bad note never hides the rest
            continue
        if p.stem.startswith("_") or not _is_task(p):   # a template, a learning or an idea: not a task (Nov 6)
            continue
        sid = live.get("next_id")
        why = (live.get("unclear") or {}).get(sid) if sid else None
        if live.get("status") not in OPEN or not why or not re.sub(r"\^s[0-9a-f]+", "", live.get("next") or "").strip():
            continue
        if (seen.get(p.stem) or {}).get(sid) == live.get("next"):
            continue
        x = next((x for x in live.get("step_states") or [] if x.get("id") == sid), {})
        since = dates.get((p.stem, sid), "")
        out.append({"task": p.stem, "sid": sid, "next": live.get("next", ""), "why": why,
                    "explain": (live.get("unclear_x") or {}).get(sid, []),
                    "est": x.get("min") if x.get("min") is not None else _minutes(p, sid),
                    "since": since or _captured(p), "since_by": "step" if since else "task"})
    from tools import steps as S
    cut = (dt.datetime.now(S.TZ).date() - dt.timedelta(days=STALE_DAYS)).isoformat()   # his day, Pacific
    out = [r for r in out if r["since_by"] == "step" or not r["since"] or r["since"] >= cut]
    out.sort(key=lambda r: r["since"] or "", reverse=True)
    return out


def _minutes(p, sid):
    try:
        s = _step(p, sid)
    except OSError:
        return None
    return s.planned if s else None


def feedback(p, task: str, sid: str, votes: dict | None = None, est: dict | None = None,
             note: str = "", verdict: str = "save") -> bool:
    """One step's feedback: recorded, the step hidden until reworded. False when the step is gone,
    None when it says nothing (no vote, no minutes, no note: an accidental Save)."""
    from tools import steps as S
    from . import contract
    s = _step(p, sid)
    if s is None:
        return False
    codes = contract.check(s)
    votes = {c: v for c, v in (votes or {}).items() if c in codes and v in ("right", "wrong")}
    est = est if isinstance(est, dict) else {}
    his = est.get("min")
    est_row = {"shown": s.planned, "vote": est.get("vote") if est.get("vote") in ("right", "wrong") else "",
               "his_min": int(his) if isinstance(his, (int, float)) and 0 < his <= 600 else None}
    line = S.format_line(S.Step(state=s.state, kind=s.kind, text=s.text, est=s.est, box=s.box, event=s.event))
    if verdict not in ("fine", "notatask") and not votes and not est_row["vote"] and not est_row["his_min"] and not str(note or "").strip():
        return None
    row = {"at": dt.datetime.now(S.TZ).isoformat(timespec="seconds"), "task": task, "sid": sid, "line": line,
           "kind": s.kind, "text": s.text, "codes": codes, "votes": votes, "estimate": est_row,
           "note": " ".join(str(note or "").split())[:1000], "verdict": verdict if verdict in VERDICTS else "save"}
    FEEDBACK.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    shown = s.display()

    def f(d):
        d.setdefault("fine", {}).setdefault(task, {})[sid] = shown
        return d
    state.update(STORE, f, {})
    S.append("", task, "carmen", {"event": "feedback", "sid": sid, "text": s.text, "codes": codes,
                                  "votes": votes, "verdict": row["verdict"]})
    return True


def mark_fine(p, task: str, sid: str) -> bool:
    """Fine as is, the one-tap form: every reason it was flagged for is wrong."""
    s = _step(p, sid)
    if s is None:
        return False
    from . import contract
    return feedback(p, task, sid, {c: "wrong" for c in contract.check(s)}, None, "", "fine")


def rows() -> list[dict]:
    try:
        return [json.loads(x) for x in FEEDBACK.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError):
        return []


def golden(feedback_rows=None, now_text=None) -> list[dict]:
    """The labelled examples: per step his last feedback, the reasons he called right ("fire") and
    wrong ("not"), his minutes, his note, and the step's wording now when it was rewritten."""
    last = {}
    for r in feedback_rows if feedback_rows is not None else rows():
        last[(r.get("task"), r.get("sid"), r.get("text"))] = r
    out = []
    for (task, sid, _), r in last.items():
        v = r.get("votes") or {}
        e = r.get("estimate") or {}
        ex = {"line": r["line"], "kind": r.get("kind") or "Next",
              "fire": sorted(c for c, x in v.items() if x == "right"),
              "not": sorted(c for c, x in v.items() if x == "wrong"),
              "his_min": e.get("his_min") or (e.get("shown") if e.get("vote") == "right" else None),
              "note": r.get("note", ""), **({"not_a_task": True} if r.get("verdict") == "notatask" else {})}
        final = now_text(task, sid) if now_text else None
        if final and final != r.get("text"):
            ex["final"] = final
        if ex["fire"] or ex["not"] or ex["his_min"] or ex["note"] or ex.get("not_a_task"):
            out.append(ex)
    return out


def agreement(examples: list[dict]) -> tuple[int, int, list[str]]:
    """(labels the rules and the estimator agree with, labels, the disagreements). An estimate
    agrees within half to double his minutes."""
    from tools import steps as S
    from . import contract, estimates
    ok = n = 0
    off = []
    for ex in examples:
        s = S.parse_line(ex["line"])
        if s is None:
            continue
        s.kind = ex.get("kind") or s.kind
        got = set(contract.check(s))
        for c in ex.get("fire", []):
            n += 1
            ok += c in got
            if c not in got:
                off.append(f"should flag {c}: {s.text[:60]}")
        for c in ex.get("not", []):
            n += 1
            ok += c not in got
            if c in got:
                off.append(f"should not flag {c}: {s.text[:60]}")
        if ex.get("his_min"):
            n += 1
            kind, m = estimates.by_rules(s.text)
            good = ex["his_min"] / 2 <= m <= ex["his_min"] * 2
            ok += good
            if not good:
                off.append(f"estimate {m} min, his {ex['his_min']}: {s.text[:60]}")
    return ok, n, off


def export(path=GOLDEN) -> dict:
    def now_text(task, sid):
        p = config.INBOX / f"{task}.md"
        try:
            s = _step(p, sid)
        except OSError:
            return None
        return s.text if s else None
    ex = golden(now_text=now_text)
    ok, n, _ = agreement(ex)
    from runner import clock
    d = {"made": clock.today().isoformat(), "baseline": ok, "labels": n, "examples": ex}
    pathlib.Path(path).write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"examples": len(ex), "agree": ok, "labels": n}


if __name__ == "__main__":
    if sys.argv[1:] == ["export"]:
        print(json.dumps(export()))
    else:
        print(__doc__)
