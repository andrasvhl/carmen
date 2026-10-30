#!/usr/bin/env python3
"""Module aliasing: a rename must relocate, never duplicate and never delete.

    ~/automation/.venv/bin/python -m tests.canvas_alias

The assertion that matters is the destructive one. State keys items by Canvas id
but stores an absolute path, so a folder renamed behind the tool's back makes the
next pull rewrite the note at the original path and `--prune` delete the renamed
tree. Going through the alias map instead has to move the folders, move the
folder note in lockstep, fix every stored path, and leave nothing behind for
prune to reap.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import archive as A

TMP = pathlib.Path(tempfile.mkdtemp())
VAULT = TMP / "vault"
ROOT = VAULT / "Areas/MBA/Course Archive"
FILES = ROOT / "_files"
A.VAULT = VAULT
A.ROOT = ROOT
A.FILES_ROOT = FILES

COURSE = "Group Dynamics - F25-OB-779"
RAW = "Session 1: Opening Session - Monday, September 22"
DERIVED = "1 - Opening Session - Monday, September 22"
ALIAS = "1 - Opening Session"

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        fails.append(name)


def build(alias=None):
    """A course with one module already on disk under its derived name."""
    for p in (ROOT, FILES):
        if p.exists():
            import shutil; shutil.rmtree(p)
    base = ROOT / COURSE / DERIVED
    (base / "Materials").mkdir(parents=True)
    (base / f"{DERIVED}.md").write_text("# the session's own page\n", encoding="utf-8")
    (base / "Materials" / "Deck.pdf.md").write_text("# Deck\n\ntext\n", encoding="utf-8")
    fdir = FILES / COURSE / DERIVED
    fdir.mkdir(parents=True)
    (fdir / "Deck.pdf").write_bytes(b"%PDF-1.4 fake")
    rec = {
        "name": "OB", "folder": COURSE, "sections": {},
        "module_dirs": {RAW: DERIVED},
        "items": {"page:intro": {"path": str(base / f"{DERIVED}.md"), "hash": "x"},
                  "file:1": {"path": str(base / "Materials" / "Deck.pdf.md"), "hash": "y"}},
        "files": {"1": {"path": str(fdir / "Deck.pdf"), "module": RAW,
                        "note": f"Areas/MBA/Course Archive/{COURSE}/{DERIVED}/Materials/Deck.pdf.md"}},
    }
    if alias:
        rec["module_aliases"] = {RAW: alias}
    return rec


READING_URL = "https://hbsp.harvard.edu/tu/abc12345"

def reading_state(rec):
    """Full state, including a reading note recorded under the old folder."""
    return {"courses": {"1": rec},
            "readings": {"k": {"url": READING_URL, "title": "A Reading",
                               "canvas_fetch_status": "login-required",
                               "notes": {COURSE: f"Areas/MBA/Course Archive/{COURSE}/"
                                                 f"{DERIVED}/Readings/A Reading"}}}}


class FakePuller(A.Puller):
    def __init__(self, rec, data=None):
        self.problems = []
        self.verbose = False
        self.dry_run = False
        self.aliases = rec.get("module_aliases") or {}
        self.data = data if data is not None else reading_state(rec)


# --- naming ---------------------------------------------------------------
check("no alias falls back to the stripped name",
      A.module_dir(RAW, {}) == DERIVED, A.module_dir(RAW, {}))
check("an alias wins outright",
      A.module_dir(RAW, {RAW: ALIAS}) == ALIAS, A.module_dir(RAW, {RAW: ALIAS}))
check("aliases is a required argument, so a missed call site is a TypeError",
      _typeerror := (lambda: [False for _ in [A.module_dir(RAW)]])() if False else True)
try:
    A.module_dir(RAW)          # type: ignore[call-arg]
    check("module_dir refuses to run without an alias map", False)
except TypeError:
    check("module_dir refuses to run without an alias map", True)

# --- the relocation -------------------------------------------------------
rec = build(ALIAS)
data = reading_state(rec)
p = FakePuller(rec, data)
p._relocate_modules(ROOT / COURSE, COURSE, rec, [RAW])

new_base = ROOT / COURSE / ALIAS
check("the notes folder moved", new_base.is_dir() and not (ROOT / COURSE / DERIVED).exists(),
      str(sorted(x.name for x in (ROOT / COURSE).iterdir())))
check("the binaries folder moved",
      (FILES / COURSE / ALIAS).is_dir() and not (FILES / COURSE / DERIVED).exists())
check("the folder note renamed in lockstep",
      (new_base / f"{ALIAS}.md").exists() and not (new_base / f"{DERIVED}.md").exists(),
      str(sorted(x.name for x in new_base.iterdir())))
check("the session's own page kept its content",
      "the session's own page" in (new_base / f"{ALIAS}.md").read_text())
check("nested notes came with it", (new_base / "Materials" / "Deck.pdf.md").exists())
check("the binary kept its bytes",
      (FILES / COURSE / ALIAS / "Deck.pdf").read_bytes() == b"%PDF-1.4 fake")

# Every stored path must now point at the new tree, or the next pull rewrites
# the note at the old path and --prune deletes the relocated one.
paths = [e["path"] for e in rec["items"].values()] + [e["path"] for e in rec["files"].values()]
check("no stored path still points at the old folder",
      not any(DERIVED in x for x in paths), str([x for x in paths if DERIVED in x]))
check("every stored path points at the new folder", all(ALIAS in x for x in paths))
check("the vault-relative note path was fixed too",
      rec["files"]["1"]["note"] == f"Areas/MBA/Course Archive/{COURSE}/{ALIAS}/Materials/Deck.pdf.md",
      rec["files"]["1"]["note"])
check("module_dirs records the new folder", rec["module_dirs"][RAW] == ALIAS)
# The links pass seeds itself from these. Left stale, it writes every reading
# back into the old folder and recreates it - two folders per module again.
check("the reading note's recorded path moved too",
      data["readings"]["k"]["notes"][COURSE]
      == f"Areas/MBA/Course Archive/{COURSE}/{ALIAS}/Readings/A Reading",
      data["readings"]["k"]["notes"][COURSE])
check("no problems reported", not p.problems, str(p.problems))

# --- nothing left for --prune to reap -------------------------------------
seen = {str(new_base / f"{ALIAS}.md"), str(new_base / "Materials" / "Deck.pdf.md"),
        str(FILES / COURSE / ALIAS / "Deck.pdf")}
known = ({e["path"] for e in rec["items"].values()}
         | {e["path"] for e in rec["files"].values()})
gone = A.prune(ROOT / COURSE, FILES / COURSE, seen, known, dry_run=True)
check("--prune finds nothing to delete after a relocation", not gone, str(gone))

# The whole point of the inversion: a file the tool never wrote is unreachable,
# whatever it is called and wherever it sits.
mine = new_base / "Mine"
mine.mkdir(parents=True, exist_ok=True)
(mine / "my cheat sheet.pdf").write_bytes(b"irreplaceable")
(new_base / "loose note.md").write_text("mine too", encoding="utf-8")
gone = A.prune(ROOT / COURSE, FILES / COURSE, seen, known)
check("prune cannot touch a file the tool never wrote",
      (mine / "my cheat sheet.pdf").exists() and (new_base / "loose note.md").exists(),
      str(gone))
check("an empty folder of his is left alone", mine.is_dir())

# And it still removes the tool's own leftovers.
stale = new_base / "Materials" / "Deck.pdf.md"
known2 = known | {str(stale)}
gone = A.prune(ROOT / COURSE, FILES / COURSE, set(), known2)
check("prune still removes what the tool wrote and no longer vouches for",
      str(stale) in gone and not stale.exists(), str(gone))

# --- re-running is a no-op ------------------------------------------------
before = sorted(str(x) for x in (ROOT / COURSE).rglob("*"))
p2 = FakePuller(rec)
p2._relocate_modules(ROOT / COURSE, COURSE, rec, [RAW])
after = sorted(str(x) for x in (ROOT / COURSE).rglob("*"))
check("a second relocation changes nothing", before == after)

# --- first sighting must not move anything --------------------------------
rec3 = build(ALIAS)
del rec3["module_dirs"]
p3 = FakePuller(rec3)
p3._relocate_modules(ROOT / COURSE, COURSE, rec3, [RAW])
check("a module never seen before is recorded, not relocated",
      rec3["module_dirs"][RAW] == ALIAS and (ROOT / COURSE / DERIVED).is_dir(),
      str(sorted(x.name for x in (ROOT / COURSE).iterdir())))

# --- a collision is refused, not merged -----------------------------------
rec4 = build(ALIAS)
(ROOT / COURSE / ALIAS).mkdir(parents=True)
p4 = FakePuller(rec4)
p4._relocate_modules(ROOT / COURSE, COURSE, rec4, [RAW])
check("two modules aliased onto one folder is refused",
      p4.problems and "already exists" in p4.problems[0], str(p4.problems))
check("the original folder is untouched after a refusal",
      (ROOT / COURSE / DERIVED / f"{DERIVED}.md").exists())

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
