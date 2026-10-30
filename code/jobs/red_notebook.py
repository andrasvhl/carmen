"""Daily: distil pending #coreLearning material into the Red Notebook.

`jobs/core_learnings.py` finds what is waiting. This writes it, on the pattern
`jobs/vaultmap.py` established: one `runner.ask` call, guarded in Python, written
without waiting for approval, with the runner's vault git commit as the revert
path. The three gaps that used to forbid this closed on 2026-08-28 - see
`_system/maintenance/scheduled-execution.md`, where each now carries a status
line.

## One deliberate difference from vaultmap

vaultmap asks the model to rewrite the *whole file* and diffs the answer. That is
right there: the Vault Map describes a directory tree, so a wrong answer is
detectable by regenerating it. **A distillation has no ground truth** - it is a
judgement about meaning, and nothing can diff it against reality.

So the model never sees or returns the notebook. It returns only the new entries;
Python does the insertion. The append-only guarantee then holds by construction
rather than by inspection: no existing byte can change, because no existing byte
was ever in the model's answer. That is strictly stronger than a prefix check,
and it is the whole reason this is safe to run unattended on a file thirteen wiki
articles cite.

What Python still cannot check is whether the distillation is *good*. It checks
that nothing was destroyed, that every pending item is accounted for exactly
once, and that each entry is notebook-shaped. Wording stays reviewable after the
fact, in the commit.

    ssh vault-host '~/automation/bin/run-job red_notebook --dry-run'
"""

import re
from datetime import date

from runner import config
from runner.ask import AskFailed, ask
from runner.job import IMPORTANT, ROUTINE, YOU, Job, log

from jobs.core_learnings import (CONSOLIDATED, NOTEBOOK, _cited_by_notebook,
                                 _tagged)

NOTE = NOTEBOOK / "Red Notebook.md"
MECHANICS = "\n---\n\n## How this note works"

# Distilling twenty items in one unattended pass is a bigger judgement than any
# guard below can cover. Past this, the job reports and leaves it to a session.
MAX_PER_RUN = 6

# Real notebook entries run to 96 words at the outside (34 measured, median 26).
# 130 is loose enough not to fight a genuinely long one and tight enough to
# catch an answer that pasted the source note instead of distilling it.
MAX_ENTRY_WORDS = 130

ASK_TIMEOUT = 420
ARROW = "→"

# Language is checked, not just asked for. On the first real test the model was
# handed an English note and returned a German entry - every structural guard
# passed, because the wording is exactly what they cannot see. The notebook is
# roughly half German, so the pull toward it is strong and a prompt line alone
# does not hold. Python now detects the source language, states it per note, and
# refuses an answer that switched.
GERMAN = {"der","die","das","und","ich","nicht","ist","mich","dass","wenn","für",
          "eine","einen","mir","auch","aber","noch","sich","habe","war","wird",
          "mit","von","zu","ein","es","sie","dem","den","im","auf","als",
          # Added 2026-10-05: "10k laufen hat meinen kater so viel besser gemacht"
          # matched no word in either set and was called English. A short
          # capture is exactly where the original list was too thin.
          "hat","mein","meine","meinen","meiner","viel","sehr","sind","wir",
          "kann","muss","sollte","jetzt","schon","nur","oder","weil","über",
          "bei","nach","wie"}
ENGLISH = {"the","and","not","is","that","if","for","a","to","of","it","in","my",
          "was","are","this","with","on","but","have","i","me","what","they",
          "be","from","at","you","should","would"}


def _language(text: str) -> str:
    words = re.findall(r"[a-zA-ZäöüÄÖÜß]+", text.lower())
    de = sum(w in GERMAN for w in words)
    en = sum(w in ENGLISH for w in words)
    if de == en:
        # No signal from stopwords: an umlaut is the next best one.
        return "German" if re.search(r"[äöüÄÖÜß]", text) else "English"
    return "German" if de > en else "English"

PROMPT = """\
The Red Notebook is the owner's record of his most important life learnings. It is
terse: entries are single arrow-prefixed paragraphs, a median of 26 words, in his
own voice, in English or German depending on how he was thinking. It is not a
summary document and not advice - each line is something he concluded.

Below are {n} note(s) he has marked `#coreLearning`. Write one notebook entry for
each, in the order given.

Rules, all binding:

- One entry per note. Exactly {n} entries. Never merge two notes into one entry,
  never split one note into two.
- Start each entry with `{arrow} ` and keep it to one paragraph.
- **Each note states its LANGUAGE. Write that entry in that language, always.**
  The notebook contains both English and German entries; what the neighbouring
  entries are written in is irrelevant. An English note gets an English entry
  even if every other entry on the page is German, and the reverse. An answer in
  the wrong language is rejected.
- Use his words wherever they work. You are compressing, not rephrasing. Never
  add a claim he did not make, never soften one he did.
- A note of 80 words or fewer is already notebook-shaped: keep it essentially
  verbatim. A longer one is distilled to the learning itself - the reasoning,
  sources, reading lists and any AI-written material stay in the original note
  and must not appear here.
- After each entry, on its own line, write exactly this attribution and nothing
  else: *DATE · [[LINK|full note]]* using the DATE and LINK given for that note.
- Separate entries with one blank line.

Output the entries only. No preamble, no heading, no commentary, no code fence.

{items}
"""


def main() -> int:
    with Job("red_notebook", description="Red Notebook distillation",
             watches=("red-notebook-consolidation",)) as job:

        if not job.check("Red Notebook present", ok=NOTE.is_file(),
                         tier=IMPORTANT, owner=YOU, covers="red-notebook-consolidation",
                         detail=f"{NOTE} is missing; nothing can be appended"):
            return 0

        cited = _cited_by_notebook()
        pending = sorted(
            ((p, c) for p, c in _tagged()
             if config.INBOX in p.parents or p.stem not in cited),
            key=lambda t: (t[1] or date.max, t[0].name))

        job.record("pending at start", len(pending))
        if not pending:
            job.check("Red Notebook: nothing to distil", ok=True,
                      tier=ROUTINE, owner=YOU, covers="red-notebook-consolidation")
            return 0

        if len(pending) > MAX_PER_RUN:
            job.check("Red Notebook backlog small enough to distil unattended",
                      ok=False, tier=IMPORTANT, owner=YOU, key="rn-too-many",
                      covers="red-notebook-consolidation",
                      detail=f"{len(pending)} pending, over the {MAX_PER_RUN} this job will "
                             f"write in one pass. Distilling that many at once is a judgement "
                             f"no guard here covers — run it as a session instead.")
            return 0

        original = NOTE.read_text(encoding="utf-8")
        try:
            entries = _strip_fence(ask(_prompt(pending), timeout=ASK_TIMEOUT))
            _guard(entries, pending)
        except AskFailed as exc:
            # Never a pass: "could not distil" and "nothing to distil" must stay
            # different outcomes, or a blind week reads as a clean one.
            job.check("Red Notebook distillation attempted", ok=False,
                      tier=IMPORTANT, owner=YOU, covers="red-notebook-consolidation",
                      detail=f"could not distil: {exc}. The backlog is untouched, not cleared.")
            return 0

        updated = _insert(original, entries)
        if job.dry_run:
            # A dry run that will not show you what it would write is a dry run
            # you cannot judge. The wording is the only part no guard checks.
            log("red_notebook: would append --------------------------------")
            for line in entries.splitlines():
                log(f"  {line}")
            log("red_notebook: ------------------------------------------------")
        if not job.dry_run:
            NOTE.write_text(updated, encoding="utf-8")
            moved = _close(pending)
        else:
            moved = 0

        job.record("entries appended", len(pending))
        job.record("captures moved to Core Learnings", moved)
        job.check("Red Notebook updated", ok=True, tier=IMPORTANT, owner=YOU,
                  covers="red-notebook-consolidation",
                  detail=f"{len(pending)} entr(y/ies) appended: "
                         + "; ".join(p.stem[:48] for p, _ in pending)
                         + (" (dry run, not written)" if job.dry_run else ""))
        log(f"red_notebook: {len(pending)} distilled, {moved} moved")
    return 0


def _prompt(pending) -> str:
    items = []
    for i, (path, captured) in enumerate(pending, 1):
        body = path.read_text(encoding="utf-8").split("---", 2)[-1].strip()
        items.append(f"--- NOTE {i} ---\nDATE: {(captured or date.today()).isoformat()}\n"
                     f"LINK: {path.stem}\nLANGUAGE: {_language(body)}\n\n{body}")
    return PROMPT.format(n=len(pending), arrow=ARROW, items="\n\n".join(items))


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return t.strip()


def _guard(entries: str, pending) -> None:
    """Refuse an answer that misunderstood the task. Raises AskFailed.

    Only structural claims are checkable here. Whether the distillation is
    faithful is not, which is why the wording stays reviewable in the commit.
    """
    if not entries:
        raise AskFailed("empty answer")
    if not entries.startswith(ARROW):
        raise AskFailed(f"answer opens {entries[:80]!r}, which is preamble, not an entry")

    blocks = [b for b in entries.split("\n\n") if b.strip().startswith(ARROW)]
    if len(blocks) != len(pending):
        raise AskFailed(f"{len(blocks)} entries for {len(pending)} notes; must be one each")

    for block in blocks:
        words = len(block.split())
        if words > MAX_ENTRY_WORDS:
            raise AskFailed(f"an entry is {words} words, over {MAX_ENTRY_WORDS} — that is the "
                            f"source note pasted in, not a distillation")

    # Every pending note must be linked exactly once. This is what stops an item
    # being silently dropped (it would then be distilled again next week) or
    # double-counted.
    for path, _ in pending:
        n = entries.count(f"[[{path.stem}|full note]]")
        if n != 1:
            raise AskFailed(f"{path.stem[:40]!r} is linked {n} times, expected exactly 1")

    # Language, per entry, against its own source. Checked rather than trusted:
    # see the note on GERMAN/ENGLISH above for the failure that put this here.
    for block, (path, _) in zip(blocks, pending):
        want = _language(path.read_text(encoding="utf-8").split("---", 2)[-1])
        got = _language(block)
        if want != got:
            raise AskFailed(f"{path.stem[:40]!r} is written in {want} but its entry came back "
                            f"in {got}; the notebook keeps each learning in the language it "
                            f"was thought in")


def _insert(original: str, entries: str) -> str:
    """Put the entries in front of the mechanics section. Python does this, not
    the model, so nothing outside the inserted block can change."""
    if MECHANICS not in original:
        raise AskFailed(f"{NOTE.name} has no '## How this note works' section to insert above")
    head, _, tail = original.partition(MECHANICS)
    return head.rstrip("\n") + "\n\n" + entries.strip() + "\n" + MECHANICS + tail


def _close(pending) -> int:
    """Move consolidated captures out of Inbox. Passages need nothing - the
    notebook now cites them, which is what closes those."""
    CONSOLIDATED.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path, _ in pending:
        if config.INBOX in path.parents:
            path.rename(CONSOLIDATED / path.name)
            moved += 1
    return moved


if __name__ == "__main__":
    raise SystemExit(main())
