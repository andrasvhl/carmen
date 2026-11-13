"""Weekly: has the vault grown out from under the documents that describe it?

Five kinds of drift, all silent by nature:

  1. Frontmatter properties nobody declared, and near-misses that look like
     typos of reserved ones. A note with `revist:` instead of `revisit:` drops
     out of the maintenance sweep and looks identical to one never enrolled.
  2. Component docs in _system/ with no row in SYSTEM.md, and rows pointing at
     files that no longer exist.
  3. The Vault Map's folder list against a real listing.
  4. Wikilinks to notes that do not exist.
  5. Component docs whose code changed after their `verified:` date, and
     systemd timers against the repos and the docs: not installed, running but
     undocumented, or still scheduled past a job's EXPIRES date (Nov 8).

    ssh vault-host '~/automation/bin/run-job inventory --dry-run'
"""

import re
from difflib import get_close_matches

from runner import config
from tools import check_docs
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU

REGISTRY = config.VAULT / "_system" / "frontmatter-properties.md"
SYSTEM = config.VAULT / "_system" / "SYSTEM.md"
VAULT_MAP = config.VAULT / "_system" / "README.md"

# "Course Archive" is machine-written by `vt canvas`: thousands of generated
# notes whose properties are already declared in the registry. Scanning them
# weekly for undeclared properties and dead wikilinks costs real time and can
# only ever report noise. Added 2026-09-01, same reasoning as Notion/.
SKIP_DIRS = {".obsidian", "Archive", "Notion", "Clippings", "Attachments",
             "Course Archive"}

FRONT_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):", re.M)
# Stop at a backslash too: the vault escapes pipes as [[Path\|Alias]], and
# without this the target captures the trailing backslash and every aliased
# link reads as a dead one.
WIKILINK = re.compile(r"\[\[([^\]|#^\\]+)")


def main() -> int:
    with Job("inventory", description="Vault inventory drift",
             watches=("doc-standard", "doc-history-placement", "syncable-filenames",
                      "property-registry-drift", "system-components-drift",
                      "vault-map-drift", "dead-wikilinks", "doc-code-drift",
                      "timer-drift", "job-expiry")) as job:
        _check_doc_standard(job)
        _check_history_placement(job)
        _check_syncable_filenames(job)
        _check_properties(job)
        _check_component_rows(job)
        _check_vault_map(job)
        _check_wikilinks(job)
        _check_docs_behind_code(job)
        _check_timers(job)
        log(f"inventory: {len([c for c in job.checks if c.ok])}/{len(job.checks)} checks passed")
    return 0


# ---- 0. the documentation standard, the mechanical half -------------------

# The standard itself lives in tools/check_docs.py, shared with `vt docs` and
# the Claude Code edit hook, so the three can never disagree. This job is the
# backstop for edits the hook cannot see (Bash rewrites, Obsidian on the phone).


def _check_doc_standard(job: Job) -> None:
    docs = check_docs.all_component_docs(config.VAULT)
    job.record("component_docs_checked", len(docs))
    bad = []
    for path in docs:
        problems = check_docs.check_text(path.read_text(errors="ignore"))
        if problems:
            bad.append(f"{path.stem} ({problems[0][:90]})")
    job.check("component docs follow the template", ok=not bad,
              tier=ROUTINE, owner=YOU, key="doc-standard", covers="doc-standard",
              detail=f"{len(bad)} doc(s) break _component-doc-template: {'; '.join(bad[:5])}. "
                     f"Run `vt docs` for the full list")


# ---- 0a. history sitting in the reading path ------------------------------

# SYSTEM.md's documentation standard, rule 3: present tense above the fold,
# superseded material under ## History. It gets broken because history
# disguises itself as justification at the moment you are editing - a "Why
# it's here" cell citing a past incident is legitimate rationale, so writing
# "this superseded three rows on 2026-08-21" into one feels identical. It is
# not: one says why the row exists, the other says what used to be there.
#
# These markers are narrow on purpose. They match statements ABOUT THE
# DOCUMENT'S OWN PAST, never a rationale that happens to mention a date.
def _check_history_placement(job: Job) -> None:
    offenders = []
    for path in sorted((config.VAULT / "_system").rglob("*.md")):
        if check_docs.is_generated(path):
            continue    # a weekly brief may say "resolved 2026-..." and be right to
        found = check_docs.history_markers_above_fold(path.read_text(errors="ignore"))
        if found:
            offenders.append(f"{path.stem} ({', '.join(found)})")

    job.record("docs_with_history_above_the_fold", len(offenders))
    job.check("history stays below the fold", ok=not offenders,
              tier=ROUTINE, owner=YOU, key="history-placement",
              covers="doc-history-placement",
              detail=f"{len(offenders)} doc(s) carry resolved/superseded notes above their "
                     f"## History section: {'; '.join(offenders[:4])}. Move them down - "
                     f"SYSTEM.md documentation standard, rule 3")


# ---- 0b. files that cannot reach the box ----------------------------------

# The headless Obsidian Sync client refuses these and logs
# "Ignoring remote file name with illegal characters" exactly once, then stays
# silent forever. Two notes sat on the Mac only, one for ten days, while
# obsidian-sync reported healthy the entire time. Anything running on
# vault-host - every job here, the MCP server, Remote Control - simply cannot
# see such a file.
ILLEGAL_IN_FILENAME = set(':*?"<>|')


def _check_syncable_filenames(job: Job) -> None:
    bad = [p.relative_to(config.VAULT) for p in config.VAULT.rglob("*.md")
           if not any(x in SKIP_DIRS for x in p.parts)
           and ILLEGAL_IN_FILENAME & set(p.name)]
    job.record("unsyncable_filenames", len(bad))
    job.check("every note has a syncable filename", ok=not bad,
              tier=IMPORTANT, owner=YOU, key="unsyncable-filenames",
              covers="syncable-filenames",
              detail=f"{len(bad)} note(s) contain characters Obsidian Sync refuses "
                     f"({''.join(sorted(ILLEGAL_IN_FILENAME))}), so they exist on one "
                     f"machine only: {', '.join(str(b) for b in bad[:3])}. Rename them; "
                     f"the sync client reports this once and then never again")


# ---- 1. property drift ----------------------------------------------------

def _check_properties(job: Job) -> None:
    """The registry doc is the allowlist. Anything in the vault and not in it
    is either a new property nobody documented, or a typo."""
    dormant = _registry_block("dormant-properties")
    known = _known_properties() | set(dormant)
    if not job.check("property allowlist readable", ok=bool(known),
                     tier=IMPORTANT, owner=YOU, covers="property-registry-drift",
                     detail=f"no ```known-properties block in {REGISTRY.name} - the "
                            f"registry cannot act as an allowlist"):
        return

    found = _vault_properties()
    job.record("properties_in_vault", len(found))
    job.record("properties_declared", len(known))

    unknown = sorted(set(found) - known)

    # A near-miss is far more serious than a genuinely new property: it means a
    # note silently fell out of whatever reads the real one.
    typos, novel = [], []
    for prop in unknown:
        close = get_close_matches(prop, known, n=1, cutoff=0.85)
        (typos if close else novel).append((prop, close[0] if close else None))

    for prop, looks_like in typos:
        job.check(f"`{prop}` looks like a typo of `{looks_like}`", ok=False,
                  tier=IMPORTANT, owner=YOU, key=f"typo|{prop}",
                  covers="property-registry-drift",
                  detail=f"used in {found[prop]} note(s). If it should be `{looks_like}`, "
                         f"those notes are silently excluded from whatever reads it")

    job.record("undeclared_properties", len(novel))
    for prop, _ in novel:
        job.check(f"`{prop}` is not in the property registry", ok=False,
                  tier=ROUTINE, owner=YOU, key=f"undeclared|{prop}",
                  covers="property-registry-drift",
                  detail=f"used in {found[prop]} note(s). Add it to "
                         f"_system/frontmatter-properties.md or rename it")

    # The reverse: the doc claims a property the vault no longer uses.
    unscanned = _unscanned_prefixes()
    dead = sorted(p for p in (known - set(found) - set(dormant))
                  if not (unscanned and p.startswith(unscanned)))
    job.record("declared_but_unused", len(dead))
    if dead:
        job.check(f"{len(dead)} declared properties are unused", ok=False,
                  tier=ROUTINE, owner=YOU, key="dead-properties",
                  covers="property-registry-drift",
                  detail=f"registry lists {', '.join(dead[:6])}"
                         f"{' and more' if len(dead) > 6 else ''} but no note uses them")


def _known_properties() -> set[str]:
    if not REGISTRY.exists():
        return set()
    m = re.search(r"```known-properties\n(.*?)```", REGISTRY.read_text(errors="ignore"), re.S)
    if not m:
        return set()
    words = set()
    for line in m.group(1).split("\n"):
        line = line.split("#")[0]
        words.update(w for w in line.split() if w)
    return words


def _registry_block(name: str) -> tuple[str, ...]:
    """Words in a fenced ```<name> block of the registry, comments stripped."""
    if not REGISTRY.exists():
        return ()
    m = re.search(rf"```{name}\n(.*?)```", REGISTRY.read_text(errors="ignore"), re.S)
    if not m:
        return ()
    return tuple(w for line in m.group(1).split("\n")
                 for w in line.split("#")[0].split() if w)


def _unscanned_prefixes() -> tuple[str, ...]:
    """Namespaces whose notes this job deliberately never reads.

    `Course Archive` is in SKIP_DIRS, so every `canvas_` property is declared
    and permanently invisible - which the dead-property check would report as
    drift every week, forever. Declaring the prefix says "absent here is
    expected" rather than silencing the check wholesale.
    """
    return _registry_block("unscanned-prefixes")


def _vault_properties() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _notes():
        text = path.read_text(errors="ignore")
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end < 0:
            continue
        for key in FRONT_KEY.findall(text[3:end]):
            counts[key] = counts.get(key, 0) + 1
    return counts


# ---- 2, 3, 4. document inventory ------------------------------------------

def _check_component_rows(job: Job) -> None:
    """Every _system/ doc should appear in SYSTEM.md's component table, and
    every row should point at a file that exists."""
    if not SYSTEM.exists():
        job.check("SYSTEM.md present", ok=False, tier=IMPORTANT, owner=YOU,
                  detail=f"{SYSTEM} is missing")
        return

    text = SYSTEM.read_text(errors="ignore")
    # rglob, not glob: _system/maintenance/ holds five docs, and a
    # non-recursive scan would report them as absent from SYSTEM.md
    # while quietly no longer checking them at all.
    # Generated records (a brief a week, an alert log a month) are not
    # components and must not each need a SYSTEM.md row.
    # Nor are archived proposals or Claude's memory entries: on 2026-10-04
    # they were 9 of the 13 docs this check reported, which buried the real ones.
    docs = {p.stem for p in (config.VAULT / "_system").rglob("*.md")
            if not p.stem.startswith("_") and not check_docs.is_generated(p)
            and "Archive" not in p.parts
            and p.parent != config.VAULT / "_system" / "claude" / "memory"}
    docs -= {"SYSTEM", "README"}

    missing = sorted(d for d in docs if d not in text)
    job.record("system_docs", len(docs))
    job.record("docs_missing_from_components", len(missing))

    if missing:
        job.check(f"{len(missing)} _system doc(s) absent from SYSTEM.md", ok=False,
                  tier=IMPORTANT, owner=YOU, key="components-missing",
                  covers="system-components-drift",
                  detail=f"{', '.join(missing[:6])}"
                         f"{' and more' if len(missing) > 6 else ''} - a component doc "
                         f"nobody lists is a component nobody knows exists")
    else:
        job.check("every _system doc is listed in SYSTEM.md", ok=True,
                  tier=IMPORTANT, owner=YOU, covers="system-components-drift")


def _check_vault_map(job: Job) -> None:
    """README.md claims a folder map. It drifted for three weeks in August."""
    if not VAULT_MAP.exists():
        job.check("Vault Map present", ok=False, tier=IMPORTANT, owner=YOU,
                  detail=f"{VAULT_MAP} is missing")
        return

    text = VAULT_MAP.read_text(errors="ignore")
    actual = {p.name for p in config.VAULT.iterdir()
              if p.is_dir() and not p.name.startswith(".")}
    missing = sorted(f for f in actual if f not in text)
    job.record("top_level_folders", len(actual))

    if missing:
        job.check(f"{len(missing)} folder(s) missing from the Vault Map", ok=False,
                  tier=IMPORTANT, owner=YOU, key="vaultmap-folders",
                  covers="vault-map-drift",
                  detail=f"{', '.join(missing)} exist but are not described in "
                         f"_system/README.md")
    else:
        job.check("Vault Map lists every top-level folder", ok=True,
                  tier=IMPORTANT, owner=YOU, covers="vault-map-drift")


def _check_wikilinks(job: Job) -> None:
    """Dead links fail silently. Routine, because the router explicitly allows
    a forward link to a note not written yet.

    Three defects fixed 2026-08-28 after this reported 41 items of which 40
    were working links:

    1. The index only held `.md` stems, so every `![[file.pdf]]` embed read as
       dead. 31 of the 41 were live <COACHING_PROJECT> course attachments.
    2. Notion/, Archive/ and Clippings/ were skipped as link SOURCES and also
       missing from the index, so links pointing INTO them read as dead. They
       resolve fine in Obsidian, which has no such exclusion.
    3. No Unicode normalisation. macOS stores filenames NFD while note bodies
       are NFC, so any accented target - Steuererklärung - could never match.

    The lesson worth keeping: a check reporting 41 findings of which 1 is real
    is worse than no check. It sat at "failing (reported)" for a week and
    nobody could act on it.
    """
    import unicodedata

    def key(s: str) -> str:
        return unicodedata.normalize("NFC", s).casefold()

    # Every file, not just markdown, and including the directories excluded as
    # link sources - Obsidian resolves into them perfectly well.
    index = {key(p.stem) for p in config.VAULT.rglob("*") if p.is_file()}
    index |= {key(p.name) for p in config.VAULT.rglob("*") if p.is_file()}

    # vault-host deliberately does not sync Attachments/ - 247MB of the
    # vault's 276MB stays off the box. So the box structurally cannot resolve
    # an attachment embed and would report all 31 as dead forever, while the
    # Mac reports none. A check whose answer depends on which machine ran it
    # is not a check, so binary embeds are out of scope entirely.
    ATTACHMENT_SUFFIXES = (".pdf", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
                           ".mp3", ".mp4", ".m4a", ".mov", ".wav", ".heic", ".zip")

    dead: dict[str, int] = {}
    skipped_attachments = 0
    for path in _notes():
        body = re.sub(r"```.*?```", "", path.read_text(errors="ignore"), flags=re.S)
        body = re.sub(r"`[^`\n]*`", "", body)
        for target in WIKILINK.findall(body):
            stem = target.strip().split("/")[-1]
            if stem.lower().endswith(ATTACHMENT_SUFFIXES):
                skipped_attachments += 1
                continue
            if stem and key(stem) not in index:
                dead[stem] = dead.get(stem, 0) + 1

    job.record("attachment_embeds_skipped", skipped_attachments)
    job.record("dead_wikilink_targets", len(dead))
    if dead:
        top = sorted(dead.items(), key=lambda kv: -kv[1])[:5]
        job.record("most_linked_missing", [f"{k} ({v}x)" for k, v in top])
        job.check(f"{len(dead)} wikilink target(s) do not exist", ok=False,
                  tier=ROUTINE, owner=YOU, key="dead-wikilinks",
                  covers="dead-wikilinks",
                  detail=f"most linked: {', '.join(f'{k} ({v}x)' for k, v in top)}. "
                         f"Some are deliberate forward links; a repeatedly-linked one "
                         f"is usually a rename that left links behind")


# ---- 5. docs behind their code, timers behind their docs (Nov 8) -----------

# The Nov 8 audit found the same two shapes again and again. A build session
# updated the section of a doc it touched but not the doc's status line,
# `verified:` date or SYSTEM.md row, so the top of the doc told the past. And
# what systemd runs drifted from what the docs say: carmen-kit-sync.timer was
# documented but never installed for 9 days, and bike_watch fired every 15
# minutes for a month after its listing expired.

AUTOMATION = config.HOME / "automation"
UNIT_DIRS = (AUTOMATION / "units", config.HOME / "carmen" / "units")
PT = "America/Los_Angeles"
CODE_PATH = re.compile(
    r"(?<![\w/.~-])(?:~/automation/)?((?:jobs|tools|runner|bin|units|tests)/[\w@.\-/]*[\w@])")
# Commits that rename or scrub strings across many files say nothing about
# whether a doc's claims still hold.
COSMETIC = re.compile(r"^(Public-kit hygiene|Neutral names)", re.I)
VERIFIED = re.compile(r"^verified:\s*(\d{4}-\d{2}-\d{2})", re.M)
STAGE = re.compile(r"^stage:\s*(\w[\w-]*)", re.M)
EXPIRES = re.compile(r"^EXPIRES\s*=\s*[\"'](\d{4}-\d{2}-\d{2})[\"']", re.M)
OWN_TIMER = re.compile(r"^(automation@[\w-]+|carmen-[\w-]+|backup-vault|claude-auth-watch)\.timer$")
# Shared plumbing nearly every build touches. A doc naming one of these is not
# behind when a commit for some other component changes it (first run, Nov 8:
# wispr-import was flagged only because `bin/vt` gained a subcommand).
HUBS = {"bin/vt", "runner/config.py", "runner/job.py", "jobs/inventory.py"}


def doc_code_paths(text: str, exists=lambda p: (AUTOMATION / p).exists()) -> list[str]:
    """Paths in ~/automation a doc names, only those that exist, hubs left out."""
    seen = []
    for m in CODE_PATH.finditer(text):
        p = m.group(1).rstrip("./")
        if p not in seen and p not in HUBS and exists(p):
            seen.append(p)
    return seen


def doc_behind(verified: str, path_dates: dict[str, str]) -> list[tuple[str, str]]:
    """(path, date) for every named path with a real commit after `verified`,
    newest first."""
    late = [(p, d) for p, d in path_dates.items() if d and d > verified]
    return sorted(late, key=lambda pd: pd[1], reverse=True)


def _last_change(path: str, cache: dict[str, str]) -> str:
    """Pacific date of the newest non-cosmetic commit touching `path`."""
    if path not in cache:
        import os, subprocess
        r = subprocess.run(["git", "-C", str(AUTOMATION), "log", "-20",
                            "--date=format-local:%Y-%m-%d", "--format=%cd|%s", "--", path],
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "TZ": PT})
        cache[path] = next((l.split("|", 1)[0] for l in r.stdout.splitlines()
                            if "|" in l and not COSMETIC.match(l.split("|", 1)[1])), "")
    return cache[path]


def _check_docs_behind_code(job: Job) -> None:
    cache: dict[str, str] = {}
    behind = []
    for path in check_docs.all_component_docs(config.VAULT):
        text = path.read_text(errors="ignore")
        v, stage = VERIFIED.search(text), STAGE.search(text)
        if not v or (stage and stage.group(1) in ("superseded", "dead")):
            continue
        late = doc_behind(v.group(1), {p: _last_change(p, cache) for p in doc_code_paths(text)})
        if late:
            behind.append(f"{path.stem} (verified {v.group(1)}; {late[0][0]} changed "
                          f"{late[0][1]}{f', +{len(late) - 1} more' if len(late) > 1 else ''})")
    job.record("docs_behind_code", len(behind))
    job.check(f"{len(behind)} component doc(s) older than their code" if behind
              else "every component doc verified after its code last changed",
              ok=not behind, tier=ROUTINE, owner=YOU, key="doc-code-drift",
              covers="doc-code-drift",
              detail="; ".join(behind[:8]) + (" and more" if len(behind) > 8 else "")
                     + ". Check the status line, `verified:`, Config and the SYSTEM.md row "
                       "against the code, then set `verified:`")


def timer_findings(active: set[str], in_repo: set[str], docs_text: str,
                   expires: dict[str, str], today: str) -> dict[str, list[str]]:
    """Pure comparison of what systemd runs, what the repos ship and what the
    docs say. Timer names are unit names, e.g. `automation@heartbeat.timer`."""
    def stem(t: str) -> str:
        return t.removesuffix(".timer").removeprefix("automation@")
    return {
        "not_installed": sorted(t for t in in_repo - active if stem(t) != "example"),
        "undocumented": sorted(t for t in active if stem(t) not in docs_text),
        "expired": sorted(f"{t} (expired {expires[stem(t)]})" for t in active
                          if stem(t) in expires and expires[stem(t)] < today),
    }


def _active_timers() -> set[str]:
    import subprocess
    r = subprocess.run(["systemctl", "list-timers", "--all", "--no-legend", "--plain"],
                       capture_output=True, text=True, timeout=30)
    return {tok for line in r.stdout.splitlines() for tok in line.split()
            if OWN_TIMER.match(tok)}


def _check_timers(job: Job) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    active = _active_timers()
    if not job.check("systemd timers readable", ok=bool(active), tier=ROUTINE, owner=YOU,
                     key="timers-unreadable", covers="timer-drift",
                     detail="`systemctl list-timers` returned none of ours"):
        return
    in_repo = {p.name for d in UNIT_DIRS if d.is_dir() for p in d.glob("*.timer")}
    docs_text = "\n".join(p.read_text(errors="ignore")
                          for p in (config.VAULT / "_system").rglob("*.md")
                          if "Archive" not in p.parts and not check_docs.is_generated(p))
    expires = {}
    for f in (AUTOMATION / "jobs").glob("*.py"):
        m = EXPIRES.search(f.read_text(errors="ignore"))
        if m:
            expires[f.stem] = m.group(1)
    today = datetime.now(ZoneInfo(PT)).date().isoformat()
    found = timer_findings(active, in_repo, docs_text, expires, today)
    job.record("timers_active", len(active))
    job.check(f"{len(found['not_installed'])} timer(s) in a repo but not installed"
              if found["not_installed"] else "every timer in the repos is installed",
              ok=not found["not_installed"], tier=ROUTINE, owner=YOU,
              key="timer-not-installed", covers="timer-drift",
              detail=", ".join(found["not_installed"]) + ". Install it, or say in its doc that "
                     "it is off by decision and move the unit out of units/")
    job.check(f"{len(found['undocumented'])} running timer(s) no _system doc names"
              if found["undocumented"] else "every running timer is named in a doc",
              ok=not found["undocumented"], tier=ROUTINE, owner=YOU,
              key="timer-undocumented", covers="timer-drift",
              detail=", ".join(found["undocumented"]) + ". A job nobody documents is a job "
                     "nobody notices when it should stop")
    job.check(f"{len(found['expired'])} job(s) still scheduled past their EXPIRES date"
              if found["expired"] else "no job runs past its EXPIRES date",
              ok=not found["expired"], tier=IMPORTANT, owner=YOU,
              key="job-expired", covers="job-expiry",
              detail=", ".join(found["expired"]) + ". Disable the timer and remove the job "
                     "(the way bike_watch was removed, ab0248a)")


def _notes():
    for path in config.VAULT.rglob("*.md"):
        if not any(part in SKIP_DIRS for part in path.parts):
            yield path


if __name__ == "__main__":
    raise SystemExit(main())
