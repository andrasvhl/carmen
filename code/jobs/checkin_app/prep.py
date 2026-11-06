"""Claude prepares tasks up to the point where it needs him (owner decision, Oct 30).

"it should, if not already done for a specific task, already prepare the things that it
can, up until it needs my input, and then in claude ask for my input if it needs sth
from me concretely" ... "it should not stop at the first decision, rather do everything
it can in parallel, that is independent of decision".

One headless Claude turn per task: it may read the vault and search the web, nothing
that writes. Python lands the answer: a `## Prepared by Claude` section in the task note
(done so far, needs from you, after your answer) and a state file the app shows in the
Claude tab. Started by the app once per pre-run (a few tasks, high priority first), and
for one task after he answers a question, which continues it.

  python -m jobs.checkin_app.prep                # the next few unprepared tasks
  python -m jobs.checkin_app.prep --task "<id>"  # one task, e.g. after an answer
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from runner import config
from runner.ask import RESEARCH_TOOLS, AskFailed, ask

from .adapters import notes

from .adapters.clock import TZ, _label
from .planning.read import all_rows, build_view
from .signals.read import load_prerun
from .tasks.actions import write_dod
from .tasks.read import read_note
from .adapters.notes import task_path
from .adapters.taplog import log_event

STATE = config.STATE_DIR / "checkin_app" / "prep"
PER_RUN = 3          # sessions per pre-run: the Claude limit was hit on Oct 30
TIMEOUT = 900
# A failed preparation waits before it is tried again, and stops for the day after MAX_FAILS
# (Nov 6: a pending answer stayed pending after a failure, so every 60 s view restarted it;
# 328 failures on Nov 1 and 2, one task 116 times).
MAX_FAILS = 3
BACKOFF_MIN = (5, 30, 120)


def retry_due(st: dict, now: dt.datetime | None = None) -> bool:
    """False while a failed preparation is backing off."""
    ra = st.get("retry_after") if isinstance(st, dict) else None
    if not ra:
        return True
    try:
        return (now or dt.datetime.now(TZ)) >= dt.datetime.fromisoformat(ra)
    except (TypeError, ValueError):
        return True

PROMPT = """You prepare one of the owner's tasks so that he only has to decide. The task note,
the morning pre-run's row for it (context, why, the steps it says you can take) and
any earlier preparation and answers of his are below.

How to work:
0. Work toward the task's Definition of Done: the "DoD:" line at the top of its Next action
   block (owner decision, Nov 1). If there is none and the data does not mark it a two-minute task, first
   draft one in one line from the note, the vault and the context: the scope (the whole thing,
   not just the title), with whom, and what "well" means. Return it as "dod". Whatever of those
   three you cannot fill, ask as the first entry in "asks" instead of guessing. Never change a
   confirmed DoD; if news makes it look wrong, say so in "summary".
1. Split the task into its steps. Mark each one as independent, or blocked by a
   decision or fact only the owner has.
2. Do EVERY independent step now, not only the first one: research and compare on the
   web, look things up in the vault (read-only), work out the details he will need
   (amounts, dates, links, addresses, what to bring, what to say).
3. Where a decision has two or three branches and preparing each is cheap, prepare all
   of them, so his choice takes seconds.
4. Ask only what blocks the rest: one question per decision, concrete, with 2 to 4
   answer options, and for each option the steps it unlocks.
5. You can only read and research. Never claim to have sent, paid, booked, RSVPed,
   signed up or deleted anything; those stay his.
6. Every fact needs a source: a URL, a vault note, or the given context. Say plainly
   when something is uncertain or could not be found.
7. If an earlier preparation exists, build on it: do not redo finished steps; continue
   from his answers.
8. He reads this on a phone. "did" is one plain sentence, at most 20 words, on what you
   did. Put the decision that unblocks the most first in "asks". Option labels are at
   most 6 words each; the detail goes in "unlocks". "decide" says in one sentence what
   he has to decide or do next, even when "asks" is empty.

Reply with JSON only, no prose around it:
{"dod": "your one-line Definition of Done, only when the note has none, else empty",
 "did": "one sentence, at most 20 words",
 "decide": "one sentence: the decision or step that is his",
 "summary": "two or three lines, what is ready",
 "done": [{"step": "...", "result": "...", "source": "..."}],
 "asks": [{"q": "...", "options": ["...", "..."], "unlocks": {"<option>": "what you will do then"}}],
 "then": ["steps you will take once he answers, or that only he can do"]}
"""


def _key(row: dict) -> str:
    return hashlib.sha1(row["id"].encode()).hexdigest()[:16]


STALE_DAYS = 7     # a preparation older than this is redone even if nothing changed
NEW_GROUP = "New from mail and WhatsApp"


def _note_sig(p: Path | None) -> str:
    """The task note minus what machines rewrite on their own (the pre-run's Current
    context block, this section and its log line), so only a real change counts."""
    if not p or not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"^## (Prepared by Claude \(.*?\)|Current context)\s*\n.*?(?=^## |\Z)", "", text, flags=re.S | re.M)
    text = "\n".join(l for l in text.splitlines() if "prepared by Claude" not in l)
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def stale_reason(row: dict, st: dict, generated: str) -> str:
    """Why a task needs preparing again, or "" (owner decision, Oct 31: not every run, only when
    it went stale or something touched the task)."""
    if not retry_due(st):
        return ""
    if not st.get("result"):
        return "new"
    if st.get("pending"):
        return "answer"
    if _note_sig(task_path(row["id"])) != st.get("note_sig"):
        return "note changed"
    if row.get("group") == NEW_GROUP and st.get("generation") != generated:
        return "new message"
    try:
        if dt.datetime.now(TZ) - dt.datetime.fromisoformat(st["at"]) > dt.timedelta(days=STALE_DAYS):
            return "old"
    except (KeyError, ValueError):
        return "old"
    return ""


def load_state(row_id: str) -> dict | None:
    return load_state_file(STATE / f"{_key({'id': row_id})}.json")


def load_state_file(p: Path) -> dict | None:
    """A state file, or None when it is missing, torn or not an object (Nov 1)."""
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return st if isinstance(st, dict) else None


def save_state(row: dict, st: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / f"{_key(row)}.json"
    from .adapters import state
    state.write(p, st)   # locked, a tmp name of its own (Nov 6)


def _parse(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    d = json.loads(text[text.index("{"): text.rindex("}") + 1])
    return {"dod": " ".join(str(d.get("dod") or "").split())[:300],
            "did": str(d.get("did", ""))[:200],
            "decide": str(d.get("decide", ""))[:200],
            "summary": str(d.get("summary", ""))[:300],
            "done": [x for x in d.get("done", []) if isinstance(x, dict) and x.get("step")][:12],
            "asks": [x for x in d.get("asks", []) if isinstance(x, dict) and x.get("q")][:3],
            "then": [str(x) for x in d.get("then", []) if x][:8]}


def _section(res: dict, when: dt.datetime) -> str:
    lines = [f"## Prepared by Claude ({_label(when.date())} {when.strftime('%H:%M')})", ""]
    if res["summary"]:
        lines += [res["summary"], ""]
    if res["done"]:
        lines.append("**Done so far**")
        lines += [f"- {d['step']}: {d.get('result', '')}" + (f" ({d['source']})" if d.get("source") else "")
                  for d in res["done"]]
        lines.append("")
    if res["asks"]:
        lines.append("**Needs from you**")
        for a in res["asks"]:
            lines.append(f"- {a['q']}" + (f" Options: {' / '.join(a.get('options', []))}" if a.get("options") else ""))
            for k, v in (a.get("unlocks") or {}).items():
                lines.append(f"  - {k}: {v}")
        lines.append("")
    if res["then"]:
        lines.append("**After your answer**")
        lines += [f"- {t}" for t in res["then"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@notes.cas
def write_note(p: Path, res: dict, when: dt.datetime) -> None:
    """Replace the task's Prepared by Claude section (or add it before the Log) and log it."""
    text = notes.read_text(p)
    sec = _section(res, when)
    text = re.sub(r"## Prepared by Claude \(.*?\)\n.*?(?=^## |\Z)", "", text, flags=re.S | re.M).rstrip() + "\n"
    if re.search(r"^## Log\s*$", text, re.M):
        text = re.sub(r"^## Log\s*$", sec + "\n## Log", text, count=1, flags=re.M)
    else:
        text = text.rstrip() + "\n\n" + sec
    n_ask = len(res["asks"])
    line = (f"- {_label(when.date())} · prepared by Claude: {len(res['done'])} steps done"
            + (f", {n_ask} question{'s' if n_ask != 1 else ''} for you" if n_ask else "") + " · check-in app")
    text = text.rstrip() + "\n" + line + "\n" if re.search(r"^## Log\s*$", text, re.M) else text.rstrip() + "\n\n## Log\n" + line + "\n"
    notes._write(p, text, "prep")   # steps it ticked or added go into the step history as prep


def raw_row(row: dict) -> dict:
    for _, items in load_prerun().get("rows", []):
        for r in items:
            if (r.get("lk") or r.get("n")) == row["id"]:
                return r
    return {}


def two_minute(p: Path | None, raw: dict) -> bool:
    """SYSTEM.md's two-minute task, which gets no DoD: the whole task, not its next step.
    timeEstimate of 2 or less, or the pre-run's e at 2m or less, or exactly one open step
    marked (2 min) or less."""
    live = read_note(p) if p else {}
    te = str(live.get("time_est", "")).strip()
    if te.isdigit():
        return int(te) <= 2
    e = re.match(r"^\s*(\d+)\s*m?\s*$", str(raw.get("e") or ""))
    if e:
        return int(e.group(1)) <= 2
    one = re.search(r"\((\d+)\s*min\)\s*$", live.get("next", ""))
    return bool(live) and not live.get("steps") and bool(one) and int(one.group(1)) <= 2


def prepare(row: dict, generated: str = "") -> dict | None:
    p = task_path(row["id"])
    prev = load_state(row["id"]) or {}
    raw = raw_row(row)
    data = ("=== TWO-MINUTE TASK (no DoD) ===\n" + ("yes" if two_minute(p, raw) else "no") +
            "\n\n=== TASK NOTE ===\n" + (p.read_text(encoding="utf-8") if p else "(no task note yet)") +
            "\n\n=== PRE-RUN ROW ===\n" + json.dumps({k: raw.get(k) for k in ("n", "m", "s", "nx", "ctx", "cp", "wf")},
                                                    ensure_ascii=False, indent=1) +
            "\n\n=== EARLIER PREPARATION AND HIS ANSWERS ===\n" +
            json.dumps({"result": prev.get("result"), "answers": prev.get("answers", [])}, ensure_ascii=False, indent=1))
    now = dt.datetime.now(TZ)
    try:
        res = _parse(ask(PROMPT, data=data, tools=RESEARCH_TOOLS, cwd=config.VAULT, timeout=TIMEOUT))
    except (AskFailed, ValueError) as exc:
        today = now.date().isoformat()
        fails = (prev.get("fails", 0) if prev.get("fail_day") == today else 0) + 1
        give_up = fails >= MAX_FAILS
        log_event("prep_failed", id=row["id"], detail=str(exc)[:300], fails=fails, gave_up=give_up)
        if give_up:
            from . import alerts
            alerts.alert("prep gave up", f"{row['id']}: {MAX_FAILS} failures today, last: {str(exc)[:200]}")
        save_state(row, {**prev, "id": row["id"], "error": str(exc)[:300], "at": now.isoformat(timespec="minutes"),
                         "fails": fails, "fail_day": today,
                         # An answer still waiting is retried after the backoff, never after MAX_FAILS a day.
                         "pending": bool(prev.get("pending")) and not give_up,
                         "retry_after": (now + dt.timedelta(minutes=BACKOFF_MIN[min(fails, len(BACKOFF_MIN)) - 1])
                                         if not give_up else
                                         dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0), TZ)
                                         ).isoformat(timespec="minutes")})
        return None
    if p:
        write_note(p, res, now)
        if res.get("dod") and not read_note(p).get("dod"):
            # Its guess goes into the note marked as such; he confirms it in one tap (Nov 1).
            try:
                write_dod(p, res["dod"], False, now.date(), src="prepared by Claude")
                log_event("signal", signal="dod_proposed", id=row["id"], title=row.get("title", ""),
                          dod=res["dod"], src="prep", test=False, claude={})
            except ValueError:
                pass
    st = {"id": row["id"], "title": row.get("title", ""), "at": now.isoformat(timespec="minutes"),
          "note_sig": _note_sig(p), "generation": generated, "result": res,
          "answers": prev.get("answers", []), "pending": False}
    save_state(row, st)
    log_event("signal", signal="prepared", id=row["id"], title=row.get("title", ""),
              done=len(res["done"]), asks=len(res["asks"]))
    return st


def candidates(view: dict) -> list[dict]:
    """Tasks Claude can push that are stale: today's selection first (the pre-selection
    and what he added), then the rest of the Claude tab in its ranking."""
    t = view["today"]
    pool = ([t["one"]] if t["one"] else []) + t["needs"] + t["selection"] + view["claude"]
    out, seen = [], set()
    for r in pool:
        if r["id"] in seen or not r.get("prep"):
            continue
        seen.add(r["id"])
        if stale_reason(r, load_state(r["id"]) or {}, view.get("generated", "")):
            out.append(r)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task")
    a = ap.parse_args(argv)
    view = build_view(load_prerun())
    if a.task:
        rows = [r for r in all_rows(view) if r["id"] == a.task][:1]
    else:
        rows = candidates(view)[:PER_RUN]
    for r in rows:
        prepare(r, view.get("generated", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
