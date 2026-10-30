"""Daily: has the LLM wiki fallen behind the sources it was compiled from?

The wiki in `Wiki/` is derived, never authored - the owner edits sources, an
ingest pass writes articles, and nothing edits an article by hand. That makes
staleness completely invisible from inside the wiki: an article compiled from
a note that has since changed reads exactly like one that is current.

`Wiki/_sources.md` is the only thing that knows. It holds a content hash per
source at ingest time, in two tables: sources that were distilled, and sources
that were judged and deliberately not distilled. This job compares those hashes
against reality and reports. It never re-ingests: rewriting existing articles
unattended is what scheduled-execution's three open gaps make unsafe, so the
detection half is automated and the writing half stays a session you start.

Findings land in `Wiki/_pending.md`, a generated page, never in Inbox. Inbox is
the task list; a daily job posting a note there would bury real work under
status updates. The checks are ROUTINE for the same reason: they are real, and
never worth an interruption.

    ssh vault-host '~/automation/bin/run-job wiki --dry-run'
"""

import hashlib
import re
from datetime import datetime, timezone

from runner import config
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU

WIKI = config.VAULT / "Wiki"
SOURCES = WIKI / "_sources.md"

# `| `path` | `abc123def456` | 2026-09-02 | articles |` in either table.
# The hash column is `MISSING` when a registered source no longer exists, so
# it is matched loosely and validated after, rather than being skipped by a
# stricter pattern that would silently drop the row.
ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*`?([0-9a-f]{12}|MISSING)`?\s*\|", re.M)

REJECTED_HEADING = "## Considered and not ingested"

# What counts as a new source worth ingesting, per _system/schema.md § From now
# on. Deliberately narrow: a wide rule would report the whole vault as pending
# every day, and a check that is always failing is a check nobody reads.
FRONT_TYPE = re.compile(r"^type:\s*(\S+)", re.M)
WIKI_SOURCE = re.compile(r"^\s*-\s*\"\[\[([^\]|#]+)", re.M)


def main() -> int:
    with Job("wiki", description="LLM wiki source drift",
             watches=("wiki-source-drift", "wiki-unregistered-sources")) as job:

        # A parse that quietly returns nothing would report "all sources
        # unchanged" - true, vacuously, and indistinguishable from a healthy
        # wiki. Same silent-success shape as an expired Canvas token, so the
        # parse asserts itself before anything depends on it.
        ingested, rejected = _registered()
        parsed_ok = job.check(
            "_sources.md parsed",
            ok=bool(ingested),
            tier=IMPORTANT, owner=YOU, covers="wiki-source-drift",
            detail=f"no rows parsed from {SOURCES} - the table format changed, or the file "
                   f"is missing. Every other check below would read as passing",
        )
        if not parsed_ok:
            return 0

        registered = {**ingested, **rejected}
        job.record("sources registered", len(ingested))
        job.record("sources judged and skipped", len(rejected))

        gone = [p for p in registered if not (config.VAULT / p).is_file()]
        job.check(
            "every registered source still exists",
            ok=not gone,
            tier=ROUTINE, owner=YOU, key="wiki-sources-gone", covers="wiki-source-drift",
            detail=_lines("Registered but no longer in the vault", gone) +
                   "\nA deleted source leaves its articles standing with nothing behind them.",
        )

        drifted = [(p, h) for p, h in ingested.items()
                   if (config.VAULT / p).is_file() and _hash(config.VAULT / p) != h]
        reconsider = [(p, h) for p, h in rejected.items()
                      if (config.VAULT / p).is_file() and _hash(config.VAULT / p) != h]
        job.record("sources drifted", len(drifted))

        job.check(
            "no ingested source has changed since it was distilled",
            ok=not drifted,
            tier=ROUTINE, owner=YOU, key="wiki-drift", covers="wiki-source-drift",
            detail=_drift_detail(drifted, ingested_articles=_articles(), reconsider=reconsider),
        )

        # An article citing a source no row knows about is a hole in the drift
        # check itself: that source can change forever without anything noticing.
        uncited = sorted({s for s in _cited() if s not in registered})
        job.check(
            "every source cited by an article is registered",
            ok=not uncited,
            tier=ROUTINE, owner=YOU, key="wiki-uncited", covers="wiki-source-drift",
            detail=_lines("Cited in an article's `wiki_sources` but absent from _sources.md",
                          uncited) +
                   "\nThese are invisible to the drift check until they are added.",
        )

        candidates = sorted(_candidates() - set(registered))
        job.record("unregistered candidates", len(candidates))
        job.check(
            "no unregistered ingest candidates",
            ok=not candidates,
            tier=ROUTINE, owner=YOU, key="wiki-candidates", covers="wiki-unregistered-sources",
            detail=_lines("New notes matching the ingest rule, never ingested or judged",
                          candidates) +
                   "\nEither distil them or add a row to _sources.md's second table saying why not.",
        )

        _publish(job, drifted=[p for p, _ in drifted], gone=gone,
                 reconsider=[p for p, _ in reconsider], uncited=uncited,
                 candidates=candidates, articles=_articles())

        log(f"wiki: {len(ingested)} sources, {len(drifted)} drifted, "
            f"{len(candidates)} candidates")
    return 0


# ---- the changelog -------------------------------------------------------
#
# Findings do NOT go to Inbox. Inbox is the task list, and a job that runs
# every morning would bury real work under status updates within a week. They
# go to one generated page instead, which carries both what is outstanding now
# and a dated history of what changed - so a week away is one page to read, not
# seven notes to triage.
#
# History is stored in the job's own memory rather than parsed back out of the
# page, so a hand-edit to the page cannot corrupt it, and only days where
# something actually CHANGED get an entry. A daily "nothing happened" line is
# how a log becomes unreadable.

PENDING = WIKI / "_pending.md"
HISTORY_KEEP = 60


def _publish(job, *, drifted, gone, reconsider, uncited, candidates, articles) -> None:
    outstanding = {"drifted": sorted(drifted), "gone": sorted(gone),
                   "reconsider": sorted(reconsider), "uncited": sorted(uncited),
                   "candidates": sorted(candidates)}
    memory = job.recall()
    history = memory.get("history", [])
    previous = history[0]["outstanding"] if history else {}

    if outstanding != previous:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        appeared = {k: [i for i in v if i not in previous.get(k, [])]
                    for k, v in outstanding.items()}
        resolved = {k: [i for i in previous.get(k, []) if i not in outstanding.get(k, [])]
                    for k in set(previous) | set(outstanding)}
        entry = {"date": today, "outstanding": outstanding,
                 "appeared": {k: v for k, v in appeared.items() if v},
                 "resolved": {k: v for k, v in resolved.items() if v}}
        # Same-day reruns replace rather than stack: three runs on one morning
        # are one day's news, not three.
        if history and history[0]["date"] == today:
            history[0] = entry
        else:
            history.insert(0, entry)
        job.remember({"history": history[:HISTORY_KEEP]})

    if job.dry_run:
        log(f"wiki: dry run, {PENDING.name} not written")
        return
    PENDING.write_text(_render(outstanding, history, articles), encoding="utf-8")


LABEL = {"drifted": "changed since it was distilled",
         "gone": "registered but no longer in the vault",
         "reconsider": "judged not-a-source, but has since grown",
         "uncited": "cited by an article, never registered",
         "candidates": "new, never ingested or judged"}


def _render(outstanding, history, articles) -> str:
    now = datetime.now(timezone.utc)
    total = sum(len(v) for v in outstanding.values())
    out = ["---", "type: reference", "component: llm-wiki",
           f"last_run: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
           f"healthy: {'true' if total == 0 else 'false'}", "---", "",
           "# Wiki — what is pending", "",
           f"**Generated {now.strftime('%Y-%m-%d %H:%M')} UTC by `jobs/wiki.py`. Do not edit; "
           "every run overwrites this file.** Nothing here is acted on automatically — "
           "re-ingest is a session you start. Rules: [[_system/schema|schema]].", ""]

    if total == 0:
        out += ["## Nothing outstanding", "",
                "Every ingested source matches the version it was distilled from, and every "
                "candidate has been either ingested or judged.", ""]
    else:
        out += [f"## Outstanding — {total}", ""]
        for kind, items in outstanding.items():
            if not items:
                continue
            out.append(f"**{LABEL[kind].capitalize()}**")
            out.append("")
            for i in items:
                feeds = ", ".join(a.strip() for a in articles.get(i, [])) if kind == "drifted" else ""
                out.append(f"- `{i}`" + (f"\n    feeds: {feeds}" if feeds else ""))
            out.append("")

    out += ["## What changed, by day", "",
            "Only days where something actually moved. A daily *nothing happened* line is how a "
            "log stops being read.", ""]
    if not history:
        out += ["*Nothing recorded yet.*", ""]
    for entry in history:
        bits = []
        for kind, items in entry.get("appeared", {}).items():
            bits += [f"**{i}** — {LABEL[kind]}" for i in items]
        for kind, items in entry.get("resolved", {}).items():
            bits += [f"~~{i}~~ — resolved" for i in items]
        out.append(f"### {entry['date']}")
        out += [f"- {b}" for b in bits] if bits else ["- *first run, baseline recorded*"]
        out.append("")
    return "\n".join(out) + "\n"


def _hash(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# `_sources.md` elides a long shared prefix as `…/Core Learnings/<file>.md`,
# purely so the table stays readable. Nothing expanded it, so `(VAULT / path)`
# resolved to a directory literally named `…` and every one of those nine rows
# read as a deleted source, while the same files -- cited by articles under
# their real paths -- read as unregistered. Two checks contradicting each other
# about the same four files is what gave it away. Failing since 2026-09-15,
# fixed 2026-09-16.
ELIDED = "\u2026/"


def _vault_index() -> list[str]:
    """Every note path in the vault, relative, cached for one run."""
    global _INDEX
    if _INDEX is None:
        _INDEX = [str(q.relative_to(config.VAULT))
                  for q in config.VAULT.rglob("*.md")]
    return _INDEX


_INDEX: list[str] | None = None


def _expand(path: str) -> str:
    """Resolve an elided registry path against the vault.

    Only a unique suffix match is accepted. Zero matches means the source
    really is gone and the caller should report it; several means the elision
    is ambiguous and guessing would be worse than saying so. Both cases keep
    the literal, so the existing checks surface them instead of this silently
    inventing a path.
    """
    if not path.startswith(ELIDED):
        return path
    tail = path[len(ELIDED) - 1:]          # keep the leading "/"
    hits = [q for q in _vault_index() if q.endswith(tail)]
    return hits[0] if len(hits) == 1 else path


def _registered() -> tuple[dict[str, str], dict[str, str]]:
    """Split _sources.md into ingested rows and deliberately-skipped rows."""
    if not SOURCES.is_file():
        return {}, {}
    text = SOURCES.read_text(errors="ignore")
    head, _, tail = text.partition(REJECTED_HEADING)
    return ({_expand(k): v for k, v in ROW.findall(head)},
            {_expand(k): v for k, v in ROW.findall(tail)})


def _articles() -> dict[str, list[str]]:
    """Source path -> the articles its row says it feeds, for the report."""
    if not SOURCES.is_file():
        return {}
    out = {}
    for line in SOURCES.read_text(errors="ignore").splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 6 and cells[1].startswith("`"):
            out[_expand(cells[1].strip("`"))] = [a for a in cells[4].split(",") if a.strip()]
    return out


def _cited() -> set[str]:
    """Every path named in an article's `wiki_sources` frontmatter."""
    cited = set()
    for path in WIKI.glob("*.md"):
        text = path.read_text(errors="ignore")
        front = text.split("---")[1] if text.startswith("---") else ""
        for target in WIKI_SOURCE.findall(front):
            cited.add(target.strip() + ".md")
    return cited


def _candidates() -> set[str]:
    """Notes the ingest rule says should be considered. Narrow on purpose."""
    found = set()
    for path in (config.VAULT / "Inbox").glob("*.md"):
        m = FRONT_TYPE.search(path.read_text(errors="ignore")[:400])
        if m and m.group(1).strip('"') == "learning":
            found.add(str(path.relative_to(config.VAULT)))
    clippings = config.VAULT / "Clippings"
    if clippings.is_dir():
        for path in clippings.rglob("*.md"):
            if not path.stem.startswith("_"):
                found.add(str(path.relative_to(config.VAULT)))
    return found


def _lines(heading: str, items: list[str], limit: int = 12) -> str:
    if not items:
        return ""
    shown = "\n".join(f"  - `{i}`" for i in items[:limit])
    more = f"\n  - ... and {len(items) - limit} more" if len(items) > limit else ""
    return f"{heading}:\n{shown}{more}"


def _drift_detail(drifted, ingested_articles, reconsider) -> str:
    if not drifted:
        return ""
    parts = ["These sources changed after the articles below were compiled from them.",
             "Re-ingest is diff-scoped: feed the changed lines plus these articles and ask",
             "whether it changes the claim. See _system/schema.md § Updates to existing sources.",
             ""]
    for path, _ in drifted:
        arts = ", ".join(a.strip() for a in ingested_articles.get(path, [])) or "unknown"
        parts.append(f"  - `{path}`\n      feeds: {arts}")
    if reconsider:
        parts += ["", "Also changed, previously judged not-a-source - worth reconsidering:"]
        parts += [f"  - `{p}`" for p, _ in reconsider]
    return "\n".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
