#!/usr/bin/env python3
r"""Move or rename a note and rewrite every link that pointed at it.

    ~/automation/bin/vt move "Projects/Old/Note.md" "Archive/New/Note.md"
    ~/automation/bin/vt move "Projects/Old" "Archive/Old"      # whole folder
    ~/automation/bin/vt move A.md B.md --dry-run               # show, change nothing

## Why this exists

Obsidian rewrites inbound links when you move a file, and `alwaysUpdateLinks`
is already on in this vault. But it only fires for moves made *inside
Obsidian*. Every move made by Claude, by Codex, by `mv` in a terminal, by a git
operation on vault-host, or by a sync from the other machine bypasses it
entirely -- and in this vault most moves are agent-driven, which is why links
kept breaking.

The vault also carries ~5,900 path-qualified links (`[[Projects/X/Note|Note]]`)
against ~5,700 bare ones. A bare `[[Note]]` survives any move because Obsidian
resolves it by name; a path-qualified one breaks the moment a folder changes.
Only the path-qualified ones need rewriting, and those are exactly what this
walks.

## What it rewrites

  [[path]]  [[path|alias]]  [[path#heading]]  [[path#^block]]  ![[path]]
  [](path)  and bare-name links when the basename itself changes

Escaped pipes inside table cells (`[[path\|alias]]`) are handled: they are how
Obsidian writes a link inside a Markdown table, and they are common here.

## What it refuses

A source registered in `Wiki/_sources.md`. `Wiki/` is derived, never authored,
and `_sources.md` holds a content hash per registered source; moving one
silently breaks the wiki's provenance and only a re-ingest can repair it. That
is not a thing to do by accident, so it stops and says so. `--force` proceeds
and tells you what to re-ingest.

Nothing inside `Wiki/` is ever edited by this tool, for the same reason.

## Bookmarks

`.obsidian/bookmarks.json` stores bookmarks by path, and Obsidian hides a
bookmark whose target is gone rather than flagging it. Three of five
bookmarks went dark that way after the <COACHING_PROJECT> -> <PROJECT> move and nobody
noticed until the sidebar was rebuilt around them. So a move also rewrites
any bookmark on a moved file, the moved folder, or anything beneath it.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runner import config

# Folders whose contents are not ours to rewrite.
#   Wiki/   derived, never authored -- see _system/schema.md
#   Notion/ the frozen import; immutable by the same schema
SKIP_DIRS = {"Wiki", "Notion", ".obsidian", ".git", ".claude", ".trash"}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="vt move",
        description="Move a note or folder and rewrite inbound links.")
    ap.add_argument("src", help="path relative to the vault root")
    ap.add_argument("dst", help="path relative to the vault root")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change, touch nothing")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if the source is a registered wiki source")
    ap.add_argument("--no-git", action="store_true",
                    help="plain move instead of `git mv` even inside a repo")
    args = ap.parse_args()

    vault = config.VAULT
    src, dst = (vault / args.src), (vault / args.dst)

    if not src.exists():
        print(f"vt move: {args.src} does not exist", file=sys.stderr)
        return 2
    if dst.exists() and dst.is_file():
        print(f"vt move: {args.dst} already exists", file=sys.stderr)
        return 2

    # A folder destination means "move into it", the way `mv` behaves.
    if dst.is_dir() and src.is_file():
        dst = dst / src.name
        if dst.exists():
            print(f"vt move: {dst.relative_to(vault)} already exists", file=sys.stderr)
            return 2

    pairs = _pairs(vault, src, dst)

    blocked = _wiki_registered(vault, pairs)
    if blocked and not args.force:
        print("vt move: refusing — these are registered wiki sources:\n", file=sys.stderr)
        for p in blocked:
            print(f"    {p}", file=sys.stderr)
        print("\nWiki/_sources.md holds a content hash per source and is generated, never\n"
              "hand-edited, so moving one breaks the wiki's provenance and only a re-ingest\n"
              "can repair it. Re-ingest first, or pass --force and re-ingest after.",
              file=sys.stderr)
        return 3

    # A folder move also renames the folder itself, and docs refer to folders
    # directly (`Archive/Project/`). Without this, a folder move fixes every
    # file reference and leaves every folder reference wrong.
    folder = None
    if src.is_dir():
        folder = (src.relative_to(vault).as_posix(), dst.relative_to(vault).as_posix())
    edits = _plan_edits(vault, pairs, folder)
    bookmarks, marks = _plan_bookmarks(vault, pairs, folder)

    print(f"{'Would move' if args.dry_run else 'Moving'} "
          f"{len(pairs)} file(s), rewriting {sum(len(v) for v in edits.values())} "
          f"link(s) across {len(edits)} note(s)"
          + (f" and {len(marks)} bookmark(s)." if marks else "."))
    for old, new in marks:
        print(f"    bookmark  {old}  ->  {new}")
    for old, new in pairs[:10]:
        print(f"    {old}  ->  {new}")
    if len(pairs) > 10:
        print(f"    ... and {len(pairs) - 10} more")
    for f, subs in list(edits.items())[:10]:
        print(f"    {f}  ({len(subs)} link{'s' if len(subs) != 1 else ''})")
    if len(edits) > 10:
        print(f"    ... and {len(edits) - 10} more note(s)")

    if args.dry_run:
        if blocked:
            print(f"\n(--force would be required: {len(blocked)} registered wiki source(s))")
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_git and _in_git(vault):
        subprocess.run(["git", "-C", str(vault), "mv", str(src), str(dst)], check=True)
    else:
        src.rename(dst)

    for f, subs in edits.items():
        p = vault / f
        text = p.read_text(encoding="utf-8")
        for old, new in subs:
            text = text.replace(old, new)
        p.write_text(text, encoding="utf-8")

    if marks:
        (vault / BOOKMARKS).write_text(
            json.dumps(bookmarks, indent=2, ensure_ascii=False), encoding="utf-8")

    _prune_empty(src.parent, vault)

    print(f"\nMoved. Rewrote {sum(len(v) for v in edits.values())} link(s).")
    if blocked:
        print("\nRE-INGEST REQUIRED — these were registered wiki sources and Wiki/ was not "
              "touched:")
        for p in blocked:
            print(f"    {p}")
    return 0


def _pairs(vault, src, dst):
    """Every (old_rel, new_rel) this move produces. One entry for a file."""
    if src.is_file():
        return [(src.relative_to(vault).as_posix(), dst.relative_to(vault).as_posix())]
    out = []
    for f in sorted(src.rglob("*")):
        if f.is_file():
            out.append((f.relative_to(vault).as_posix(),
                        (dst / f.relative_to(src)).relative_to(vault).as_posix()))
    return out


def _wiki_registered(vault, pairs):
    sources = vault / "Wiki" / "_sources.md"
    if not sources.is_file():
        return []
    text = sources.read_text(encoding="utf-8", errors="ignore")
    return [old for old, _ in pairs if old in text]


def _plan_edits(vault, pairs, folder=None):
    """Which notes need which literal substitutions.

    Substitutions are literal string pairs rather than regexes so the write step
    cannot reinterpret anything it was not shown at plan time -- what --dry-run
    prints is exactly what runs.
    """
    targets = {}
    for old, new in pairs:
        o, n = old[:-3] if old.endswith(".md") else old, new[:-3] if new.endswith(".md") else new
        targets[o] = n
        # A rename also moves the bare name, which bare links resolve by.
        if Path(o).name != Path(n).name:
            targets[Path(o).name] = Path(n).name
    if folder:
        targets[folder[0]] = folder[1]

    # A moved file's own links need rewriting too. Skipping them (they are, after
    # all, "the file being moved") left every sibling link inside a moved folder
    # pointing at the old path: moving <COACHING_PROJECT>/ fixed all 35 inbound links and
    # broke 2 internal ones. Moved files are planned here and applied at their
    # NEW path, since the move happens first.
    moved_from = {old: new_ for old, new_ in pairs}
    edits = {}
    for f in _notes(vault):
        rel = f.relative_to(vault).as_posix()
        text = f.read_text(encoding="utf-8", errors="ignore")
        subs = []
        for o, n in targets.items():
            if o not in text:
                continue
            # [[o]] [[o|alias]] [[o\|alias]] [[o#heading]] [[o#^block]] ![[o]]
            #
            # The trailing delimiter is captured, not looked ahead at. A
            # zero-width lookahead makes the matched text a bare prefix, and the
            # literal str.replace below would then also rewrite the inside of a
            # longer name that starts the same way: moving `Old/Note` silently
            # rewrote `[[Old/NoteOther]]` too. Including the delimiter makes
            # every substitution unambiguous.
            #
            # `.md` before the delimiter is optional, and kept if present. The
            # Canvas archive writes every file-note link as `[[.../deck.pdf.md|x]]`
            # (the name already has an extension, so the `.md` has to stay), and
            # people type `[[Note.md]]`. Requiring the delimiter straight after
            # the path left all of those behind: 14 archive notes still pointed
            # at the old folders after the 2026-09-23 quarter regroup.
            for m in re.finditer(r"!?\[\[" + re.escape(o) + r"(\.md)?([\]|#\\])", text):
                bang = "!" if m.group(0).startswith("!") else ""
                subs.append((m.group(0), f"{bang}[[{n}{m.group(1) or ''}{m.group(2)}"))
            # markdown links: ](o.md) or ](o)
            for m in re.finditer(r"\]\(" + re.escape(o) + r"(\.md)?\)", text):
                subs.append((m.group(0), m.group(0).replace(o, n)))
            # Backticked prose paths: `o`, `o.md`, `o/`.
            #
            # Not links, so nothing renders as broken -- they just quietly
            # become false, which is worse. This vault's docs refer to paths in
            # backticks constantly (SYSTEM.md, the Vault Map, skill bodies), and
            # the 2026-09-09 <COACHING_PROJECT> move left several of them wrong for six
            # days with no check able to notice.
            # The tail allows a deeper path under a moved folder:
            # `Old/Folder/Sub/file.md` has to move with `Old/Folder`, and
            # matching only the exact folder left those pointing at nothing.
            for m in re.finditer(r"`" + re.escape(o) + r"([^`]*)`", text):
                tail = m.group(1)
                if tail and not tail.startswith(("/", ".md")):
                    continue        # a different path that merely shares a prefix
                subs.append((m.group(0), f"`{n}{tail}`"))
        if subs:
            edits[moved_from.get(rel, rel)] = sorted(set(subs))
    return edits


BOOKMARKS = ".obsidian/bookmarks.json"


def _plan_bookmarks(vault, pairs, folder=None):
    """The rewritten bookmarks tree and the (old, new) paths it changed.

    Groups nest (`"type": "group"` with its own `items`), so this walks them.
    A folder move also carries every bookmark on a subfolder, which `pairs`
    cannot see because it lists files only.
    """
    f = vault / BOOKMARKS
    if not f.is_file():
        return None, []
    tree = json.loads(f.read_text(encoding="utf-8"))
    files = dict(pairs)
    changed = []

    def walk(items):
        for it in items:
            if "items" in it:
                walk(it["items"])
            p = it.get("path")
            if not p:
                continue
            new = files.get(p)
            if new is None and folder and (p == folder[0] or p.startswith(folder[0] + "/")):
                new = folder[1] + p[len(folder[0]):]
            if new and new != p:
                changed.append((p, new))
                it["path"] = new

    walk(tree.get("items", []))
    return tree, changed


def _notes(vault):
    for f in vault.rglob("*.md"):
        if SKIP_DIRS & set(f.relative_to(vault).parts):
            continue
        yield f


def _in_git(vault):
    r = subprocess.run(["git", "-C", str(vault), "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _prune_empty(folder, vault):
    """Remove folders the move emptied, up to but never including the vault."""
    while folder != vault and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        print(f"    removed empty {folder.relative_to(vault)}/")
        folder = folder.parent


if __name__ == "__main__":
    raise SystemExit(main())
