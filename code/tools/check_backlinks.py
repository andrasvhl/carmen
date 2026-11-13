#!/usr/bin/env python3
"""List every vault file that wikilinks to a given note.

Written 2026-08-21 to the interface `_system/scripts/README.md` had already documented
since 2026-08-06 — the doc described this script for two weeks while the file was missing,
which `_system/maintenance.md` recorded as known drift. Read-only by design.

    python3 _system/scripts/check_backlinks.py <note-name>
    python3 _system/scripts/check_backlinks.py <note-name> --context
"""

import os
import re
import sys

# Resolved by the shared config, not from this file's own location. These
# scripts used to live inside the vault and walk three directories up;
# they now live in ~/automation, so that trick no longer works.
from runner.config import VAULT as _VAULT
VAULT = str(_VAULT)
SKIP = {".obsidian", ".trash", "Attachments", ".git", "Course Archive"}


def link_pattern(name):
    """Match [[name]], [[name|Alias]], [[name#Section]] and path-qualified forms.

    The basename is what matters: Obsidian resolves [[Foo]] and [[path/to/Foo]] to the same
    note, so both have to count. Case-insensitive, per the README.
    """
    base = re.escape(name)
    return re.compile(
        r"\[\[(?:[^\]\[|#]*/)?" + base + r"(?:\.md)?(?:#[^\]\[|]*)?(?:\|[^\]\[]*)?\]\]",
        re.IGNORECASE,
    )


def walk_markdown(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP]
        for fn in filenames:
            if fn.endswith(".md"):
                yield os.path.join(dirpath, fn)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    context = "--context" in sys.argv
    if len(args) != 1:
        print(__doc__.strip())
        return 2

    name = args[0].removesuffix(".md")
    name = os.path.basename(name)
    pattern = link_pattern(name)

    hits = []
    for path in walk_markdown(VAULT):
        rel = os.path.relpath(path, VAULT)
        if os.path.basename(path)[:-3].lower() == name.lower():
            continue  # a note linking to itself is noise
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        matched = [(i + 1, l) for i, l in enumerate(lines) if pattern.search(l)]
        if matched:
            hits.append((rel, matched))

    if not hits:
        print(f"No files link to [[{name}]].")
        print("Note: plain-text mentions without [[ ]] are not found — see README, Known limitations.")
        return 1

    total = sum(len(m) for _, m in hits)
    print(f"{len(hits)} file(s) link to [[{name}]] — {total} link(s) total:\n")
    for rel, matched in sorted(hits):
        print(f"  {rel}  ({len(matched)})")
        if context:
            for lineno, line in matched:
                print(f"      {lineno}: {line.strip()}")
    if not context:
        print("\nRe-run with --context to see the matching lines.")
    print("\nThis finds links, not duplicated facts. Read what each one says before calling it done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
