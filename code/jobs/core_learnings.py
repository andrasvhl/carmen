"""Daily: how much #coreLearning material is waiting to reach the Red Notebook?

the owner separates important learnings from ordinary ones. The important tier is
marked `#coreLearning`, and it arrives two ways:

  - **Captures.** Ticking "Red Notebook" in Raycast, or picking it on the phone,
    writes a note into `Inbox/` with `#coreLearning` as its first body line.
  - **Marked passages.** He tags a line mid-text in a note he is already writing
    (`Projects/<PROJECT>/<PRIVATE_NOTE>.md` does this). The host note has its own
    life and its own topic; only the passage is a core learning.

The two need different handling and the job reports them separately. A capture is
consolidated by distilling it into the notebook and moving the note into
`Resources/Red Notebook/Core Learnings/`. A marked passage cannot move anywhere -
its note belongs where it is - so it counts as consolidated once the notebook
cites the host note, which is state the notebook already carries and nothing has
to remember to set.

Merging itself is never automatic. It needs judgement - which entries belong, how
they are worded, what supersedes what - and the notebook is a *wiki source* that
thirteen articles draw on, so an unattended job rewriting it would corrupt the
provenance chain with nobody watching. `_system/maintenance/automation.md` says
it plainly: when AI jobs arrive they must record, never check. So this is the
same split as `jobs/wiki.py` - **detection is automated, the writing half stays a
session the owner starts.** It reads, counts and reports. It writes nothing, ever,
which is what makes it safe to run unattended.

    ssh vault-host '~/automation/bin/run-job core_learnings --dry-run'
"""

import re
from datetime import date, datetime

from runner import config
from runner.job import Job, log, ROUTINE, YOU

NOTEBOOK = config.VAULT / "Resources" / "Red Notebook"
CONSOLIDATED = NOTEBOOK / "Core Learnings"

# Folders that can never hold a pending learning.
#   Wiki/      - derived, never authored; it describes the tag, it does not use it
#   Red Notebook/ - the destination itself, plus everything already consolidated
#   Archive/   - finished work
SKIP_DIRS = ("Wiki", "Archive", "Resources/Red Notebook")

# A tag inside a code span or fence is documentation ABOUT the tag, not a use of
# it. This is the single discriminator the whole job rests on, and it is not a
# heuristic: Obsidian's own indexer does not treat `#coreLearning` in backticks
# as a tag either, so matching bare-only is matching what Obsidian matches.
#
# It was measured, not assumed. Across the vault on 2026-09-11, all 16 mentions
# in `_system/`, `Wiki/` and task notes were backticked, and all 3 real uses were
# bare - including two in a `type: action` note that quotes the tag while
# explaining it. Gating on `type: learning` instead would have been wrong twice
# over: it would have missed that a note can be an action AND a core learning,
# and it would still have counted prose that merely mentions the tag.
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
SPAN = re.compile(r"`[^`\n]*`")
TAG = re.compile(r"(?:^|\s)#coreLearning(?![\w/-])")

# The negative lookahead keeps `#coreLearnings` out. The plural is a live hazard:
# the owner typed it when specifying this feature, and Obsidian would have treated
# it as a separate tag, silently splitting the set in two.

FRONT_TYPE = re.compile(r"^type:\s*(\S+)", re.M)
CAPTURED = re.compile(r"^captured:\s*(\d{4}-\d{2}-\d{2})", re.M)
WIKILINK = re.compile(r"\[\[([^\]|#]+)")

# A backlog is normal; a backlog nobody drains is the failure. 10 is about a
# third of the physical notebook's 34 entries - enough that merging is a real
# sitting. 35 days is the registry's monthly cadence plus slack, so an on-time
# month never trips it.
MAX_PENDING = 10
MAX_AGE_DAYS = 35


def main() -> int:
    with Job("core_learnings", description="Red Notebook consolidation backlog",
             watches=("red-notebook-consolidation",)) as job:

        # If the folder is gone, every count below reads as a healthy zero.
        # "Nothing pending" and "I could not look" must never render alike.
        if not job.check(
            "Red Notebook folder structure intact",
            ok=NOTEBOOK.is_dir() and (NOTEBOOK / "Red Notebook.md").is_file(),
            tier=ROUTINE, owner=YOU, key="cl-structure",
            covers="red-notebook-consolidation",
            detail=f"expected the folder note at {NOTEBOOK / 'Red Notebook.md'}. "
                   f"Without it a backlog of any size reports as zero pending.",
        ):
            return 0

        cited = _cited_by_notebook()
        captures, passages = [], []
        for path, captured in _tagged():
            if config.INBOX in path.parents:
                captures.append((path, captured))
            elif path.stem not in cited:
                passages.append((path, captured))

        pending = sorted(captures + passages,
                         key=lambda t: (t[1] or date.max, t[0].name))
        job.record("captures pending consolidation", len(captures))
        job.record("marked passages not yet cited", len(passages))
        job.record("captures already consolidated",
                   len(list(CONSOLIDATED.glob("*.md"))) if CONSOLIDATED.is_dir() else 0)

        dated = [c for _, c in pending if c]
        oldest_age = _age(min(dated)) if dated else 0
        if pending:
            job.record("oldest pending, days", oldest_age)

        too_many = len(pending) > MAX_PENDING
        too_old = oldest_age > MAX_AGE_DAYS
        job.check(
            "Red Notebook consolidation backlog is under control",
            ok=not (too_many or too_old),
            tier=ROUTINE, owner=YOU, key="cl-backlog",
            covers="red-notebook-consolidation",
            detail=_detail(captures, passages, too_many, too_old, oldest_age),
        )

        log(f"core_learnings: {len(captures)} captures, {len(passages)} passages, "
            f"oldest {oldest_age}d")
    return 0


def _body(text: str) -> str:
    """The note with code fences and spans blanked out, so only real tags match."""
    return SPAN.sub("", FENCE.sub("", text))


def _tagged():
    """Every note in the vault carrying a bare #coreLearning tag."""
    for path in config.VAULT.rglob("*.md"):
        rel = path.relative_to(config.VAULT).as_posix()
        if rel.startswith(".") or any(rel.startswith(d + "/") for d in SKIP_DIRS):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not TAG.search(_body(text)):
            continue
        stamp = CAPTURED.search(text)
        captured = None
        if stamp:
            try:
                captured = datetime.strptime(stamp.group(1), "%Y-%m-%d").date()
            except ValueError:
                captured = None
        yield path, captured


def _cited_by_notebook() -> set:
    """Note names the notebook already links to - i.e. already folded in.

    Derived from the notebook rather than from a flag on each note. A marked
    passage lives inside a note about something else, so there is nowhere to put
    a 'consolidated' marker that would not be lying about the whole note. The
    notebook citing it is the same fact, already recorded, and it cannot drift
    out of sync with itself.
    """
    cited = set()
    if not NOTEBOOK.is_dir():
        return cited
    for path in NOTEBOOK.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for target in WIKILINK.findall(text):
            cited.add(target.strip().split("/")[-1])
    return cited


def _age(when: date) -> int:
    return (date.today() - when).days


def _detail(captures, passages, too_many, too_old, oldest_age) -> str:
    why = []
    if too_many:
        why.append(f"{len(captures) + len(passages)} pending, over the {MAX_PENDING} threshold")
    if too_old:
        why.append(f"oldest is {oldest_age} days old, over {MAX_AGE_DAYS}")
    out = [" and ".join(why) + ".", ""]

    # Named, not summarised: a merge session needs to know WHICH notes. The list
    # is bounded by the threshold that triggered it, so it stays readable.
    if captures:
        out.append("Captures in Inbox/ — distil, then move to Red Notebook/Core Learnings/:")
        out += _rows(captures)
        out.append("")
    if passages:
        out.append("Tagged mid-text elsewhere — the note stays put; the notebook should cite it:")
        out += _rows(passages, show_dir=True)
        out.append("")
    out.append("Merging is a session you start, never automatic — the notebook is a wiki "
               "source and rewriting it unattended would corrupt provenance.")
    return "\n".join(out)


def _rows(items, show_dir=False):
    rows = []
    for path, captured in items[:20]:
        stamp = captured.isoformat() if captured else "no captured date"
        name = (path.relative_to(config.VAULT).as_posix() if show_dir else path.name)
        rows.append(f"  - {name}  ({stamp})")
    if len(items) > 20:
        rows.append(f"  ... and {len(items) - 20} more")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
