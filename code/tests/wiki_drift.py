#!/usr/bin/env python3
"""The wiki drift check must fail loudly, and must never pass vacuously.

    ~/automation/.venv/bin/python -m tests.wiki_drift

The assertion that matters is the last one. Every other failure mode here is
visible - a drifted source names the articles it feeds, a deleted source says
so. But if `_sources.md` stops parsing, a naive implementation reports "all
sources unchanged", which is true of an empty set and indistinguishable from a
healthy wiki. That is the same shape as the expired Canvas token and the
`Connected` message on blanked tokens: the failure looks exactly like nothing
happening. The parse has to assert itself before anything depends on it.
"""
import hashlib
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from runner import config

TMP = pathlib.Path(tempfile.mkdtemp())
VAULT = TMP / "vault"
for d in ("_system", "Wiki", "Inbox", "Clippings"):
    (VAULT / d).mkdir(parents=True)

config.VAULT = VAULT
config.INBOX = VAULT / "Inbox"
from jobs import wiki as W
W.WIKI = VAULT / "Wiki"
W.SOURCES = VAULT / "Wiki" / "_sources.md"

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def h(rel):
    return hashlib.sha256((VAULT / rel).read_bytes()).hexdigest()[:12]


def write_state(rows, rejected):
    body = ["---", "type: reference", "---", "# Wiki sources — state", "",
            "| Source | Hash | Ingested | Articles fed |", "|---|---|---|---|"]
    body += [f"| `{p}` | `{hh}` | 2026-09-02 | {a} |" for p, hh, a in rows]
    body += ["", W.REJECTED_HEADING, "",
             "| Source | Hash | Judged | Why not |", "|---|---|---|---|"]
    body += [f"| `{p}` | `{hh}` | 2026-09-02 | judged |" for p, hh in rejected]
    W.SOURCES.write_text("\n".join(body) + "\n")


(VAULT / "Inbox/moved.md").write_text("original\n")
(VAULT / "Inbox/stable.md").write_text("stable\n")
(VAULT / "Inbox/skipped.md").write_text('---\ntype: learning\n---\nnope\n')
write_state(
    [("Inbox/moved.md", h("Inbox/moved.md"), "article-a, article-b"),
     ("Inbox/stable.md", h("Inbox/stable.md"), "article-c"),
     ("Inbox/gone.md", "aaaaaaaaaaaa", "article-d")],
    [("Inbox/skipped.md", h("Inbox/skipped.md"))],
)

ingested, rejected = W._registered()
check("both tables parse separately", len(ingested) == 3 and len(rejected) == 1,
      f"{len(ingested)}/{len(rejected)}")
check("a MISSING hash still yields a row", "Inbox/gone.md" in ingested)

drifted = [p for p, hh in ingested.items()
           if (VAULT / p).is_file() and W._hash(VAULT / p) != hh]
check("nothing drifts before anything changes", drifted == [], str(drifted))

(VAULT / "Inbox/moved.md").write_text("original\nappended later\n")
drifted = [p for p, hh in ingested.items()
           if (VAULT / p).is_file() and W._hash(VAULT / p) != hh]
check("an edited source drifts", drifted == ["Inbox/moved.md"], str(drifted))
check("the report names the articles that source fed",
      "article-a, article-b" in W._drift_detail([("Inbox/moved.md", "")], W._articles(), []))

gone = [p for p in ingested if not (VAULT / p).is_file()]
check("a deleted source is reported", gone == ["Inbox/gone.md"], str(gone))

# A rejected note that grows is worth reconsidering, not permanently skipped.
(VAULT / "Inbox/skipped.md").write_text('---\ntype: learning\n---\n' + "much more substance\n" * 40)
recon = [p for p, hh in rejected.items() if W._hash(VAULT / p) != hh]
check("a previously skipped source that grew is flagged", recon == ["Inbox/skipped.md"], str(recon))

check("a judged note is not reported as a new candidate",
      W._candidates() - set({**ingested, **rejected}) == set(),
      str(W._candidates() - set({**ingested, **rejected})))
(VAULT / "Inbox/fresh.md").write_text('---\ntype: learning\n---\nnew\n')
check("a genuinely new note is a candidate",
      W._candidates() - set({**ingested, **rejected}) == {"Inbox/fresh.md"})

W.SOURCES.write_text("# Wiki sources\n\nthe table format changed entirely\n")
empty_ingested, _ = W._registered()
check("THE ONE THAT MATTERS: an unparseable state file yields no rows, so the job "
      "must refuse to report health rather than pass vacuously",
      empty_ingested == {}, str(empty_ingested))

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
