"""Twice daily: what every Claude session worked on, as a rolling 7-day page.

Every new session starts blind to what earlier ones did, and much of the owner's
work now happens through AI: on the Mac, in Telegram, over Remote Control. This
job reads all of it and writes one section per day into
`_system/claude/session-log.md`. Its main purpose is the task system: each day
names the tasks and build items a session touched, and the ones it probably
left stale.

## Sources, in order of weight

1. **Session digests** (`tools/session_digest.py`): the box's own transcripts,
   digested in place, plus the Mac's, which a launchd agent pushes to
   `~/.local/state/automation/session-digests/mac/`. Headless job runs are
   excluded there.
2. **Vault git history** for the day: which notes changed, by anyone, including
   edits on the phone that no session saw.
3. **`~/automation` git log**: what code shipped.

## How it writes

The pattern is red_notebook's: the model returns one day section and nothing
else; Python places it. The model never sees the page, so it cannot damage the
other days. A day is rewritten until a run after its end has written it (then it
is marked final and left alone). Days older than WINDOW_DAYS move, verbatim, to
the top of `session-log-history.md`. Revert path is the runner's vault commit.

The job never edits a task. It names them; the planner, enrichment or the owner act.

    ssh vault-host '~/automation/bin/run-job session_log --dry-run'
    python -m jobs.session_log --preview --days 7 --out /tmp/preview.md
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from runner import config
from runner.ask import AskFailed, ask
from runner.job import IMPORTANT, ROUTINE, YOU, Job, log
from tools import session_digest

TZ = ZoneInfo("America/Los_Angeles")
WINDOW_DAYS = 7
PAGE = config.VAULT / "_system" / "claude" / "session-log.md"
HISTORY = config.VAULT / "_system" / "claude" / "session-log-history.md"
MAC_DIGESTS = config.STATE_DIR / "session-digests" / "mac"
AUTOMATION = Path.home() / "automation"

ASK_TIMEOUT = 600
# Summarising, not planning: Sonnet (owner decision, 2026-10-05). About 100k tokens a day
# on Opus; concern `ai-job-model-tier`.
MODEL = "sonnet"
MAC_STALE_DAYS = 3
HEADINGS = ("### Worked on", "### Tasks and build items", "### Open threads")
DAY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})\b.*$", re.M)
FINAL = "<!-- final -->"
MAX_FILES = 120

PAGE_HEAD = """\
---
type: reference
last_run: {last_run}
---
# Session log: last {n} days

What every Claude session worked on, on both machines, one section per day,
newest first. Written by `session_log` on vault-host at 04:30 and 13:00 PT from
session transcripts and the vault and automation git history. **Machine-written:
do not edit**, the next run overwrites today. Older days are in
[[session-log-history]]. How it works: [[claude-context-layer]].
"""

HISTORY_HEAD = """\
---
type: reference
last_run: {last_run}
---
# Session log history

Days that have left the 7-day window of [[session-log]], newest first, moved
here verbatim by `session_log`. Machine-written: do not edit.
"""

PROMPT = """\
You are writing one day's entry in the owner's session log: a record of what his
Claude sessions worked on, so that later sessions and his task system know what
happened. Its main use is keeping tasks current: which tasks and build items
were worked on, finished, changed, or are now probably stale.

The day is {day} ({weekday}). Below are: every Claude session active that day
(digests: his prompts, the files it edited, commits, actions like drafting
mail, and how it ended), the vault notes that changed that day by any means,
the code commits in his automation repo, and the names of his open tasks and
ideas.

Write exactly these three sections, in this order, and nothing else:

### Worked on
One bullet per thread of work (merge sessions that were the same thread). Lead
with **what**, in bold, then the outcome in one or two plain sentences: shipped,
decided, drafted, blocked, abandoned. Name the machine in brackets at the end:
(Mac), (Telegram), (Remote), or (Mac, Telegram). Most important first.

### Tasks and build items
One bullet per open task or idea that the day's work touched, as a wikilink
using the exact name from OPEN TASKS: `[[name]]`. Then what happened to it, and
if the work suggests the note is now out of date (done but still open, a new
next step, a changed date), say "possibly stale:" and why. Only names from OPEN
TASKS may be linked. If none were touched, write "- none".

### Open threads
Things left unfinished or waiting on the owner: a question he did not answer, a
plan approved but not built, a draft not sent. One bullet each. If none,
write "- none".

Rules: English, plain and terse, no em dashes. Do not invent anything that is
not in the material. No preamble, no day heading, no code fence.

=== SESSIONS ({n_sessions}) ===
{sessions}

=== VAULT NOTES CHANGED ===
{files}

=== AUTOMATION COMMITS ===
{commits}

=== OPEN TASKS ===
{tasks}
"""


# --- gathering -----------------------------------------------------------

def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, TZ)
    return start, start + timedelta(days=1)


def _sessions(since: datetime) -> list[dict]:
    """Box transcripts digested now, plus whatever the Mac last pushed."""
    out = session_digest.collect(since)
    if MAC_DIGESTS.is_dir():
        for f in MAC_DIGESTS.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if datetime.fromisoformat(d["end"]) >= since:
                out.append(d)
    return out


def _for_day(sessions: list[dict], day: date) -> list[dict]:
    """Each session's slice that fell on `day`. A session running past
    midnight shows on both days, each with its own prompts; the ending is told
    only on the day it ended."""
    lo, hi = _day_bounds(day)
    picked = []
    for s in sessions:
        prompts = [p for p in s["prompts"]
                   if p["at"] and lo <= datetime.fromisoformat(p["at"]) < hi]
        if not prompts:
            continue
        ended_today = lo <= datetime.fromisoformat(s["end"]) < hi
        picked.append({**s, "prompts": prompts, "final": s["final"] if ended_today else ""})
    return sorted(picked, key=lambda s: s["prompts"][0]["at"])


def _render_session(s: dict) -> str:
    when = datetime.fromisoformat(s["prompts"][0]["at"]).astimezone(TZ).strftime("%H:%M")
    lines = [f"--- {s['machine']} / {s['kind']} · {when} · "
             f"{s.get('title') or '(untitled)'} · cwd {s['cwd'] or '-'}"]
    for p in s["prompts"]:
        lines.append("ASKED: " + p["text"].replace("\n", " ")[:session_digest.PROMPT_CHARS])
    if s["files"]:
        lines.append("EDITED: " + ", ".join(s["files"]))
    if s["commits"]:
        lines.append("COMMITS: " + " | ".join(s["commits"]))
    if s["actions"]:
        lines.append("ACTIONS: " + ", ".join(s["actions"]))
    if s["final"]:
        lines.append("ENDED WITH: " + s["final"].replace("\n", " "))
    return "\n".join(lines)


def _git(repo: Path, day: date, fmt: str, *extra) -> str:
    lo, hi = _day_bounds(day)
    if not (repo / ".git").exists():
        return ""
    r = subprocess.run(["git", "-C", str(repo), "log", f"--since={lo.isoformat()}",
                        f"--until={hi.isoformat()}", f"--format={fmt}", *extra],
                       capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ""


def _vault_files(day: date) -> list[str]:
    seen = []
    for line in _git(config.VAULT, day, "", "--name-only").splitlines():
        line = line.strip()
        if line.endswith(".md") and line not in seen and "Course Archive/" not in line:
            seen.append(line)
    return seen


def _code_commits(day: date) -> list[str]:
    return [l for l in _git(AUTOMATION, day, "%h %s").splitlines() if l.strip()]


def _open_tasks() -> list[str]:
    names = []
    for p in sorted(config.INBOX.glob("*.md")):
        head = p.read_text(encoding="utf-8", errors="replace")[:600]
        m = re.search(r"^status:\s*(\S+)", head, re.M)
        if m and m.group(1) not in ("done", "cancelled"):
            names.append(p.stem)
    return names


def _prompt(day: date, sessions: list[dict], files: list[str], commits: list[str],
            tasks: list[str]) -> str:
    shown = files[:MAX_FILES]
    more = f"\n(+{len(files) - MAX_FILES} more)" if len(files) > MAX_FILES else ""
    return PROMPT.format(
        day=day.isoformat(), weekday=day.strftime("%A"), n_sessions=len(sessions),
        sessions="\n\n".join(_render_session(s) for s in sessions) or "(none)",
        files=("\n".join(shown) + more) or "(none)",
        commits="\n".join(commits) or "(none)",
        tasks="\n".join(tasks) or "(none)")


# --- guarding and placing ------------------------------------------------

def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return t.strip()


def _guard(section: str, known: set[str]) -> str:
    """Refuse a malformed answer (raises AskFailed); unlink names that resolve to
    nothing, rather than refuse, since one wrong link should not cost the day."""
    if not section.startswith(HEADINGS[0]):
        raise AskFailed(f"answer opens {section[:80]!r}, not '{HEADINGS[0]}'")
    pos = [section.find(h) for h in HEADINGS]
    if -1 in pos or pos != sorted(pos):
        raise AskFailed("the three section headings are missing or out of order")
    if DAY_RE.search(section):
        raise AskFailed("answer contains a day heading; it would split the page")

    def unlink(m):
        name = m.group(1).split("|")[0].strip()
        return m.group(0) if name in known else name
    return re.sub(r"\[\[([^\]]+)\]\]", unlink, section)


def _known_notes() -> set[str]:
    return {p.stem for p in config.VAULT.rglob("*.md") if ".obsidian" not in p.parts}


def _split(page: str) -> tuple[str, dict[str, str]]:
    """Page -> (head, {day: section body including its heading})."""
    marks = list(DAY_RE.finditer(page))
    if not marks:
        return page, {}
    head = page[:marks[0].start()]
    days = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        days[m.group(1)] = page[m.start():end].rstrip() + "\n"
    return head, days


def _section(day: date, body: str, now: datetime) -> str:
    final = now >= _day_bounds(day)[1]
    stamp = now.astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    return (f"## {day.isoformat()} ({day.strftime('%a')})\n"
            f"*Written {stamp} PT{', final' if final else ', still in progress'}.*"
            f"{' ' + FINAL if final else ''}\n\n{body.strip()}\n")


def place(page: str, history: str, new: dict[str, str], today: date,
          now: datetime) -> tuple[str, str]:
    """Merge new day sections into the page, roll old days into history.
    Pure, so the tests can hold it to idempotence."""
    last_run = now.strftime("%Y-%m-%dT%H:%M:%S%z")
    _, days = _split(page)
    days.update(new)
    cutoff = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
    keep = {d: s for d, s in days.items() if d >= cutoff}
    roll = {d: s for d, s in days.items() if d < cutoff}

    head = PAGE_HEAD.format(last_run=last_run, n=WINDOW_DAYS)
    page = head + "\n" + "\n".join(keep[d] for d in sorted(keep, reverse=True))

    if roll:
        _, old = _split(history)
        for d, s in roll.items():
            old.setdefault(d, s)
        history = HISTORY_HEAD.format(last_run=last_run) + "\n" + \
            "\n".join(old[d] for d in sorted(old, reverse=True))
    return page, history


def due_days(page: str, today: date, days: int) -> list[date]:
    """Days in the window that still need writing: any not on the page, and any
    written before the day was over."""
    _, have = _split(page)
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        s = have.get(d.isoformat())
        if s is None or FINAL not in s:
            out.append(d)
    return out


def build(days: list[date], sessions: list[dict], now: datetime) -> tuple[dict, list[str]]:
    """Ask once per day. Returns ({day: section}, [failures])."""
    tasks, known = _open_tasks(), _known_notes()
    out, failed = {}, []
    for day in days:
        todays = _for_day(sessions, day)
        files, commits = _vault_files(day), _code_commits(day)
        if not todays and not commits:
            body = "### Worked on\n- no Claude sessions\n\n### Tasks and build items\n" \
                   "- none\n\n### Open threads\n- none"
        else:
            try:
                body = _guard(_strip_fence(ask(_prompt(day, todays, files, commits, tasks),
                                               timeout=ASK_TIMEOUT, model=MODEL)), known)
            except AskFailed as exc:
                failed.append(f"{day}: {exc}")
                continue
        out[day.isoformat()] = _section(day, body, now)
        log(f"session_log: {day} written from {len(todays)} session(s), "
            f"{len(files)} note(s), {len(commits)} commit(s)")
    return out, failed


# --- entry points --------------------------------------------------------

def main() -> int:
    with Job("session_log", description="Session log", watches=("session-log",)) as job:
        now = datetime.now(TZ)
        today = now.date()
        page = PAGE.read_text(encoding="utf-8") if PAGE.exists() else ""
        history = HISTORY.read_text(encoding="utf-8") if HISTORY.exists() else ""
        days = due_days(page, today, WINDOW_DAYS if not page else 2)
        lo, _ = _day_bounds(min(days))
        sessions = _sessions(lo)
        job.record("days written", len(days))
        job.record("sessions read", len(sessions))

        newest_mac = max((datetime.fromisoformat(s["end"]) for s in sessions
                          if s["machine"] == "mac"), default=None)
        job.check("Mac sessions reaching the box", tier=ROUTINE, owner=YOU,
                  covers="session-log", key="session-log-mac",
                  ok=newest_mac is not None and now - newest_mac < timedelta(days=MAC_STALE_DAYS),
                  detail=f"no Mac session digest newer than {MAC_STALE_DAYS} days in "
                         f"{MAC_DIGESTS}. Fine if the Mac has been shut; otherwise the "
                         f"launchd agent com.example.session-digest is not pushing")

        new, failed = build(days, sessions, now)
        if new:
            page2, history2 = place(page, history, new, today, now)
            if job.dry_run:
                log("session_log: would write ------------------------------------")
                for line in page2.splitlines():
                    log(f"  {line}")
            else:
                PAGE.parent.mkdir(parents=True, exist_ok=True)
                PAGE.write_text(page2, encoding="utf-8")
                if history2 != history:
                    HISTORY.write_text(history2, encoding="utf-8")
        job.check("Session log written", ok=not failed, tier=IMPORTANT, owner=YOU,
                  covers="session-log",
                  detail="; ".join(failed) + ". Those days are untouched and retry next run."
                  if failed else f"{len(new)} day(s) written")
    return 0


def preview(argv) -> int:
    """Build N days into a scratch file without a Job: no state file, no vault
    commit, no alert. For judging the output before the job is installed."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--mac-digests", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    global MAC_DIGESTS
    if a.mac_digests:
        MAC_DIGESTS = a.mac_digests
    now = datetime.now(TZ)
    days = [now.date() - timedelta(days=i) for i in range(a.days - 1, -1, -1)]
    sessions = _sessions(_day_bounds(days[0])[0])
    new, failed = build(days, sessions, now)
    page, _ = place("", "", new, now.date(), now)
    a.out.write_text(page, encoding="utf-8")
    for f in failed:
        print("FAILED", f, file=sys.stderr)
    print(f"wrote {a.out}: {len(new)} day(s), {len(sessions)} session(s)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(preview(sys.argv[1:]) if "--preview" in sys.argv else main())
