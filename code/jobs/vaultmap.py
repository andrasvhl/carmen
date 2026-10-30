"""Weekly: does the Vault Map still describe the vault that actually exists?

`inventory` already checks that every top-level folder is *named* somewhere in
`_system/README.md`. That check passed on 2026-09-02 while the file contained
three false sentences, because "Wiki" appeared inside the words "Wiki/ - LLM-
owned distilled knowledge, built from sources at Stage 4. Empty for now." The
wiki had been built that morning. Presence is checkable by string match;
accuracy is not, and no deterministic check ever will be.

So this job asks a model, in the weakest form available:

  - The model reads. `runner.ask` hands it Read/Glob/Grep and nothing else, so
    it cannot write to the vault even if a note it reads tells it to. Python
    writes the answer, after checking it. That separation is the safety
    property, not the review step below it.
  - It writes the map itself, and does not report having done so. Drift that
    has been corrected is not news; a failing check here would mail and open
    an Inbox note every week the job worked. The record is the commit the
    runner makes around the write, which names this job and reverts cleanly.
    Only being unable to check, or refusing an answer, alerts.
  - It never claims the map is correct. Two runs minutes apart on 2026-09-02,
    against a file nobody touched in between, returned seven findings and then
    zero. A model finding nothing is evidence of nothing, so a quiet run says
    "no drift found this run" and nothing stronger. Only a difference is a
    positive result here; silence is not.

## Why one call and not two

The first version asked twice: find what is wrong, then patch those lines. It
detected well (five real drift items on its first run, all confirmed) and
repaired badly - it wrote full vault paths into a list already scoped to
`_system/`, kept a retired file in that list with `Archive/` bolted onto the
front, and narrated "none of which this README mentions" into the document.

Every one of those is what patching looks like. Handed a list of wrong lines
and no view of the whole, a model edits a diff instead of describing a vault.
The owner's own observation ended it: writing this map with an AI from scratch has
always produced a good map, without any of this machinery. So the job now does
the thing that already worked - look at the vault, write the map - and Python
diffs the answer against the current file. The diff is the finding list, which
also removes the bespoke output format that produced two false results in three
runs by failing to parse.

    ssh vault-host '~/automation/bin/run-job vaultmap --dry-run'
"""

import difflib

from runner import config
from runner.ask import AskFailed, ask
from runner.job import IMPORTANT, YOU, Job, log

VAULT_MAP = config.VAULT / "_system" / "README.md"

# the owner's judgement rather than a description of disk state. An answer that
# drops one has misunderstood the task, and is refused rather than reviewed.
PROTECTED = ("## Folder conventions", "## Loose files in the vault root")

# An answer that loses more than a fifth of the file is a rewrite or a
# truncation, not a correction.
MIN_KEEP_RATIO = 0.8

# One model call, well inside automation@.service's 600s TimeoutStartSec, so a
# slow answer is reported as slow rather than killed and reported as a crash.
ASK_TIMEOUT = 420

PROMPT = """\
`_system/README.md` is the Vault Map. It answers one question: where does a
thing live. A person or an agent reads it to orient - what folders exist, what
each one holds, and enough description to know whether what they want is inside.
It is not a tutorial and not a routing table; `Claude Master Router.md` decides
what to read for a given task, and the map must not duplicate that.

Read it. Then look at what is actually in this vault, using Glob and Read. Output
the Vault Map as it should read today.

It describes a real directory tree, so write it from what you find rather than
from what the current file claims. But it is also a document the owner wrote, most
of it is still correct, and the wording is his:

- Keep every line that is still accurate **exactly as written, character for
  character**. Do not reword, reformat, tidy, shorten or improve anything true.
- Correct a line only where it no longer matches the vault.
- Delete an entry for something that no longer exists.
- Add an entry for something that exists and nothing covers, in the style of its
  neighbours.
- If files moved into a subfolder, give that subfolder its own subsection rather
  than lengthening the old lines.

The file states its own scope under "## What this file covers", including what it
deliberately does not enumerate. Treat that as binding: a folder excluded there
is a decision, not a gap, and adding it back is wrong.

Never write what the document does or does not mention - "none of which this
README lists" is a note to an editor, not a map entry. Leave "## Folder
conventions" and "## Loose files in the vault root" exactly as they are.

You have no write tools, and that is deliberate — Python does the writing. Do not
try to edit the file and do not remark on being unable to; just return its content.

Output the complete file between these two markers, on lines of their own, with
nothing else between them:

<<<VAULT_MAP
# Vault structure
...the entire file...
VAULT_MAP>>>

Anything you want to say goes outside the markers, where it will be discarded.
"""

# The model is told to fence its answer in these, and Python takes only what is
# between them. Preamble is then free: it lands outside the markers and is
# dropped, instead of costing a week of verification.
BEGIN, END = "<<<VAULT_MAP", "VAULT_MAP>>>"


def main() -> int:
    with Job("vaultmap", description="Vault Map accuracy") as job:
        if not job.check("Vault Map present", ok=VAULT_MAP.is_file(),
                         tier=IMPORTANT, owner=YOU, covers="vault-map-accuracy",
                         detail=f"{VAULT_MAP} is missing"):
            return 0

        original = VAULT_MAP.read_text(encoding="utf-8", errors="ignore")

        try:
            rewritten = _extract(ask(PROMPT, timeout=ASK_TIMEOUT), original)
            _guard(rewritten, original)
        except AskFailed as exc:
            # Never a pass. "I could not get a usable answer" and "the map is
            # fine" have to stay different outcomes, or a blind week reads as
            # a clean one - which is the failure this job exists to catch, one
            # level up.
            job.check("Vault Map checked", ok=False, tier=IMPORTANT, owner=YOU,
                      covers="vault-map-accuracy",
                      detail=f"could not check: {exc}. The map is unverified this "
                             f"week, not verified clean")
            return 0

        rewritten, reverted = _undo_perturbations(original, rewritten)
        job.record("vaultmap_perturbations_reverted", len(reverted))
        for r in reverted:
            log(f"vaultmap: kept original, model perturbed it — {r}")

        changed = _changed_lines(original, rewritten)
        job.record("vaultmap_changed_lines", len(changed))

        if not changed:
            job.check("Vault Map: no drift found this run", ok=True,
                      tier=IMPORTANT, owner=YOU, covers="vault-map-accuracy")
            return 0

        if not job.dry_run:
            VAULT_MAP.write_text(rewritten, encoding="utf-8")

        # Deliberately a pass, not a failure. Drift that has already been
        # corrected is not a problem anyone needs to be told about, and a
        # failing check here would mail the owner and open an Inbox note every
        # time the job did its job. The record of what changed is the commit
        # the runner makes around this write, which names this job and is
        # revertable. Only being unable to check, or refusing an answer, is
        # worth waking someone for.
        job.check("Vault Map updated", ok=True, tier=IMPORTANT, owner=YOU,
                  covers="vault-map-accuracy",
                  detail=f"{len(changed)} line(s) rewritten to match the vault: "
                         f"{'; '.join(changed[:3])}"
                         f"{' and more' if len(changed) > 3 else ''}"
                         + (" (dry run, not written)" if job.dry_run else ""))
        return 0


def _guard(rewritten: str, original: str) -> None:
    """Refuse an answer that has clearly misunderstood the task.

    Raises AskFailed, so a refusal reports as unverified rather than as a
    clean week.
    """
    # _extract guarantees the answer starts at the file's first line, so the
    # old startswith check lives there now. What remains here are the two
    # judgements Python can make about content: size, and protected sections.
    if len(rewritten) < len(original) * MIN_KEEP_RATIO:
        raise AskFailed(f"answer is {len(rewritten)} chars against "
                        f"{len(original)} - that is a rewrite, not a correction")

    dropped = [h for h in PROTECTED if h in original and h not in rewritten]
    if dropped:
        raise AskFailed(f"answer dropped protected section(s): {', '.join(dropped)}")


def _undo_perturbations(original: str, rewritten: str) -> tuple[str, list[str]]:
    """Put back lines the model changed without meaning to.

    On a 2026-09-15 run it rewrote a correct line from "Backblaze B2" to
    "Backblaza B2" -- one letter, in a proper noun, on a line that needed no
    correction at all. The prompt already says to keep accurate lines "character
    for character", and the prompt was not enough; the same argument that moved
    the preamble check into Python applies here.

    This job writes straight to the live file, so a corruption like that lands
    silently and stays until a human happens to read that sentence. The commit
    is revertable, but only if someone notices, and the whole reason this job
    exists is that nobody notices drift in this file.

    A real correction and a perturbation look different. "the counselling
    process ... and the two branches of the decision" becoming "paused since 8
    Sep 2026 ..." shares little text. A typo shares almost all of it. So a
    replaced line that is still ~90% the same is suspect -- and it is only
    treated as a perturbation when every word that actually differs is itself a
    near-miss of the word it replaced and carries no digits.

    That last clause is what keeps date and count corrections working: those are
    exactly the small, high-similarity edits this job is for. "2026-08-28" ->
    "2026-09-15" differs in digits and is kept; "Backblaze" -> "Backblaza" is
    not, and is reverted.
    """
    a, b = original.splitlines(), rewritten.splitlines()
    out, reverted = list(b), []

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            continue
        for k in range(i2 - i1):
            old_line, new_line = a[i1 + k], b[j1 + k]
            if _is_perturbation(old_line, new_line):
                out[j1 + k] = old_line
                reverted.append(f"{_short(new_line, 60)} -> kept {_short(old_line, 60)}")

    return "\n".join(out) + "\n", reverted


def _is_perturbation(old_line: str, new_line: str) -> bool:
    if old_line == new_line:
        return False
    if difflib.SequenceMatcher(None, old_line, new_line).ratio() < 0.9:
        return False                      # a real rewrite, not a slip

    ow, nw = old_line.split(), new_line.split()
    sm = difflib.SequenceMatcher(None, ow, nw)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag != "replace" or (i2 - i1) != (j2 - j1):
            return False                  # words added or dropped: real content
        for k in range(i2 - i1):
            o, n = ow[i1 + k], nw[j1 + k]
            if any(c.isdigit() for c in o + n):
                return False              # a date or count: the job's actual purpose
            if difflib.SequenceMatcher(None, o, n).ratio() < 0.7:
                return False              # a different word, deliberately chosen
    return True

def _changed_lines(original: str, rewritten: str) -> list[str]:
    """Lines the model would change, as short one-line summaries.

    Trailing whitespace is normalised first: a reflowed space is not drift and
    should not put a failing check on the maintenance-status page.
    """
    a = [l.rstrip() for l in original.splitlines()]
    b = [l.rstrip() for l in rewritten.splitlines()]

    out = []
    for line in difflib.unified_diff(a, b, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line.startswith(("-", "+")) and line[1:].strip():
            out.append(_short(line))
    log(f"vaultmap: {len(out)} changed line(s)")
    return out


def _short(text: str, limit: int = 110) -> str:
    """The full text is in the proposal. This lands on the status page."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def _extract(answer: str, original: str) -> str:
    """Pull the file out of whatever the model wrapped it in.

    This job failed every run from 2026-09-02 to 09-14 -- three weeks, on a
    weekly timer -- and every failure was the same shape: a sentence of narration
    before the file.

        "Here is the corrected file, per the instruction to output raw content"
        "I verified this against the actual directory tree..."
        "Since the Edit tool is blocked in this mode, I'll output the corrected"

    None of those is a bad answer. The file came after them each time. The old
    guard demanded the response *begin* with the file and reported "unverified"
    otherwise, so a chatty sentence cost a week of verification -- and because a
    refusal looks exactly like a run that could not check, nobody could tell
    three weeks of narration from three weeks of a broken model.

    Telling the model not to narrate was already tried; the prompt has said "no
    preamble" since the first version and the 09-02 failure quotes it back. So
    narration is now allowed and discarded instead: the model fences the file in
    markers, and anything outside them is thrown away.

    Three ways in, most explicit first. Preamble can never reach the file by any
    of them, because each one starts the result at the file's own first line.
    """
    first = original.split("\n", 1)[0]

    # 1. the markers it was asked for
    if BEGIN in answer and END in answer:
        body = answer.split(BEGIN, 1)[1].rsplit(END, 1)[0]
        return _clean(body, first)

    # 2. no markers, but the file is in there somewhere
    if first in answer:
        return _clean(answer[answer.index(first):], first)

    # 3. nothing usable. Still a refusal, so an unusable answer reports as
    #    unverified rather than as a clean week.
    raise AskFailed(f"answer never contains {first!r}; it opens {answer[:80]!r}, "
                    f"so there is no file in it to use")


def _clean(body: str, first: str) -> str:
    """Trim fences and any trailing chat, and assert we start at the file."""
    lines = body.strip().split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]

    # Trailing commentary after the file's last real line. The map ends on a
    # content line, never on prose addressed to a reader of the answer, so
    # anything after a trailing blank line that reads like a sentence about the
    # work is dropped. Conservative on purpose: only the tail, only when it is
    # clearly not map content.
    while lines and not lines[-1].strip():
        lines.pop()

    out = "\n".join(lines).strip()
    if not out.startswith(first):
        raise AskFailed(f"extracted text does not start with {first!r}; it opens "
                        f"{out[:80]!r}")
    return out + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
