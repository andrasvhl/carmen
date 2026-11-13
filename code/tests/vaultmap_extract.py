#!/usr/bin/env python3
"""The Vault Map job must survive a model that narrates.

    ~/automation/.venv/bin/python -m tests.vaultmap_extract

vaultmap failed every run from 2026-09-02 to 2026-09-14 -- three weeks on a
weekly timer -- and all three failures were one sentence of chat before the
file. The strings below are the real ones, copied from the job's own reports.

The file came after the narration every time, so the answers were usable and
were thrown away. Worse, a refusal and a genuinely broken model reported
identically ("could not check"), so nothing distinguished three weeks of
harmless narration from three weeks of real breakage.

The last two assertions are the ones that must never regress: preamble must not
reach the file, and an answer with no file in it must still fail. Being lenient
about where the file starts is only safe while both of those hold.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner.ask import AskFailed
from jobs.vaultmap import _extract, _guard, _undo_perturbations, BEGIN, END

ORIGINAL = ("# Vault structure\n\nStart at the Router.\n\n"
            "## What this file covers\n\nEverything.\n\n"
            "## Folder conventions\n\nRules.\n\n"
            "## Loose files in the vault root\n\nNone.\n")
FILE = ORIGINAL.strip()

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def ext(answer):
    return _extract(answer, ORIGINAL)


# --- the three real failures ----------------------------------------------
REAL = [
    ("2026-09-02", "Here is the corrected file, per the instruction to output raw "
                   "content only.\n\n"),
    ("2026-09-07", "I verified this against the actual directory tree and found two "
                   "lines that no longer match.\n\n"),
    ("2026-09-14", "Since the Edit tool is blocked in this mode, I'll output the "
                   "corrected file content below.\n\n"),
]
for day, preamble in REAL:
    got = ext(preamble + FILE)
    check(f"{day} preamble is discarded", got.startswith("# Vault structure"), repr(got[:60]))
    check(f"{day} file survives intact", got.strip() == FILE, repr(got[:60]))

# --- the markers it is now asked for ---------------------------------------
got = ext(f"Sure, here you go.\n{BEGIN}\n{FILE}\n{END}\nHope that helps!")
check("marker-fenced answer extracts cleanly", got.strip() == FILE, repr(got[:60]))
check("text after the closing marker is dropped", "Hope that helps" not in got)

# --- code fences, with and without markers ---------------------------------
check("bare code fence is stripped",
      ext(f"```markdown\n{FILE}\n```").strip() == FILE)
check("fence inside markers is stripped",
      ext(f"{BEGIN}\n```\n{FILE}\n```\n{END}").strip() == FILE)

# --- a clean answer still works --------------------------------------------
check("an answer that is just the file is unchanged", ext(FILE).strip() == FILE)

# --- what must still fail --------------------------------------------------
try:
    ext("I could not read the vault, so I have not produced a map.")
    check("an answer with no file in it is refused", False, "no AskFailed raised")
except AskFailed as e:
    check("an answer with no file in it is refused", True)
    check("...and says what it got", "opens" in str(e), str(e)[:80])

# preamble must never reach the file, by any path
for _, preamble in REAL:
    got = ext(preamble + FILE)
    check("preamble never reaches the file",
          preamble.split("\n")[0] not in got, repr(got[:80]))

# --- the guards downstream of extraction still bite ------------------------
try:
    _guard("# Vault structure\n", ORIGINAL)
    check("a truncated answer is refused", False, "no AskFailed raised")
except AskFailed:
    check("a truncated answer is refused", True)

try:
    _guard(ORIGINAL.replace("## Folder conventions", "## Something else"), ORIGINAL)
    check("dropping a protected section is refused", False, "no AskFailed raised")
except AskFailed:
    check("dropping a protected section is refused", True)

_guard(ORIGINAL, ORIGINAL)
check("an unchanged file passes the guards", True)

# --- perturbations: a correct line quietly corrupted ----------------------
# On a 2026-09-15 run the model rewrote "Backblaze B2" as "Backblaza B2" on a
# line that needed no correction. This job writes straight to the live file, so
# that lands and stays until a human happens to read that sentence.
#
# The date case below is the one that must NOT be reverted: small,
# high-similarity edits to dates and counts are exactly what this job is for.
BASE = ("# Vault structure\n"
        "- `backups.md` — vault to Backblaze B2 from the box\n"
        "- `canvas-archive.md` — built 2026-08-28, partial\n"
        "- `wiki.md` — the derived knowledge layer\n")

typo = BASE.replace("Backblaze", "Backblaza")
fixed, reverted = _undo_perturbations(BASE, typo)
check("a one-letter corruption is reverted", "Backblaze B2" in fixed, fixed)
check("...and it is reported, not silent", len(reverted) == 1, str(reverted))

dated = BASE.replace("2026-08-28", "2026-09-14")
fixed, reverted = _undo_perturbations(BASE, dated)
check("a date correction is KEPT", "2026-09-14" in fixed, fixed)
check("...and nothing is reported", not reverted, str(reverted))

real = BASE.replace("the derived knowledge layer",
                    "the derived knowledge layer, rebuilt 2026-09-02 from sources")
fixed, reverted = _undo_perturbations(BASE, real)
check("a genuine rewrite is KEPT", "rebuilt 2026-09-02" in fixed, fixed)

added = BASE + "- `sweep.md` — moves finished tasks out of the Inbox\n"
fixed, reverted = _undo_perturbations(BASE, added)
check("an added line is KEPT", "sweep.md" in fixed)
check("...with nothing reverted", not reverted, str(reverted))

wordsub = BASE.replace("partial", "complete")
fixed, reverted = _undo_perturbations(BASE, wordsub)
check("a deliberate word change is KEPT", "complete" in fixed, fixed)

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
