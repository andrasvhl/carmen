#!/usr/bin/env python3
"""The distillation guard refuses every answer shape that has actually gone wrong.

Structural checks are all Python can make about a distillation - there is no
ground truth to diff a judgement about meaning against. So each one has to hold.

    ~/automation/.venv/bin/python -m tests.red_notebook_guard
"""
import pathlib, sys, tempfile
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from runner.ask import AskFailed
import jobs.red_notebook as rn

TMP = pathlib.Path(tempfile.mkdtemp())
EN = TMP / "An english note.md"
EN.write_text("---\ntype: learning\n---\n#coreLearning\n\nI keep trying to do this and it "
              "never works for me, the thing that works is a block.\n", encoding="utf-8")
DE = TMP / "Eine deutsche Notiz.md"
DE.write_text("---\ntype: learning\n---\n#coreLearning\n\nIch versuche das immer wieder und "
              "es funktioniert nicht, was wirklich hilft ist ein Block.\n", encoding="utf-8")

PEND_1 = [(EN, date(2026, 9, 11))]
PEND_2 = [(EN, date(2026, 9, 11)), (DE, date(2026, 9, 11))]

GOOD_1 = ("→ Deep work needs one protected block, not the gaps between meetings.\n"
          "*2026-09-11 · [[An english note|full note]]*")


def refuses(entries, pending, why):
    try:
        rn._guard(entries, pending)
    except AskFailed:
        return True
    fails.append(f"should have refused: {why}")
    return False


fails = []

# the happy path must actually pass, or every refusal below proves nothing
try:
    rn._guard(GOOD_1, PEND_1)
except AskFailed as e:
    fails.append(f"rejected a good answer: {e}")

refuses("Here is the entry:\n\n" + GOOD_1, PEND_1, "preamble before the arrow")
refuses("", PEND_1, "empty answer")
refuses(GOOD_1 + "\n\n" + GOOD_1, PEND_1, "two entries for one note")
refuses(GOOD_1, PEND_2, "one entry for two notes")
refuses("→ " + ("word " * 140) + "\n*2026-09-11 · [[An english note|full note]]*",
        PEND_1, "source note pasted in rather than distilled")
refuses("→ A fine entry.\n*2026-09-11 · [[Some other note|full note]]*",
        PEND_1, "links a note that was not pending")

# the one that actually happened on 2026-09-11
refuses("→ Ich versuche immer wieder, in den Lücken zwischen Meetings zu arbeiten, und es "
        "funktioniert nicht für mich.\n*2026-09-11 · [[An english note|full note]]*",
        PEND_1, "English note answered in German")

# a short German capture with none of the original stopwords (2026-10-05)
for text, want in (("10k laufen hat meinen kater so viel besser gemacht", "German"),
                   ("Kater weg, Läufe helfen", "German"),
                   ("Double down on what Stanford is great at", "English")):
    if rn._language(text) != want:
        fails.append(f"_language({text!r}) is not {want}")

# _insert must not disturb one byte outside the inserted block
original = ("# Red Notebook\n\n## Digital entries\n\n→ existing entry\n"
            + rn.MECHANICS + "\n\n**Page order.** unchanged.\n")
out = rn._insert(original, GOOD_1)
if "→ existing entry" not in out:
    fails.append("_insert dropped an existing entry")
if not out.endswith("**Page order.** unchanged.\n"):
    fails.append("_insert disturbed the mechanics section")
if out.index(GOOD_1.splitlines()[0]) > out.index("## How this note works"):
    fails.append("_insert put the entry after the mechanics section")
if original.replace("\n", "") not in out.replace(GOOD_1, "").replace("\n", ""):
    fails.append("_insert changed existing content")

print("\n".join(f"FAIL: {f}" for f in fails) if fails else
      "ok - preamble, empty, miscount, over-length, wrong link and language switch all refused; "
      "insert is append-only")
sys.exit(1 if fails else 0)
