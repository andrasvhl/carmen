"""Daily: join what the vault says needs maintaining against what code checks.

Three documents, one job each:

  _system/maintenance.md         declares concerns and why they matter (by hand)
  ~/automation                   implements checks, each naming a concern slug
  _system/maintenance-status.md  this file's output: the join, plus the schedule

The join is the point. A concern with no check is unwatched and says so; a check
naming no concern was automated and never written down. Neither side can rot
quietly, because disagreement is itself reported.

Also stamps a short status block back into maintenance.md, between HTML markers.
Only the block is rewritten - never the surrounding prose - and if the markers
are absent it does nothing rather than guessing where they belong.

    ssh vault-host '~/automation/bin/run-job status --dry-run'
"""

import json
import re
import subprocess
from datetime import datetime, timezone

from runner import config
from runner.job import Job, log, CRITICAL, IMPORTANT, ROUTINE, YOU, AUTOMATIC

MAINT_DIR = config.VAULT / "_system" / "maintenance"
OUT = MAINT_DIR / "maintenance-status.md"
MAINTENANCE = MAINT_DIR / "maintenance.md"

BLOCK_START = "<!-- maintenance-status:start -->"
BLOCK_END = "<!-- maintenance-status:end -->"

# Concern slugs are HTML comments in maintenance.md's table rows:
# `| WhatsApp bridge store <!--[whatsapp-bridge-freshness]--> | ... |`
#
# A comment rather than backticked text because Obsidian hides comments in
# reading view. Visible slugs wrapped onto three lines in the first column and
# tripled the height of every row, for a token only this regex ever reads.
CONCERN = re.compile(r"^\|.*?<!--\[([a-z][a-z0-9-]{2,})\]-->", re.M)

TIERS = [
    (CRITICAL, "Critical", "Breaks the system, and you would not find out on your own. "
                           "Pushes every run until fixed."),
    (IMPORTANT, "Important", "Degrades silently with a real cost. Logged once, then stays "
                             "here and in the Monday brief while outstanding."),
    (ROUTINE, "Routine", "Rots slowly. Never interrupts; visible only here and in the "
                         "Monday brief."),
]


def main() -> int:
    with Job("status", description="Maintenance status page") as job:
        job.check("vault _system reachable", ok=OUT.parent.is_dir(),
                  tier=CRITICAL, owner=AUTOMATIC, covers="vault-reachable",
                  detail=f"{OUT.parent} missing - is obsidian-sync running?")

        concerns = _declared_concerns()
        job.check("maintenance.md declares concerns", ok=bool(concerns),
                  tier=IMPORTANT, owner=YOU,
                  detail=f"no `[slug]` markers in {MAINTENANCE.name} - the join cannot be "
                         f"computed, so coverage gaps would go unreported")

        states = _load_states()
        checks = [(s, c) for s in states for c in s.get("checks", [])]
        covered = {c["covers"] for _, c in checks if c.get("covers")}
        unwatched = sorted(concerns - covered)
        undeclared = sorted(covered - concerns)

        job.record("concerns_declared", len(concerns))
        job.record("concerns_watched", len(concerns & covered))
        job.record("concerns_unwatched", len(unwatched))
        job.record("checks_total", len(checks))
        job.record("checks_failing", len([1 for _, c in checks if not c["ok"]]))

        unconsidered = _unconsidered()
        job.record("unconsidered_things", len(unconsidered))
        job.check("nothing exists that maintenance.md has never considered",
                  ok=not unconsidered, tier=IMPORTANT, owner=YOU,
                  key="unconsidered", detail=f"{len(unconsidered)} thing(s) exist that the "
                  f"registry never mentions: "
                  f"{', '.join(f'{k} {n}' for k, n in unconsidered[:5])}. Add a row, or "
                  f"decide explicitly that it needs no maintenance")

        stale_checks = [(s_, c) for s_, c in checks if c["ok"] and _is_stale(s_)]
        stale_jobs = sorted({s_["job"] for s_, _ in stale_checks})
        job.record("checks_stale", len(stale_checks))
        job.check("no check is answering from a job that stopped running",
                  ok=not stale_checks, tier=IMPORTANT, owner=YOU, key="stale-checks", covers="stale-green-checks",
                  detail=f"{len(stale_checks)} check(s) still render their last result but "
                         f"their job has not run since: {', '.join(stale_jobs[:5])}. "
                         f"A stale-green row answers \"is this still true?\" with a yes it "
                         f"cannot support")

        job.check("every check maps to a declared concern", ok=not undeclared,
                  tier=IMPORTANT, owner=YOU, key="undeclared-concerns",
                  detail=f"{len(undeclared)} check(s) name a concern absent from "
                         f"maintenance.md: {', '.join(undeclared[:5])}")

        page = _render(states, checks, concerns, covered, unwatched, undeclared,
                       unconsidered)

        if job.dry_run:
            log(f"status: dry run, would write {OUT.name} and stamp {MAINTENANCE.name}")
            log(f"status: {len(concerns & covered)}/{len(concerns)} concerns watched, "
                f"{len(unwatched)} unwatched, {len(undeclared)} undeclared")
        else:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            OUT.write_text(page, encoding="utf-8")
            ok = _stamp_maintenance(states, concerns, covered, unwatched, undeclared)
            log(f"status: wrote {OUT.name} ({len(checks)} checks, {len(concerns)} concerns); "
                f"maintenance.md {'stamped' if ok else 'NOT stamped (markers missing)'}")
    return 0


def _unconsidered() -> list[tuple[str, str]]:
    """Things that exist and that maintenance.md has never mentioned at all.

    The other two directions compare the registry against the code. This one
    compares the registry against REALITY - services running, credentials on
    disk, component docs written - and asks what nobody has thought about.

    That is the gap the other two cannot see: a concern nobody declared has no
    slug, so it cannot show up as unwatched. It simply does not exist as far as
    the system is concerned, which is exactly how the schedule table came to
    list one timer while six were running.

    Matching is loose on purpose. This produces a prompt for a human, not an
    alert: over-reporting costs a glance, under-reporting costs an outage.
    """
    if not MAINTENANCE.exists():
        return []
    text = MAINTENANCE.read_text(errors="ignore").lower()
    # A job that reports into this system is covered by its own concerns; the
    # timer that fires it is not a separate thing to maintain.
    text += " ".join(f"automation@{s.stem}" for s in config.STATE_DIR.glob("*.json")).lower()
    found = []

    def consider(kind: str, name: str, needle: str | None = None):
        """Report only if NO meaningful part of the name appears.

        Exact-string matching produced six phantom gaps: the registry says
        "Apple Shortcut revision" while the doc is `apple-shortcuts-capture`,
        and "Frontmatter property drift" while the doc is
        `frontmatter-properties`. Parts shorter than six characters are
        ignored, so common words like "daily" cannot match everything.
        """
        needle = (needle or name).lower()
        if needle in text:
            return
        parts = [w for w in re.split(r"[-_./]", needle) if len(w) >= 6]
        if any(w in text for w in parts):
            return
        found.append((kind, name))

    # Services and timers we own
    for line in _run(["systemctl", "list-units", "--type=service", "--no-pager",
                      "--no-legend", "--plain"]).splitlines():
        unit = next((p for p in line.split() if p.endswith(".service")), None)
        if unit and any(k in unit for k in ("obsidian", "claude", "automation", "vault")):
            consider("service", unit, unit.replace(".service", ""))
    for line in _run(["systemctl", "list-timers", "--all", "--no-pager",
                      "--no-legend", "--plain"]).splitlines():
        unit = next((p for p in line.split() if p.endswith(".timer")), None)
        if unit and ("automation@" in unit or "claude" in unit):
            consider("timer", unit, unit[:-6].replace("automation@", ""))

    # Cron entries
    for entry in _run(["crontab", "-l"]).splitlines():
        entry = entry.strip()
        if entry and not entry.startswith("#"):
            for tok in entry.split()[5:]:
                if tok.endswith((".py", ".sh")):
                    consider("cron", tok.split("/")[-1], tok.split("/")[-1].rsplit(".", 1)[0])
                    break

    # Credential-shaped files. Every silent auth break so far came from one.
    seen = set()
    for pat in ("*.env", ".*env", ".*/credentials/*.json", ".*/config.json",
                ".*/client_secret.json", ".claude/.credentials.json"):
        for f in config.HOME.glob(pat):
            if f.is_file() and f.name not in seen:
                seen.add(f.name)
                # Judge a credential by the store it lives in. Naming every
                # per-account token file in the registry would be a list that
                # rots each time an account is added.
                store = f.parent.name if f.parent.name != "credentials" \
                    else f.parent.parent.name
                consider("credential", str(f.relative_to(config.HOME)), store)

    # Component docs describing something live
    for f in (config.VAULT / "_system").rglob("*.md"):
        if f.stem.startswith("_") or f.stem in ("SYSTEM", "README"):
            continue
        head = f.read_text(errors="ignore")[:400]
        if "stage: built" in head:
            consider("component doc", f.stem)

    return sorted(set(found))


def _declared_concerns() -> set[str]:
    if not MAINTENANCE.exists():
        return set()
    return set(CONCERN.findall(MAINTENANCE.read_text(errors="ignore")))


def _load_states() -> list[dict]:
    now = datetime.now(timezone.utc)
    out = []
    for path in sorted(config.STATE_DIR.glob("*.json")):
        if path.name.endswith(".memory.json"):
            continue
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # STATE_DIR is not exclusively job state. `vt canvas` keeps its archive
        # state in the same directory (canvas.json: courses/readings/saved/
        # version, no `finished`), and this crashed with KeyError the moment it
        # appeared - status would have died on its next timed run, 2026-09-01.
        # Anything without the job-state shape is another tool's file, not a
        # job that failed to record itself, so skip rather than crash.
        if not isinstance(data, dict) or "finished" not in data:
            continue
        try:
            data["_age_h"] = (now - datetime.fromisoformat(data["finished"])).total_seconds() / 3600
        except (TypeError, ValueError):
            continue
        out.append(data)
    return out


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception:
        return ""


def _schedule_rows():
    """Read live. Replaces the hand-maintained table in cron-jobs.md, which
    listed one timer while six were running."""
    rows = []
    listing = _run(["systemctl", "list-timers", "--all", "--no-pager", "--no-legend", "--plain"])
    units = sorted({p for line in listing.splitlines() for p in line.split()
                    if p.endswith(".timer") and ("automation@" in p or "claude-auth-watch" in p)})
    for unit in units:
        name = unit[:-6].replace("automation@", "")
        ran = "never"
        for f in (config.STATE_DIR / f"{name}.json", config.HOME / f".{name}.state"):
            if f.exists():
                ran = datetime.fromtimestamp(f.stat().st_mtime).astimezone().strftime("%Y-%m-%d %H:%M")
                break
        nxt = _run(["systemctl", "show", unit, "-p", "NextElapseUSecRealtime", "--value"]).split()
        nxt = " ".join(nxt[1:3]) if len(nxt) >= 3 else "unknown"
        active = _run(["systemctl", "is-active", unit])
        flag = "" if active == "active" else f" **{active.upper()}**"
        rows.append(f"| `{unit[:-6]}` | {_schedule_of(unit)} | {ran} | {nxt}{flag} |")
    for entry in _run(["crontab", "-l"]).splitlines():
        entry = entry.strip()
        if entry and not entry.startswith("#"):
            rows.append(f"| `{_cron_name(entry)}` | {' '.join(entry.split()[:5])} (cron) "
                        f"| unknown | unknown |")
    return rows


def _schedule_of(unit):
    m = re.search(r"OnCalendar=([^;}]+)",
                  _run(["systemctl", "show", unit, "-p", "TimersCalendar", "--value"]))
    raw = m.group(1).strip() if m else "?"
    # "*-*-* 07:30:00" is daily; "Mon *-*-* 07:45:00" is weekly and must keep
    # its weekday, or it reads as "Mon daily" which is neither.
    return raw.replace("*-*-* ", "daily ") if raw.startswith("*-*-*") else raw


def _cron_name(entry):
    tokens = entry.split()
    for token in tokens[5:]:
        if token.endswith((".py", ".sh")):
            return "cron: " + token.split("/")[-1]
    return "cron: " + (tokens[5].split("/")[-1] if len(tokens) > 5 else entry[:24])


# How long a job's report stays evidence of anything. A check said "ok" at the
# moment its job ran; if the job has not run since, that sentence is in the past
# tense and the page must not print it in the present.
#
# This exists because `selftest` has no timer. Five rows -- "credentials file
# present", "mail configured", "state directory writable", "heartbeat
# configured", "vault Inbox reachable" -- rendered **healthy** for thirteen days
# off a single run on 2026-09-02, and would have rendered healthy forever. A
# status page that reports stale-green is worse than one that reports nothing:
# it actively answers "is this still true?" with a yes it cannot support.
#
# Graced at 2x cadence so an ordinary late run is not called stale.
_CADENCE_H = {"snapshot": 0.25}   # anything not listed is inferred from its timer
_DAILY_H, _WEEKLY_H = 24.0, 168.0


def _cadence_h(job_name):
    """Expected hours between runs, or None if nothing will ever refresh it."""
    if job_name in _CADENCE_H:
        return _CADENCE_H[job_name]
    unit = f"automation@{job_name}.timer"
    if _run(["systemctl", "is-enabled", unit]) != "enabled":
        # No timer: nothing is coming. A manual tool's report ages out and
        # never returns, which is exactly the selftest case.
        return None
    raw = _schedule_of(unit)
    return _WEEKLY_H if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)", raw) else _DAILY_H


def _is_stale(state):
    """True when this report is too old to speak for the present."""
    cadence = _cadence_h(state.get("job", ""))
    age = state.get("_age_h", 0.0)
    if cadence is None:
        # Ungraced: a one-off run stops being current once its own day is out.
        return age > _DAILY_H
    return age > cadence * 2


def _ago(hours):
    if hours < 1:
        return f"{hours * 60:.0f}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.1f}d ago"


def _render(states, checks, concerns, covered, unwatched, undeclared, unconsidered):
    now = datetime.now().astimezone()
    failing = [c for _, c in checks if not c["ok"]]
    # healthy tracks what would actually interrupt you. Routine entropy and
    # already-reported findings do not make the system unhealthy.
    alerting = [c for c in failing if c.get("tier") != ROUTINE and not c.get("ongoing")]
    # Staleness counts against health for the same reason failure does: in both
    # cases the page cannot claim the thing is true. Without this, silencing a
    # check by stopping its job would read as fixing it.
    alerting += [c for st, c in checks
                 if c["ok"] and c.get("tier") != ROUTINE and _is_stale(st)]

    L = ["---", "type: reference", "component: maintenance-status",
         f"last_run: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
         f"healthy: {'true' if not alerting else 'false'}",
         "---", "",
         "# Maintenance status — what is actually watched", "",
         f"**Generated {now.strftime('%Y-%m-%d %H:%M %Z')} by `~/automation`. Do not edit;",
         "every run overwrites this file.** Why each concern matters lives in [[maintenance]];",
         "how the machinery works lives in [[automation]]. This page is the join of the two.", "",
         f"**{len(concerns & covered)} of {len(concerns)} declared concerns watched by "
         f"{len(checks)} checks. {len(failing)} failing.**", ""]

    # Ordered by what the reader has to do, not by how the data is shaped.
    #
    # Failures first, whoever owns them: a critical service being down is the
    # most urgent thing on the page even though nobody has to "decide"
    # anything. Then the healthy items split by owner, because once nothing is
    # broken the only useful question is which of it is your problem. Sorting
    # healthy items by severity sorts on a dimension that stopped mattering.
    TIER_RANK = {CRITICAL: 0, IMPORTANT: 1, ROUTINE: 2}

    def rows(pred):
        return sorted([(s, c) for s, c in checks if pred(s, c)],
                      key=lambda r: (TIER_RANK.get(r[1].get("tier"), 9), r[1]["name"]))

    def table(rs, show_state=True):
        head = ("| What | Severity | Concern | Job | Last checked |"
                + (" State |" if show_state else " Fixed by |"))
        out = [head, "|---|---|---|---|---|---|"]
        for s, c in rs:
            slug = f"`{c['covers']}`" if c.get("covers") else "—"
            last = ("**failing**" if not c.get("ongoing") else "**failing** (reported)") \
                if show_state else {YOU: "you", AUTOMATIC: "automatic"}.get(
                    c.get("owner"), c.get("owner", "?"))
            out.append(f"| {c['name']} | {c.get('tier', '?')} | {slug} | `{s['job']}` "
                       f"| {_ago(s['_age_h'])} | {last} |")
        return out

    broken = rows(lambda s, c: not c["ok"])
    L += [f"## Needs attention now — {len(broken)}", ""]
    if broken:
        L += ["*Everything currently failing, most severe first, whoever owns it. "
              "`routine` entries here are real but never interrupt you.*", ""] + table(broken)
    else:
        L.append("*Nothing is failing.*")

    # A passing check whose job has stopped running is not a passing check. It
    # gets its own section rather than a footnote, because the reader's question
    # ("is this still true?") has a third answer besides yes and no: nobody has
    # asked recently.
    stale = rows(lambda s, c: c["ok"] and _is_stale(s))
    if stale:
        L += ["", f"## Last answer is out of date — {len(stale)}", "",
              "*These passed when their job last ran, and their job has not run since. "
              "Treat them as unknown, not as healthy — fix the job, not the check.*", ""]
        out = ["| What | Severity | Concern | Job | Last checked | State |",
               "|---|---|---|---|---|---|"]
        for s, c in stale:
            slug = f"`{c['covers']}`" if c.get("covers") else "—"
            cadence = _cadence_h(s.get("job", ""))
            why = "no timer — nothing will refresh it" if cadence is None else "job overdue"
            out.append(f"| {c['name']} | {c.get('tier', '?')} | {slug} | `{s['job']}` "
                       f"| {_ago(s['_age_h'])} | **stale** ({why}) |")
        L += out

    healthy_you = rows(lambda s, c: c["ok"] and not _is_stale(s) and c.get("owner") == YOU)
    L += ["", f"## Waiting on you — {len(healthy_you)} healthy", "",
          "*Passing today, but only a person can act when they stop passing: a decision, a "
          "payment, an interactive login, a judgement that needs re-checking.*", ""]
    L += table(healthy_you, show_state=False) if healthy_you else ["*(none)*"]

    healthy_auto = rows(lambda s, c: c["ok"] and not _is_stale(s) and c.get("owner") != YOU)
    L += ["", f"## Runs without you — {len(healthy_auto)} healthy", "",
          "*Passing, and the machine handles them. You only need to know they are still true.*",
          ""]
    L += table(healthy_auto, show_state=False) if healthy_auto else ["*(none)*"]
    L.append("")

    L += ["## Declared but watched by nothing", ""]
    if unwatched:
        L += [f"*{len(unwatched)} concern(s) in [[maintenance]] that no check covers. Derived, "
              "not hand-kept: a concern added there appears here until code covers it.*", "",
              "| Concern |", "|---|"] + [f"| `{s}` |" for s in unwatched]
    else:
        L.append("*Every declared concern is covered by at least one check.*")

    L += ["", "## Checked but never declared", ""]
    if undeclared:
        L += ["*A check names a concern [[maintenance]] does not list — something was automated "
              "and never written down.*", "", "| Slug |", "|---|"] + [f"| `{s}` |" for s in undeclared]
    else:
        L.append("*Every check maps to a declared concern.*")

    L += ["", "## Exists, but never considered", ""]
    if unconsidered:
        L += ["*Real things on the box or in the vault that [[maintenance]] does not mention at "
              "all. Not a failure — a prompt. Either add a row, or decide out loud that it needs "
              "no maintenance.*", "", "| Kind | What |", "|---|---|"]
        L += [f"| {k} | `{n}` |" for k, n in unconsidered]
    else:
        L.append("*Everything running or stored has at least been considered.*")

    L += ["", "## What runs, and when", "",
          "*Read live from `systemctl` and `crontab`. Never hand-maintained: the file that used "
          "to hold this listed one timer while six were running.*", "",
          "| Job | Schedule | Last ran | Next |", "|---|---|---|---|"]
    L += _schedule_rows() or ["| *(none found)* | | | |"]

    L += ["", "## Measurements from the last run", "", "| Job | Ran | Reading |", "|---|---|---|"]
    for s in sorted(states, key=lambda x: x["job"]):
        for k, v in (s.get("records") or {}).items():
            if not k.startswith("_"):
                L.append(f"| `{s['job']}` | {_ago(s['_age_h'])} | {k.replace('_', ' ')}: {v} |")

    L += ["", "---", "",
          "Job detail: `ssh vault-host 'journalctl -u automation@<job> -n 50 --no-pager'`", ""]
    return "\n".join(L)


def _stamp_maintenance(states, concerns, covered, unwatched, undeclared):
    if not MAINTENANCE.exists():
        return False
    text = MAINTENANCE.read_text(encoding="utf-8")
    if BLOCK_START not in text or BLOCK_END not in text:
        return False

    now = datetime.now().astimezone()
    checks = [c for s in states for c in s.get("checks", [])]
    failing = [c for c in checks if not c["ok"]]

    # The registry is grouped by domain, because that is what makes the "why"
    # column readable - credentials together, profiles together. Owner and
    # severity live in code, so regrouping these rows by owner would either
    # duplicate them here or scatter each domain across two sections. This
    # block carries the owner split instead: same answer, no second copy.
    failing = [c for c in checks if not c["ok"]]
    yours = [c for c in failing if c.get("owner") == YOU]
    theirs = [c for c in failing if c.get("owner") != YOU]
    crit = [c for c in failing if c.get("tier") == CRITICAL]

    def names(cs, n=4):
        shown = ", ".join(f"`{c['covers'] or c['name']}`" for c in cs[:n])
        return shown + (f" and {len(cs) - n} more" if len(cs) > n else "")

    lines = [BLOCK_START,
             f"> [!info] **Live status, {now.strftime('%Y-%m-%d %H:%M %Z')}** — generated, do not edit."]

    if crit:
        lines.append(f"> ### 🔴 {len(crit)} critical, act now")
        lines.append(f"> {names(crit)}")

    lines.append("> ### Needs you")
    lines.append(f"> {names(yours) if yours else '*Nothing. Everything needing a person is passing.*'}")

    lines.append("> ### Failing but handled automatically")
    lines.append(f"> {names(theirs) if theirs else '*Nothing.*'}")

    lines.append("> ### Coverage")
    lines.append(f"> **{len(concerns & covered)} of {len(concerns)} concerns watched** by "
                 f"{len(checks)} checks.")
    if unwatched:
        lines.append(f"> Watched by nothing: {', '.join(f'`{s}`' for s in unwatched[:5])}"
                     + (f" and {len(unwatched) - 5} more" if len(unwatched) > 5 else "") + ".")
    if undeclared:
        lines.append(f"> **Automated but not declared here:** "
                     f"{', '.join(f'`{s}`' for s in undeclared[:5])} — add a row.")
    lines += ["> Full detail, grouped by what you have to do: [[maintenance-status]].", BLOCK_END]

    MAINTENANCE.write_text(
        re.sub(re.escape(BLOCK_START) + r".*?" + re.escape(BLOCK_END),
               "\n".join(lines), text, flags=re.S), encoding="utf-8")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
