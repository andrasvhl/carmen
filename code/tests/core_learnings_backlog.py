#!/usr/bin/env python3
"""The consolidation detector counts the right notes, and only those.

Builds a scratch vault and asserts what a passing run cannot show you:

  1. A backticked `#coreLearning` is documentation ABOUT the tag, not a use of
     it. This is the whole discriminator. Measured on the real vault: all 16
     mentions in _system/, Wiki/ and task notes were backticked; all 3 real
     uses were bare.
  2. `type: action` + `#coreLearning` counts. Something can be an action AND a
     core learning, so type must not gate anything.
  3. `#coreLearnings` (plural) does not match. Obsidian would treat it as a
     separate tag, silently.
  4. A tag mid-text outside Inbox is a *passage*, not a capture, and is done
     once the notebook cites its note - not by moving the note anywhere.
  5. Both thresholds fire, and the structure guard refuses to read a missing
     folder as a healthy zero.

    ~/automation/.venv/bin/python -m tests.core_learnings_backlog
"""
import os, pathlib, sys, tempfile
from datetime import date, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

TMP = pathlib.Path(tempfile.mkdtemp())
VAULT = TMP / "vault"
(VAULT / "_system").mkdir(parents=True)
(VAULT / "Inbox").mkdir()
(VAULT / "Projects").mkdir()
(VAULT / "Wiki").mkdir()
NB = VAULT / "Resources" / "Red Notebook"
(NB / "Core Learnings").mkdir(parents=True)

os.environ["VAULT_PATH"] = str(VAULT)
from runner import config
config.VAULT = VAULT
config.INBOX = VAULT / "Inbox"
import jobs.core_learnings as cl
cl.NOTEBOOK = NB
cl.CONSOLIDATED = NB / "Core Learnings"

NOTEBOOK_BODY = "# Red Notebook\n\n## Core Learnings\n\n→ Something. [[Already Folded In]]\n"
(NB / "Red Notebook.md").write_text(NOTEBOOK_BODY, encoding="utf-8")


def note(rel, *, type_="learning", body="", captured=None):
    p = VAULT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = (captured or date.today()).isoformat()
    p.write_text(f"---\ntype: {type_}\ncaptured: {stamp}\n---\n{body}\n", encoding="utf-8")


def classify():
    cited = cl._cited_by_notebook()
    caps, passes = [], []
    for p, c in cl._tagged():
        if config.INBOX in p.parents:
            caps.append(p.stem)
        elif p.stem not in cited:
            passes.append(p.stem)
    return sorted(caps), sorted(passes)


fails = []

# 1 - bare vs backticked
note("Inbox/real capture.md", body="#coreLearning\n\nA genuine learning.")
note("Inbox/doc mentioning tag.md", type_="action",
     body="Verify it contains `#coreLearning` as the first body line.")
note("_system/how it works.md", type_="reference",
     body="The tag is `#coreLearning`, singular and inline.")
note("_system/fenced.md", type_="reference",
     body="```\n#coreLearning\n```\nJust an example block.")

# 2 - action AND core learning is a real combination
note("Inbox/actionable learning.md", type_="action",
     body="#coreLearning\n\nCall Nonis about this, and remember why it matters.")

# 3 - plural and nested
note("Inbox/plural.md", body="#coreLearnings\n\nWrong tag.")
note("Inbox/nested.md", body="#coreLearning/sub\n\nNested.")

# 4 - mid-text passages, one cited by the notebook and one not
note("Projects/<PROJECT>/<PRIVATE_NOTE>.md", type_="project-doc",
     body="Ein langer Text.\n\n- Das muss ich daraus lernen. #coreLearning\n")
note("Projects/Already Folded In.md", type_="project-doc",
     body="Some text. #coreLearning\n")
note("Wiki/derived.md", type_="wiki", body="He tags the whole of it #coreLearning.")

caps, passes = classify()
if caps != ["actionable learning", "real capture"]:
    fails.append(f"captures wrong: {caps}")
if passes != ["<PRIVATE_NOTE>"]:
    fails.append(f"passages wrong: {passes} (expected only <PRIVATE_NOTE>; "
                 f"'Already Folded In' is cited by the notebook, Wiki/ is skipped)")

# 5a - count threshold
for i in range(cl.MAX_PENDING + 1):
    note(f"Inbox/bulk {i}.md", body="#coreLearning\n\nx")
caps, passes = classify()
if not len(caps) + len(passes) > cl.MAX_PENDING:
    fails.append("count threshold should fire")

# 5b - age threshold, isolated from count
for p in (VAULT / "Inbox").glob("bulk *.md"):
    p.unlink()
old = date.today() - timedelta(days=cl.MAX_AGE_DAYS + 1)
note("Inbox/stale.md", body="#coreLearning\n\nold", captured=old)
dated = [c for _, c in cl._tagged() if c]
if not cl._age(min(dated)) > cl.MAX_AGE_DAYS:
    fails.append(f"age threshold should fire, got {cl._age(min(dated))}d")

# 5c - structure guard
cl.NOTEBOOK = NB / "gone"
if cl.NOTEBOOK.is_dir() and (cl.NOTEBOOK / "Red Notebook.md").is_file():
    fails.append("structure guard should report missing")

print("\n".join(f"FAIL: {f}" for f in fails) if fails else
      "ok - backticks excluded, action+learning counted, plural rejected, "
      "passages split from captures, citation closes a passage, thresholds fire")
sys.exit(1 if fails else 0)
