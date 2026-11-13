"""Weekly: which dates in the vault have passed, or are about to.

Documents, not tasks. Task due dates belong to the ToDo cockpit; sweeping
Inbox/ would mail 28 overdue items every Monday and turn a system-health
alert into a second, worse task list. maintenance.md draws the same line:
"Do not confuse this with a task list ... this file is for system state that
degrades."

Only NEWLY expired items are logged. Something stale for three weeks does not
need a fourth report; it stays visible under "Still broken" in the Monday
maintenance brief instead. New problems are logged, ongoing problems report.

    ssh vault-host '~/automation/bin/run-job tripwires --dry-run'
"""

import re
from datetime import date, timedelta

from runner import config
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU

WARN_WITHIN_DAYS = 14
# How long a component doc's `verified:` stays trustworthy. A quarter matches
# the cadence maintenance.md already uses for its own quarterly checks.
VERIFIED_STALE_DAYS = 90

SCAN_DIRS = ("_system", "Areas")

# horizon_end is North Star's own field. Reading it here is what let the last
# hardcoded date go: every commitment is now declared in the vault, not in code.
# Which concern in maintenance.md each dated file answers to. A finding in a
# file not listed here still reports; it just maps to no concern.
FILE_CONCERNS = {
    "Areas/North Star.md": "north-star-revisit",
    "_system/scheduling/working-conditions.md": "working-conditions-revisit",
    "Areas/Personal/Identity Profile.md": "identity-revisit",
    "Areas/Finances/Finances Profile.md": "finances-revisit",
    "Areas/Career/Career Profile.md": "career-decision",
}

FRONT_DATE = re.compile(
    r"^(revisit|verified|horizon_end):\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)


def main() -> int:
    with Job("tripwires", description="Dated tripwires",
             watches=tuple(FILE_CONCERNS.values()) + ("profile-verified-freshness",)) as job:
        today = date.today()
        horizon = today + timedelta(days=WARN_WITHIN_DAYS)

        scanned = 0
        findings = []
        for key, due, label, source, why in _all_dates(_count := []):
            if due > horizon:
                continue
            expired = due < today
            days = abs((due - today).days)
            findings.append({
                "key": key,
                "covers": FILE_CONCERNS.get(source, ""),
                "due": due,
                "label": f"{label} ({due}, "
                         f"{'expired ' + str(days) + 'd ago' if expired else 'in ' + str(days) + 'd'})",
                "detail": f"{source} - {why}",
                "expired": expired,
            })
        scanned = len(_count)

        # Tier decides the channel, and the runner does the rest.
        #
        # A `revisit:` date is a promise with a consequence, so it is
        # IMPORTANT: it is logged once, then goes quiet while it stays
        # outstanding. A stale `verified:` is entropy, and every doc here was
        # verified within the same three weeks, so a single staleness rule
        # would fire twenty documents at once. Those are ROUTINE: they
        # appear on the status page and in the brief, and never interrupt.
        commitments = [f for f in findings if not f["key"].startswith("verified|")]
        entropy = [f for f in findings if f["key"].startswith("verified|")]

        job.record("documents_scanned", scanned)
        job.record("commitments_due", len(commitments))
        job.record("docs_needing_reverification", len(entropy))
        if entropy:
            oldest = sorted(entropy, key=lambda x: x["due"])[:3]
            job.record("oldest_unverified", [f["label"] for f in oldest])

        for f in sorted(commitments, key=lambda x: x["due"]):
            log(f"tripwires: due      - {f['label']}")
            job.check(f["label"], ok=False, detail=f["detail"],
                      tier=IMPORTANT, owner=YOU, key=f["key"],
                      covers=f["covers"])

        for f in sorted(entropy, key=lambda x: x["due"]):
            log(f"tripwires: stale    - {f['label']}")
            job.check(f["label"], ok=False, detail=f["detail"],
                      tier=ROUTINE, owner=YOU, key=f["key"],
                      covers="profile-verified-freshness")

        if not findings:
            log(f"tripwires: {scanned} documents scanned, nothing due within "
                f"{WARN_WITHIN_DAYS} days")

    return 0


def _all_dates(counter: list):
    """Yield (key, effective_date, label, source, why).

    effective_date is what gets compared against today. For `revisit:` that is
    the date itself. For `verified:` it is the date plus the staleness window,
    since a doc verified last week is fine, not overdue.
    """
    for path in _scan_files():
        counter.append(path)
        rel = path.relative_to(config.VAULT)
        head = path.read_text(errors="ignore")[:1500]
        for field, iso in FRONT_DATE.findall(head):
            when = date.fromisoformat(iso)
            if field == "revisit":
                yield (f"revisit|{rel}|{iso}", when,
                       f"{rel.stem} due for review", str(rel),
                       "carries an explicit revisit date")
            elif field == "horizon_end":
                yield (f"horizon|{rel}|{iso}", when,
                       f"{rel.stem} horizon ends", str(rel),
                       "every prioritisation pass reads it; past the horizon it misleads")
            else:
                yield (f"verified|{rel}|{iso}", when + timedelta(days=VERIFIED_STALE_DAYS),
                       f"{rel.stem} unverified since {iso}", str(rel),
                       f"verified: is over {VERIFIED_STALE_DAYS} days old, so the doc "
                       f"is telling you to distrust it")


def _scan_files():
    for d in SCAN_DIRS:
        root = config.VAULT / d
        if root.is_dir():
            yield from sorted(root.rglob("*.md"))


if __name__ == "__main__":
    raise SystemExit(main())
