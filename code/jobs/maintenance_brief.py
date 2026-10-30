"""Monday 14:05 PT: fix what is safe, then one brief of what needs the owner, then one push.

Replaces the maintenance mails, which the owner does not read (2026-10-03). The
information has to arrive as decisions, not reports. Plan and decisions:
_system/proposals/maintenance-brief/plan.md; component doc:
_system/maintenance-brief.md.

## Three steps, in this order

1. **Repairs, in Python, before any model runs** (runner/repairs.py). A fixed
   menu: restart a known service that is down, re-run a crashed job, retry a
   check that failed on a transient error, clear a stale git lock. One attempt
   per item per week, every action logged. The model fixes nothing.
2. **One read-only Claude turn** (runner.ask: Read, Glob, Grep), the build_brief
   pattern. It writes only the two sections that need judgement: "Needs you"
   (at most 5, ranked by consequence) and "Expiring within 30 days". Python
   writes the rest, and "Still broken" in particular is listed mechanically
   from the alert log and every job's state, so a model that misses something
   cannot make it disappear.
3. **Telegram** (since 2026-10-04): a header, then one message per item with
   [Let Claude fix it] [Create task] / [Skip]. The item's Who only labels it. If Telegram fails, one Pushover summary linking to the note; mail only if that
   fails too.

The note, `_system/maintenance/briefs/<YYYY-MM-DD>.md`, is a record, never a
task (decision 3: no automatic tasks). It asks per item whether to create one;
the owner answers with the item's button on Telegram, or in a Claude
session ("create tasks 1 and 3"). A machine-readable block at the end lists the
items with ids; the bridge reads a tapped item back from it.

Also here: the weekly credentials drift check (runner/credentials_drift.py),
and section 5, the one line that proves a quiet week was quiet. That was the
retired `digest` job's whole purpose.

    ssh vault-host '~/automation/bin/run-job maintenance_brief --dry-run'
"""

import json
import math
import pathlib
import platform
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from runner import alert_log, config, credentials_drift, notify, repairs
from runner.ask import AskFailed, ask
from runner.job import CRITICAL, IMPORTANT, ROUTINE, YOU, AUTOMATIC, Job, log

OBSIDIAN_VAULT = "<VAULT_NAME>"
TZ = ZoneInfo("America/Los_Angeles")
COVERS = "maintenance-brief"
DRIFT = "credentials-drift"
JOB = "maintenance_brief"

ASK_TIMEOUT = 900
# build_brief starts at 14:00 (+2 min jitter) and its model turn runs ~5 min. Two
# claude -p processes at once on a small box is the risk the plan names, so the
# brief waits for it, up to this long, before its own turn.
WAIT_FOR_BUILD_BRIEF_S = 900
MAX_NEEDS_YOU = 5
STALE_AFTER_DAYS = 8
EXPIRY_WINDOW_DAYS = 30
HISTORY_DAYS = 180
MAX_LOG_LINES_IN_PROMPT = 150

TIER_RANK = {CRITICAL: 0, "crash": 1, IMPORTANT: 2, ROUTINE: 3}
NEEDS_FIELDS = ("What is wrong", "Why it matters", "Next action", "Effort")
OPTIONAL_FIELDS = ("Who", "Reopens")
YOU, CLAUDE = "you", "claude"
# Changing credentials or systemd is labelled "needs you", whatever the model
# says. Since 2026-10-04 the label is all it decides: every item gets the fix
# button, and the fix session's ask list gates sudo, systemctl stop and the
# like on the phone (the owner: "as far as it can"). ACTIONS, not
# mentions: the first version matched any mention and overruled "diff it
# against /etc/systemd/..." (a read) on the very first dry run. Reading,
# diffing and committing a copy into ~/automation stay Claude's.
_SECRET = r"(?:password|passphrase|token|api[ _-]?key|secret|credential)s?"
ALWAYS_YOU = re.compile("|".join((
    r"\bsudo\b",
    r"\bsystemctl\s+(?:--\S+\s+)*(?:start|stop|restart|reload|enable|disable|"
    r"daemon-reload|edit|mask|unmask|kill|reset-failed)\b",
    r"\b(?:cp|mv|tee|install|ln|rm|to|into|under|in)\s+`?/etc/systemd",
    r"\b(?:edit|change|write|append|add|set|remove|delete|replace|update)\b[^\n]{0,60}"
    r"[\w~./-]*\.env\b",
    r"\b(?:rotate|regenerate|revoke|reissue|renew)\b",
    r"\b(?:change|replace|reset|set)\b[^\n]{0,30}\b" + _SECRET + r"\b",
)), re.I)
ISO = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")

PROMPT = """\
You are writing the "Needs you" part of the owner's weekly maintenance brief for the
automation that runs his Obsidian vault on vault-host. He does not read
maintenance mail; this brief is the only place these findings reach him, so it
must turn findings into decisions.

Python has already done the rest of the brief: it tried the safe automatic
repairs (listed below with outcomes), and it lists every open finding
mechanically under "Still broken". You decide what actually needs HIM.

You can Read, Glob and Grep the vault (your working directory). Useful:
`_system/maintenance/maintenance.md` (why each concern matters, by slug),
`_system/maintenance/maintenance-status.md` (today's generated status),
`_system/credentials.md`, and the component docs in `_system/`. Code lives in
`~/automation`, which you cannot read.

Write exactly two sections, in this order, and nothing before the first:

## Needs you
At most {max_items} items, ranked by consequence: what breaks, for him, if this
is not handled this week. Only things that need a person: a decision, a login,
a payment, a judgement, or something the automatic repairs could not fix. Not
routine slow rot unless it has escalated. Merge findings that are one problem.
Each item exactly in this shape:

### 1. <short title>
- What is wrong: <one or two sentences, concrete>
- Why it matters: <the consequence, one sentence>
- Next action: <one physical step, e.g. a command or a file to edit>
- Effort: <5 min / 15 min / 30 min / 1 h / 2 h / half a day>
- Who: <you | Claude>
- Reopens: <task path from FIXES CLAUDE CLOSED, only if this is that fix still failing>

Who decides the button he gets. `you` when the item needs: judgement about his
life (goals, priorities, routines, how he wants something done); facts only he
knows; anything leaving the vault and the box (messages, mail, money); anything
hard to undo (deleting material, rewriting his own words); or credentials or
systemd in any way. `Claude` when a Claude session with a shell on vault-host
can finish it alone: committing files in ~/automation, fixing links or
frontmatter, filling in documentation, tidying. Split an item that is part
each. A `Claude` item's Next action is the instruction that session will get.

If nothing needs him, write the single line `Nothing needs you this week.`

## Expiring within 30 days
One line per item: `- <what> (<date or days left>): <consequence in a few words>`.
Tokens, logins, `revisit:` dates and anything in maintenance.md section 1 with a
date inside the window. Use only the candidates below or dates you read in the
vault. Write `None.` if there are none.

Rules: English. Plain and direct: no em dashes, no filler, no praise. Use only
facts given here or read from the vault; never invent a path, a command or a
date. No preamble, no frontmatter, no code fence around the answer.

Today is {today}.

--- REPAIRS TRIED THIS RUN ---
{repairs}

--- STILL BROKEN (mechanical, every open finding) ---
{broken}

--- CREDENTIALS DRIFT (box vs _system/credentials.md, names only) ---
{drift}

--- THIS WEEK'S ALERT LOG ({n_log} blocks, oldest first) ---
{log_lines}

--- EVERY JOB'S LAST RUN ---
{jobs}

--- EXPIRY CANDIDATES (mechanical) ---
{expiring}

--- maintenance.md SECTION 1, "Only you can do these" ---
{section1}

--- FIXES CLAUDE CLOSED SINCE THE LAST BRIEF ---
Each was closed as done by a Claude session. If its finding is still in STILL
BROKEN, it did not hold: make it an item with Who: you and Reopens: <its task>.
{fixes}
"""


def main() -> int:
    with Job(JOB, description="Monday maintenance brief", watches=(COVERS, DRIFT)) as job:
        now = datetime.now(TZ)
        today = now.date()
        note_path = brief_path(today)
        if note_path.exists():
            # A rerun on the same Monday must never produce a second brief.
            job.check("maintenance brief: already written today", ok=True,
                      tier=ROUTINE, owner=YOU, covers=COVERS)
            return 0

        # 1. Repairs, before anything reads state, so the brief describes the
        #    world after them.
        iso = today.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        attempted = {k: v for k, v in (job.recall().get("repairs") or {}).items() if v == week}
        rp = repairs.Repairs(attempted=attempted, week=week, dry_run=job.dry_run)
        if _on_box():
            actions = rp.run(load_states())
        else:
            # No systemd here: every service would read as down.
            log(f"{JOB}: not on vault-host, no repairs attempted")
            actions = []
        job.remember({"repairs": rp.attempted})
        job.record("repairs attempted", len([a for a in actions if a.outcome in
                                             (repairs.FIXED, repairs.NOT_FIXED)]))
        job.record("repairs fixed", len([a for a in actions if a.outcome == repairs.FIXED]))
        _log_repairs(job, actions)

        # 2. Credentials drift, box only.
        drift = _drift(job)

        # 3. Everything the brief is built from.
        states = load_states()
        utc = now.astimezone(timezone.utc)
        week_log = alert_log.read(utc - timedelta(days=7), utc)
        history = alert_log.read(utc - timedelta(days=HISTORY_DAYS), utc)
        broken = still_broken(states, history, drift or [], utc)
        fixed, tried = fixed_this_week(week_log, actions)
        ran = everything_else(states, week_log, utc)
        candidates = expiry_candidates(states, today)
        job.record("still broken", len(broken))
        job.record("log blocks this week", len(week_log))

        # 4. One read-only turn, after build_brief's.
        _wait_for_build_brief()
        model_error = ""
        try:
            fixes = _fixes_closed(today)
            answer = _no_em_dash(_strip_fence(ask(
                _prompt(today, actions, broken, drift, week_log, states, candidates, fixes),
                timeout=ASK_TIMEOUT)))
            needs, expiring = parse_answer(answer)
            apply_reopens(needs, fixes, dry_run=job.dry_run)
        except AskFailed as exc:
            model_error = str(exc)[:300]
            needs = []
            expiring = "\n".join(f"- {c}" for c in candidates) or "None."
            log(f"{JOB}: model pass failed, writing the mechanical brief: {model_error}")

        note = render(today, needs, expiring, fixed, tried, broken, ran, model_error, now)
        n_claude = sum(1 for it in needs if it.get("kind") == CLAUDE)
        summary = (f"{len(needs) - n_claude} need you, "
                   + (f"{n_claude} Claude can fix, " if n_claude else "") + f"{len(fixed)} fixed")
        if job.dry_run:
            log(f"{JOB}: would write {note_path.relative_to(config.VAULT)} ---------------")
            for line in note.splitlines():
                log(f"  {line}")
            log(f"{JOB}: ------------------------------------------------------------")
            log(f"{JOB}: would send 'Maintenance: {summary}' to Telegram, "
                f"{len(needs)} item(s) with buttons")
        else:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(note, encoding="utf-8")
            _push(job, today, summary, broken, candidates, model_error, needs)

        job.check("maintenance brief written", ok=not model_error, tier=IMPORTANT, owner=YOU,
                  covers=COVERS,
                  detail=f"the model pass failed ({model_error}); the brief was written without "
                         f"a ranked Needs you section. Every open finding is under Still broken")
    return 0


# --- paths ----------------------------------------------------------------------

def briefs_dir():
    return config.VAULT / "_system" / "maintenance" / "briefs"


def brief_path(day: date):
    return briefs_dir() / f"{day.isoformat()}.md"


def _obsidian_url(vault_path: str) -> str:
    return ("obsidian://open?vault=" + quote(OBSIDIAN_VAULT)
            + "&file=" + quote(vault_path, safe=""))


# --- state files ----------------------------------------------------------------

def _systemctl(*args) -> str:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, text=True,
                              timeout=25).stdout.strip()
    except Exception:
        return ""


def _has_timer(name: str) -> bool:
    return _systemctl("is-enabled", f"automation@{name}.timer") == "enabled"


def load_states() -> list[dict]:
    """Every job report in STATE_DIR. Ported from the retired digest, with its
    lesson: not every .json there is a report (canvas.json is the archiver's
    cache), and the rollup must never be the job that crashes on one."""
    now = datetime.now(timezone.utc)
    states = []
    for path in sorted(config.STATE_DIR.glob("*.json")):
        if path.name.endswith(".memory.json"):
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            states.append({"job": path.stem, "status": "unreadable", "_age_days": 0.0,
                           "_error": str(exc), "checks": [], "_scheduled": False})
            continue
        if not isinstance(data, dict) or not isinstance(data.get("finished"), str) \
                or not data.get("job"):
            continue
        try:
            finished = datetime.fromisoformat(data["finished"])
        except ValueError as exc:
            states.append({"job": data.get("job", path.stem), "status": "unreadable",
                           "_age_days": 0.0, "_error": f"bad finished timestamp: {exc}",
                           "checks": [], "_scheduled": False})
            continue
        data["_age_days"] = (now - finished).total_seconds() / 86400
        data["_scheduled"] = _has_timer(data["job"])
        states.append(data)
    return states


# --- step 1 and 2 helpers ---------------------------------------------------------

def _log_repairs(job: Job, actions) -> None:
    done = [a for a in actions if a.outcome in (repairs.FIXED, repairs.NOT_FIXED)]
    for a in actions:
        log(f"{JOB}: repair {a.kind} {a.target}: {a.outcome} ({a.detail})")
    if job.dry_run or not done:
        return
    try:
        alert_log.append([alert_log.Entry(
            job=JOB, check=f"{a.kind} {a.target}", key=f"repair|{a.key}",
            tier=ROUTINE if a.outcome == repairs.FIXED else IMPORTANT, owner=AUTOMATIC,
            event=alert_log.REPAIR, detail=f"{a.why}. {a.outcome}: {a.detail}") for a in done])
    except alert_log.AlertLogUnavailable as exc:
        # The brief still lists them; this only costs next week's history.
        job.check("repair log written", ok=False, tier=IMPORTANT, owner=AUTOMATIC,
                  covers=COVERS, detail=str(exc))


def _on_box() -> bool:
    return platform.system() == "Linux" and (config.HOME / "vault").is_dir()


def _drift(job: Job):
    if not _on_box():
        log(f"{JOB}: not on vault-host, credentials drift not checked")
        return None
    found = credentials_drift.check()
    job.record("credentials drift", len(found))
    for key, sentence in found:
        job.check(f"credentials drift: {sentence}", ok=False, tier=IMPORTANT, owner=YOU,
                  key=f"cred|{key}", covers=DRIFT,
                  detail="fix the box or _system/credentials.md, whichever is wrong")
    if not found:
        job.check("vault-host secrets match _system/credentials.md", ok=True,
                  tier=IMPORTANT, owner=YOU, covers=DRIFT)
    return found


def _wait_for_build_brief() -> None:
    """Never touch build_brief's lock (taking it, even briefly, could make it
    skip); ask systemd whether its run is still in progress."""
    deadline = time.monotonic() + WAIT_FOR_BUILD_BRIEF_S
    while _systemctl("is-active", "automation@build_brief.service") in ("activating", "active"):
        if time.monotonic() >= deadline:
            log(f"{JOB}: build_brief still running after {WAIT_FOR_BUILD_BRIEF_S}s, going ahead")
            return
        time.sleep(15)


# --- the mechanical sections ------------------------------------------------------

def _since(history, job: str, key: str):
    """When this finding's current failing run began, from the log: the first
    failing block after its most recent resolved block."""
    start = None
    for e in history:
        if e.job != job or e.key != key:
            continue
        if e.event == alert_log.RESOLVED:
            start = None
        elif e.event in (alert_log.FAILING, alert_log.CRASHED) and start is None:
            start = e.time
    return start


def still_broken(states, history, drift, now: datetime) -> list[dict]:
    """Every open finding, from state, dated from the log. Mechanical by design:
    this is the section that must be complete when the model is wrong."""
    items = []

    def add(job, check, key, tier, owner, detail, streak=0):
        since = _since(history, job, key)
        if since:
            weeks = max(1, math.ceil((now - since).total_seconds() / (7 * 86400)))
            age = f"broken {weeks} week{'s' if weeks != 1 else ''}, since {since.astimezone(TZ).date()}"
        else:
            age = (f"failing {streak} run{'s' if streak != 1 else ''}, from before the log"
                   if streak else "not in the log yet")
        items.append({"job": job, "check": check, "key": key, "tier": tier, "owner": owner,
                      "detail": detail, "age": age, "since": since})

    for s in states:
        job = s.get("job", "?")
        if job == JOB:
            continue    # last week's brief; this run's findings are added below
        if s.get("status") == "unreadable":
            add(job, "state file unreadable", f"unreadable|{job}", IMPORTANT, AUTOMATIC,
                s.get("_error", ""))
            continue
        if s.get("status") == "error":
            add(job, "job crashed on its last run", f"crash|{job}", "crash", AUTOMATIC,
                s.get("note", ""))
        if s.get("_scheduled") and s.get("_age_days", 0) > STALE_AFTER_DAYS:
            add(job, "job stopped running", f"stale|{job}", IMPORTANT, AUTOMATIC,
                f"last run {s['_age_days']:.0f} days ago; its timer is enabled")
        for c in s.get("checks", []):
            if not c.get("ok"):
                add(job, c.get("name", "?"), c.get("key") or c.get("name", "?"),
                    c.get("tier", IMPORTANT), c.get("owner", YOU), c.get("detail", ""),
                    c.get("streak", 0))
    for key, sentence in drift:
        add(JOB, f"credentials drift: {sentence}", f"cred|{key}", IMPORTANT, YOU, "")
    return sorted(items, key=lambda i: (TIER_RANK.get(i["tier"], 9),
                                        i["since"] or now, i["job"], i["check"]))


def fixed_this_week(week_log, actions) -> tuple[list[str], list[str]]:
    """(what broke and got fixed, what was tried and did not work)."""
    fixed, tried = [], []
    for a in actions:
        line = f"**{a.kind} `{a.target}`**: {a.why}. {a.detail}."
        if a.outcome == repairs.FIXED:
            fixed.append(line + " Fixed by this run's automatic repair.")
        elif a.outcome == repairs.NOT_FIXED:
            tried.append(line + " Not fixed.")
        elif a.outcome in (repairs.SKIPPED, repairs.WOULD):
            tried.append(f"**{a.kind} `{a.target}`**: {a.why}. Not attempted: {a.detail}.")
    # week_log ends when this run started, so this run's own repairs (logged
    # since) are not counted twice; an earlier run's this week are. A repair
    # block is ROUTINE exactly when it fixed something (_log_repairs).
    for e in week_log:
        if e.event == alert_log.REPAIR and e.tier == ROUTINE:
            fixed.append(f"**{e.check}** ({e.time.astimezone(TZ).date()}): {e.detail}")
        elif e.event == alert_log.RESOLVED:
            fixed.append(f"**{e.check}** (`{e.job}`) passes again since "
                         f"{e.time.astimezone(TZ).strftime('%a %Y-%m-%d')}.")
    return list(dict.fromkeys(fixed)), tried


def everything_else(states, week_log, now: datetime) -> str:
    """Section 5: one line that proves a quiet week was quiet, rather than the
    alerting being dead. The retired digest's purpose, in one sentence."""
    jobs = [s for s in states if s.get("job") != JOB]
    scheduled = [s for s in jobs if s.get("_scheduled")]
    ran = [s for s in scheduled if s.get("_age_days", 99) <= 7]
    clean = [s for s in ran if s.get("status") == "ok"]
    findings = [s for s in ran if s.get("status") == "problem"]
    crashed = [s for s in jobs if s.get("status") == "error"]
    stale = [s for s in scheduled if s.get("_age_days", 0) > STALE_AFTER_DAYS]
    unreadable = [s for s in jobs if s.get("status") == "unreadable"]
    parts = [f"{len(clean)} clean", f"{len(findings)} with findings (above)"]
    if crashed:
        parts.append(f"{len(crashed)} crashed ({', '.join(s['job'] for s in crashed)})")
    if stale:
        parts.append(f"{len(stale)} stopped running ({', '.join(s['job'] for s in stale)})")
    if unreadable:
        parts.append(f"{len(unreadable)} unreadable")
    quiet = not (crashed or stale or unreadable)
    return (f"{'All ' if quiet and len(ran) == len(scheduled) else ''}{len(ran)} of "
            f"{len(scheduled)} scheduled jobs reported within the last 7 days: "
            + ", ".join(parts)
            + f". The alert log took {len(week_log)} block{'s' if len(week_log) != 1 else ''} "
              f"this week, so the alerting itself is alive.")


def expiry_candidates(states, today: date) -> list[str]:
    """Dated things inside the window, found mechanically. The model phrases
    section 4 from these; if the model fails, these are section 4."""
    horizon = today + timedelta(days=EXPIRY_WINDOW_DAYS)
    out = []
    hb = next((s for s in states if s.get("job") == "heartbeat"), {})
    days = (hb.get("records") or {}).get("claude_login_days_left")
    if isinstance(days, (int, float)) and days <= EXPIRY_WINDOW_DAYS:
        out.append(f"Claude Code login on vault-host: {days:.0f} days left "
                   f"(renew with `ssh -t vault-host renew-claude`)")
    try:
        from jobs import tripwires
        for key, due, label, source, _why in tripwires._all_dates([]):
            if key.startswith("verified|") or due > horizon:
                continue
            when = (f"{(today - due).days} days overdue" if due < today
                    else f"in {(due - today).days} days")
            out.append(f"{label} ({due}, {when}): `{source}`")
    except Exception as exc:
        out.append(f"(the revisit-date scan failed: {type(exc).__name__}: {exc})")
    for what, when in _section1_rows():
        for iso in ISO.findall(when):
            try:
                due = date.fromisoformat(iso)
            except ValueError:
                continue
            # Future dates only: a past date in this column is a fact ("live
            # since"), and past revisit dates come from the tripwires scan.
            if today <= due <= horizon:
                out.append(f"{_plain(what)}: {when} (maintenance.md section 1)")
    return list(dict.fromkeys(out))


def _maintenance_text() -> str:
    try:
        return (config.VAULT / "_system" / "maintenance" / "maintenance.md").read_text(
            encoding="utf-8")
    except OSError:
        return ""


def _section1() -> str:
    m = re.search(r"^### 1\. .*?$(.*?)(?=^### 2\. |\Z)", _maintenance_text(), re.M | re.S)
    return m.group(1).strip() if m else "(maintenance.md section 1 not found)"


def _section1_rows():
    for line in _section1().splitlines():
        if line.startswith("|") and not re.match(r"^\|\s*(-|What\b)", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 2:
                yield cells[0], cells[1]


def _plain(cell: str) -> str:
    cell = re.sub(r"<!--.*?-->", "", cell)
    cell = re.sub(r"\[\[[^\]|]*\\?\|([^\]]+)\]\]", r"\1", cell)
    return re.sub(r"\[\[([^\]]+)\]\]", r"\1", cell).strip()


# --- the model ----------------------------------------------------------------------

def _fixes_closed(today) -> list[dict]:
    from jobs.tg_bridge import proactive
    return proactive.FixLog().closed_since((today - timedelta(days=7)).isoformat())


def _prompt(today, actions, broken, drift, week_log, states, candidates, fixes=()) -> str:
    rep = "\n".join(f"- {a.kind} {a.target}: {a.why}. Outcome: {a.outcome}, {a.detail}"
                    for a in actions) or "(nothing needed repair)"
    br = "\n".join(f"- [{i['tier']}] {i['job']}: {i['check']} (owner {i['owner']}; {i['age']})"
                   + (f": {alert_log.one_line(i['detail'], 240)}" if i["detail"] else "")
                   for i in broken) or "(none)"
    dr = ("(not checked: not running on vault-host)" if drift is None else
          "\n".join(f"- {s}" for _, s in drift) or "(none: the box matches the doc)")
    shown = week_log[-MAX_LOG_LINES_IN_PROMPT:]
    lines = "\n".join(f"- {e.time.astimezone(TZ).strftime('%a %m-%d %H:%M')} {e.job} "
                      f"[{e.event}, {e.tier}] {e.check}" + (f": {e.detail[:200]}" if e.detail else "")
                      for e in shown) or "(empty)"
    if len(week_log) > len(shown):
        lines = f"({len(week_log) - len(shown)} older blocks omitted)\n" + lines
    jobs = "\n".join(
        f"- {s.get('job')}: {s.get('status')}, {s.get('_age_days', 0):.1f} days ago, "
        f"{s.get('checks_failed', 0)}/{s.get('checks_total', 0)} checks failing"
        + ("" if s.get("_scheduled") else ", no timer") for s in states) or "(none)"
    return PROMPT.format(max_items=MAX_NEEDS_YOU, today=today.isoformat(), repairs=rep,
                         broken=br, drift=dr, n_log=len(week_log), log_lines=lines, jobs=jobs,
                         expiring="\n".join(f"- {c}" for c in candidates) or "(none)",
                         section1=_section1(),
                         fixes="\n".join(f"- {f.get('title')}: {f.get('summary')} (task: "
                                         f"{f.get('task')})" for f in fixes) or "(none)")


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return t.strip()


def _no_em_dash(text: str) -> str:
    """The owner's rule. The prompt says so; build_brief learned it is not enough."""
    return text.replace(" — ", ": ").replace("—", ", ")


def kind_of(item: dict) -> str:
    """The model's Who, overruled to `you` by the credentials-and-systemd rule.
    Missing or unclear is `you`: offering a fix button by mistake costs more."""
    if (item.get("Who") or "").strip().lower().strip("*`. ") != CLAUDE:
        return YOU
    text = " ".join(item.get(f, "") for f in ("title",) + NEEDS_FIELDS)
    hit = ALWAYS_YOU.search(text)
    if hit:
        # Visible in a dry run: the model said Claude, the rule said no.
        log(f"{JOB}: {item.get('title', '')[:60]!r}: Who: Claude overruled to you "
            f"(matched {hit.group(0)!r})")
        return YOU
    return CLAUDE


def apply_reopens(needs: list[dict], fixes: list[dict], *, dry_run: bool) -> None:
    """Reopen a task Claude closed whose finding the model says is still failing.
    Only tasks in this week's closed list, matched by path or file name."""
    from jobs.tg_bridge import proactive
    known = {pathlib.Path(f["task"]).name: f for f in fixes if f.get("task")}
    for it in needs:
        ref = (it.get("Reopens") or "").strip().strip("`")
        fix = known.get(pathlib.Path(ref).name) if ref else None
        if not fix:
            it.pop("Reopens", None)
            continue
        it["kind"] = YOU
        it["reopened"] = pathlib.Path(fix["task"]).stem
        if dry_run:
            log(f"{JOB}: would reopen {fix['task']}")
            continue
        try:
            proactive.reopen_task(pathlib.Path(fix["task"]),
                                  f"The finding is still failing: {it['What is wrong']}")
            log(f"{JOB}: reopened {fix['task']}")
        except OSError as exc:
            log(f"{JOB}: could not reopen {fix['task']} ({exc})")


def parse_answer(text: str) -> tuple[list[dict], str]:
    """(needs-you items, expiring section). Raises AskFailed on a wrong shape.

    Only structure is checkable. More than five items is trimmed rather than
    refused, since losing the whole ranking costs more than losing item six.
    """
    # Preamble ("Checked the vault... Writing the brief now.") despite the prompt
    # forbidding it was the first live run's answer (2026-10-03). The heading is
    # what matters; text before it is dropped, never rendered.
    start = text.find("## Needs you")
    if start < 0:
        raise AskFailed(f"no '## Needs you' heading; answer opens {text[:80]!r}")
    text = text[start:]
    cut = text.find("## Expiring within 30 days")
    if cut < 0:
        raise AskFailed("missing '## Expiring within 30 days'")
    needs_md, expiring = text[len("## Needs you"):cut], text[cut + len("## Expiring within 30 days"):]
    expiring = expiring.strip()
    if not expiring:
        raise AskFailed("empty Expiring section")

    chunks = re.split(r"^###\s+\d+\.\s+", needs_md, flags=re.M)
    head, chunks = chunks[0], chunks[1:]
    if not chunks:
        if "nothing needs you" not in head.lower():
            raise AskFailed("Needs you has no items and does not say 'Nothing needs you'")
        return [], expiring
    items = []
    for chunk in chunks:
        lines = chunk.strip().splitlines()
        item = {"title": lines[0].strip().strip("*")}
        for line in lines[1:]:
            m = re.match(r"^\s*[-*]\s+\**(%s)\**:\**\s*(.+)$"
                         % "|".join(NEEDS_FIELDS + OPTIONAL_FIELDS), line)
            if m:
                item[m.group(1)] = m.group(2).strip()
        missing = [f for f in NEEDS_FIELDS if not item.get(f)]
        if missing:
            raise AskFailed(f"item {item['title'][:40]!r} lacks {', '.join(missing)}")
        item["kind"] = kind_of(item)
        items.append(item)
    if len(items) > MAX_NEEDS_YOU:
        log(f"{JOB}: model gave {len(items)} items, keeping the first {MAX_NEEDS_YOU}")
        items = items[:MAX_NEEDS_YOU]
    return items, expiring


# --- the note ------------------------------------------------------------------------

def render(today, needs, expiring, fixed, tried, broken, ran, model_error, now) -> str:
    """Frontmatter, sections 2, 3 and 5 and the items block are Python's. The
    model's two sections are re-rendered from their parsed fields, so their
    shape cannot drift either."""
    stamp = now.strftime("%Y-%m-%d %H:%M %Z")
    L = ["---", "type: reference", f"component: {COVERS}",
         f"last_run: {now.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", "---", "",
         f"# Maintenance brief, {today.isoformat()}", "",
         f"**{len(needs)} need you, {len(fixed)} fixed automatically, {len(broken)} still "
         f"broken.** Written by `maintenance_brief` on vault-host, {stamp}. Nothing here becomes "
         "a task on its own: to make one, tap Create task on the item's Telegram message, or "
         "reply in a Claude session, e.g. \"create tasks 1 and 3\".", ""]

    L += ["## 1. Needs you", ""]
    if model_error:
        L += [f"*The model pass failed ({model_error}), so nothing was ranked this week. Every "
              "open finding is listed under Still broken.*", ""]
    elif not needs:
        L += ["*Nothing needs you this week.*", ""]
    for n, item in enumerate(needs, 1):
        L += [f"### {n}. {item['title']}",
              f"- **What is wrong:** {item['What is wrong']}",
              f"- **Why it matters:** {item['Why it matters']}",
              f"- **Next action:** {item['Next action']}",
              f"- **Effort:** {item['Effort']}",
              f"- **Who:** {'Claude can do it' if item.get('kind') == CLAUDE else 'Needs you'}"]
        if item.get("reopened"):
            L += [f"- **Reopened:** Claude closed [[Inbox/{item['reopened']}|{item['reopened']}]] "
                  "as done, but the finding is still failing. The task is open again.", ""]
        else:
            L += ["- **Create a task?** On Telegram: Let Claude fix it (it creates the task, works "
                  "on it as far as it can in its own topic, and closes it if it needed nothing "
                  f"from you), or Create task. Or reply \"create task {n}\" in a Claude session.", ""]

    L += ["## 2. Fixed automatically this week", ""]
    L += [f"- {f}" for f in fixed] or ["*Nothing broke and got fixed this week.*"]
    if tried:
        L += ["", "**Tried, not fixed or not attempted:**", ""] + [f"- {t}" for t in tried]
    L.append("")

    L += ["## 3. Still broken", "",
          f"*{len(broken)} open finding{'s' if len(broken) != 1 else ''}, listed mechanically "
          "from the alert log and every job's current state, not by the model. Most severe "
          "first.*", ""]
    for i in broken:
        detail = f" {alert_log.one_line(i['detail'], 220)}" if i["detail"] else ""
        L.append(f"- **{i['check']}** · `{i['job']}` · {i['tier']}, owner {i['owner']}. "
                 f"{i['age'].capitalize()}.{detail}")
    if not broken:
        L.append("*Nothing is failing.*")
    L.append("")

    L += ["## 4. Expiring within 30 days", "", expiring.strip(), ""]

    L += ["## 5. Everything else ran", "", ran, ""]

    block = {"brief": today.isoformat(), "items": [
        {"id": f"mb-{today.isoformat()}-{n}", "rank": n, "title": it["title"],
         "what": it["What is wrong"], "why": it["Why it matters"],
         "next_action": it["Next action"], "effort": it["Effort"],
         "kind": it.get("kind", YOU), "reopened": it.get("reopened")}
        for n, it in enumerate(needs, 1)]}
    L += ["## Items, machine-readable", "",
          "*For the Telegram bridge's buttons. Generated; do not edit.*", "",
          "```maintenance-brief-items", json.dumps(block, indent=2, ensure_ascii=False), "```", ""]
    # Job details carry em dashes (the runner's own escalation suffix among
    # them); the note is the owner's to read, so his rule applies to all of it.
    return _no_em_dash("\n".join(L))


# --- delivery -------------------------------------------------------------------------

def _push(job: Job, today, summary, broken, candidates, model_error, needs=()) -> None:
    """Telegram first, since the brief can wait hours (push-notifications decision
    "Split by urgency"); Pushover, then mail, only when Telegram is down."""
    rel = f"_system/maintenance/briefs/{today.isoformat()}"
    message = (f"{len(broken)} still broken, {len(candidates)} dated item(s) due within "
               f"{EXPIRY_WINDOW_DAYS} days or overdue."
               + (" The model pass failed, so nothing is ranked." if model_error else ""))
    try:
        _telegram(today, summary, message, rel, needs)
        log(f"{JOB}: sent to Telegram, {len(needs)} item(s) with buttons")
        return
    except Exception as exc:
        # Never silent: the push below is the old path, unchanged.
        log(f"{JOB}: Telegram failed ({type(exc).__name__}: {exc}), pushing instead")
    try:
        to = notify.push_or_mail(
            f"vault-host: Maintenance: {summary}",
            f"This week's maintenance brief is in the vault: {rel}.md\n\n{message}\n",
            push_message=message, url=_obsidian_url(rel), url_title="Open brief")
        log(f"{JOB}: {'pushed' if to == 'push' else f'push failed, mailed {to}'}")
    except Exception as exc:
        job.check("maintenance brief delivered", ok=False, tier=IMPORTANT, owner=YOU,
                  covers=COVERS, detail=f"neither push nor mail went out: {exc}")


def _telegram(today, summary, message, rel, needs) -> None:
    """A header, then one message per item with [Let Claude fix it] [Create task] / [Skip].

    The bridge handles the taps (jobs/tg_bridge/proactive.py) and reads the item
    back from this brief's machine-readable block, so the ranks here must be
    the block's ranks: both count from 1 in `needs` order. Raises on the first
    failure, so a half-sent brief still falls back to the push.
    """
    from jobs.tg_bridge import proactive
    # One buzz per brief: the header follows quiet hours, the items arrive
    # silently under it (2026-10-04, six separate notifications was noise).
    notify.send_telegram(_plain(f"🛠 Maintenance: {summary}\n{message}\n\nThe brief is in the "
                                f"vault: {rel}.md"
                                + ("\n\nCreate a task for any of these?" if needs else "")))
    for n, it in enumerate(needs, 1):
        text = (f"{n}. {it['title']}\n{it['What is wrong']}\n\nNext: {it['Next action']}"
                + (f" ({it['Effort']})" if it.get("Effort") else ""))
        if it.get("reopened"):
            notify.send_telegram(_plain(text + f"\n\nReopened your task: {it['reopened']}"),
                                 silent=True)
            continue
        notify.send_telegram(_plain(f"{text}\n\n{proactive.kind_label(it.get('kind', YOU))}"),
                             silent=True, buttons=proactive.brief_buttons(today.isoformat(), n))


def _plain(text: str) -> str:
    """Telegram gets plain text from jobs (no parse_mode), where markdown backticks
    arrive as literal characters around every path. The note keeps them."""
    return _no_em_dash(text).replace("`", "")


if __name__ == "__main__":
    raise SystemExit(main())
