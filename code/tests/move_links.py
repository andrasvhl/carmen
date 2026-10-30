#!/usr/bin/env python3
r"""`vt move` must rewrite every link shape this vault actually uses.

    ~/automation/.venv/bin/python -m tests.move_links

The vault carries ~5,900 path-qualified links against ~5,700 bare ones. A bare
[[Note]] survives any move because Obsidian resolves it by name; a
path-qualified one breaks the moment a folder changes. Obsidian rewrites them
itself, but only for moves made inside Obsidian -- and most moves in this vault
are made by an agent or a shell, which bypass it entirely.

The escaped-pipe case is not exotic: `[[path\|alias]]` is how Obsidian writes a
link inside a Markdown table, and the profile and people notes are full of
tables. Missing it would silently break exactly the notes that link out most.

The last two assertions are the ones that matter most. Wiki/ is derived, never
authored, so this tool must never edit it -- and it must refuse to move a file
registered in Wiki/_sources.md at all, because that registration carries a
content hash and only a re-ingest can restore it.
"""
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = pathlib.Path(tempfile.mkdtemp())
VAULT = TMP / "vault"
(VAULT / "_system").mkdir(parents=True)
(VAULT / "Projects" / "Old").mkdir(parents=True)
(VAULT / "Archive").mkdir(parents=True)
(VAULT / "Wiki").mkdir(parents=True)

fails = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
    if not ok:
        fails.append(label)


def vt(*args):
    return subprocess.run([sys.executable, "-m", "tools.move", *args],
                          cwd=ROOT, capture_output=True, text=True,
                          env={"VAULT_PATH": str(VAULT), "PATH": "/usr/bin:/bin",
                               "HOME": str(TMP)})


# --- the file being moved, and every way the vault links to it -------------
(VAULT / "Projects" / "Old" / "Note.md").write_text("# Note\n")
(VAULT / "linker.md").write_text(
    "plain: [[Projects/Old/Note]]\n"
    "alias: [[Projects/Old/Note|Note]]\n"
    "table: | x | [[Projects/Old/Note\\|Note]] | y |\n"
    "head:  [[Projects/Old/Note#Section]]\n"
    "block: [[Projects/Old/Note#^abc123]]\n"
    "embed: ![[Projects/Old/Note]]\n"
    "md:    [text](Projects/Old/Note.md)\n"
    "bare:  [[Note]]\n"
    "other: [[Projects/Old/NoteOther]]\n"
    "prose: see `Projects/Old/Note.md` for detail\n"
    "dir:   lives under `Projects/Old/`\n"
)
(VAULT / "Wiki" / "article.md").write_text("wiki cites [[Projects/Old/Note|Note]]\n")

r = vt("Projects/Old/Note.md", "Archive/Note.md")
check("move succeeds", r.returncode == 0, r.stderr.strip()[:160])

text = (VAULT / "linker.md").read_text()
check("plain link rewritten", "[[Archive/Note]]" in text)
check("aliased link rewritten", "[[Archive/Note|Note]]" in text)
check("escaped-pipe table link rewritten", "[[Archive/Note\\|Note]]" in text)
check("heading link rewritten", "[[Archive/Note#Section]]" in text)
check("block link rewritten", "[[Archive/Note#^abc123]]" in text)
check("embed rewritten", "![[Archive/Note]]" in text)
check("markdown link rewritten", "[text](Archive/Note.md)" in text)
check("bare link left alone (name unchanged, resolves anyway)", "[[Note]]" in text)
check("a longer name sharing the prefix is NOT touched",
      "[[Projects/Old/NoteOther]]" in text, text)
check("backticked prose path rewritten", "`Archive/Note.md`" in text, text)
check("no stale path remains", "Projects/Old/Note]]" not in text)
check("the file actually moved", (VAULT / "Archive" / "Note.md").is_file())
check("the emptied folder was pruned", not (VAULT / "Projects" / "Old").exists())

check("Wiki/ was not edited",
      (VAULT / "Wiki" / "article.md").read_text() == "wiki cites [[Projects/Old/Note|Note]]\n")

# --- a moved folder: internal links and deep prose paths -------------------
# Both of these were live bugs. Moving <COACHING_PROJECT>/ rewrote all 35 inbound links
# and left 2 links *inside* the moved folder pointing at the old path, plus a
# backticked path one level deeper than the folder itself.
(VAULT / "Src" / "sub").mkdir(parents=True)
(VAULT / "Src" / "a.md").write_text("sibling: [[Src/sub/b]]\n")
(VAULT / "Src" / "sub" / "b.md").write_text("# B\n")
(VAULT / "outside.md").write_text("deep: `Src/sub/b.md` and folder `Src/`\n")

r = vt("Src", "Dst")
check("folder move succeeds", r.returncode == 0, r.stderr.strip()[:160])
moved = (VAULT / "Dst" / "a.md").read_text()
check("a link INSIDE the moved folder is rewritten",
      "[[Dst/sub/b]]" in moved, moved.strip())
out = (VAULT / "outside.md").read_text()
check("a backticked path deeper than the moved folder is rewritten",
      "`Dst/sub/b.md`" in out, out.strip())
check("the folder's own backticked path is rewritten", "`Dst/`" in out, out.strip())

# --- wikilinks that spell out `.md` ---------------------------------------
# Every file-note link the Canvas archive writes looks like this
# (`[[.../Materials/deck.pdf.md|Deck]]`, `.md` kept because the name already
# has an extension), and so do links typed with the extension. The pattern
# required the delimiter straight after the path, so all of these were left
# pointing at the old folder: 14 archive notes after the 2026-09-23 regroup.
(VAULT / "Course" / "Materials").mkdir(parents=True)
(VAULT / "Course" / "Materials" / "deck.pdf.md").write_text("# deck\n")
(VAULT / "Course" / "Materials" / "deck.pdf.md-extra.md").write_text("# other\n")
(VAULT / "Course" / "Modules.md").write_text(
    "file:  [[Course/Materials/deck.pdf.md|Deck]]\n"
    "table: | [[Course/Materials/deck.pdf.md\\|Deck]] |\n"
    "head:  [[Course/Materials/deck.pdf.md#Page 2]]\n"
    "embed: ![[Course/Materials/deck.pdf.md]]\n"
    "typed: [[Course/Modules.md]]\n"
    "near:  [[Course/Materials/deck.pdf.md-extra]]\n")
r = vt("Course", "Fall 2026/Course")
check("folder move with .md links succeeds", r.returncode == 0, r.stderr.strip()[:160])
mods = (VAULT / "Fall 2026" / "Course" / "Modules.md").read_text()
check("file-note link with .md rewritten", "[[Fall 2026/Course/Materials/deck.pdf.md|Deck]]" in mods, mods)
check("escaped-pipe .md link rewritten",
      "[[Fall 2026/Course/Materials/deck.pdf.md\\|Deck]]" in mods, mods)
check("heading .md link rewritten", "[[Fall 2026/Course/Materials/deck.pdf.md#Page 2]]" in mods, mods)
check("embedded .md link rewritten", "![[Fall 2026/Course/Materials/deck.pdf.md]]" in mods, mods)
check("typed .md link to a note rewritten", "[[Fall 2026/Course/Modules.md]]" in mods, mods)
check("the extension is kept, not dropped", "deck.pdf|" not in mods, mods)
check("a longer name sharing the .md prefix is rewritten as ITSELF",
      "[[Fall 2026/Course/Materials/deck.pdf.md-extra]]" in mods, mods)
check("no link points at the old folder", "[[Course/" not in mods, mods)

# --- a registered wiki source must be refused ------------------------------
(VAULT / "Projects").mkdir(exist_ok=True)
(VAULT / "Projects" / "Src.md").write_text("# Src\n")
(VAULT / "Wiki" / "_sources.md").write_text(
    "| `Projects/Src.md` | `abc123` | 2026-09-15 | some-article |\n")
r = vt("Projects/Src.md", "Archive/Src.md")
check("refuses to move a registered wiki source", r.returncode == 3, f"rc={r.returncode}")
check("...and says why", "re-ingest" in r.stderr.lower(), r.stderr.strip()[:120])
check("...and did not move it", (VAULT / "Projects" / "Src.md").is_file())

r = vt("Projects/Src.md", "Archive/Src.md", "--force")
check("--force proceeds", r.returncode == 0, r.stderr.strip()[:120])
check("...and names what to re-ingest", "RE-INGEST REQUIRED" in r.stdout, r.stdout[-160:])

# --- dry run changes nothing ----------------------------------------------
(VAULT / "Projects").mkdir(exist_ok=True)
(VAULT / "Projects" / "Dry.md").write_text("# Dry\n")
(VAULT / "dl.md").write_text("[[Projects/Dry]]\n")
r = vt("Projects/Dry.md", "Archive/Dry.md", "--dry-run")
check("dry run reports the rewrite", "1 link" in r.stdout, r.stdout.strip()[:120])
check("dry run moved nothing", (VAULT / "Projects" / "Dry.md").is_file())
check("dry run rewrote nothing", (VAULT / "dl.md").read_text() == "[[Projects/Dry]]\n")

# --- bookmarks follow the move -------------------------------------------
import json
(VAULT / ".obsidian").mkdir(exist_ok=True)
(VAULT / "Projects" / "BmDir" / "Sub").mkdir(parents=True)
(VAULT / "Projects" / "BmDir" / "Sub" / "Deep.md").write_text("# Deep\n")
(VAULT / "Projects" / "Bm.md").write_text("# Bm\n")
(VAULT / ".obsidian" / "bookmarks.json").write_text(json.dumps({"items": [
    {"type": "file", "path": "Projects/Bm.md", "title": "Bm"},
    {"type": "group", "title": "G", "items": [
        {"type": "folder", "path": "Projects/BmDir/Sub", "title": "Sub"},
        {"type": "folder", "path": "Projects/BmDirOther", "title": "Other"}]}]}))


def marks():
    t = json.loads((VAULT / ".obsidian" / "bookmarks.json").read_text())
    return [t["items"][0]["path"]] + [i["path"] for i in t["items"][1]["items"]]


r = vt("Projects/Bm.md", "Archive/Bm.md", "--dry-run")
check("dry run reports the bookmark", "bookmark  Projects/Bm.md" in r.stdout, r.stdout[:200])
check("dry run leaves bookmarks alone", marks()[0] == "Projects/Bm.md")
vt("Projects/Bm.md", "Archive/Bm.md")
check("file bookmark follows the file", marks()[0] == "Archive/Bm.md", str(marks()))
vt("Projects/BmDir", "Archive/BmDir")
check("subfolder bookmark inside a group follows its folder",
      marks()[1] == "Archive/BmDir/Sub", str(marks()))
check("a shared prefix is not a match", marks()[2] == "Projects/BmDirOther", str(marks()))

print()
if fails:
    print(f"FAILED: {len(fails)} check(s): {', '.join(fails)}")
    sys.exit(1)
print("all checks passed")
