"""The component-doc standard, defined once.

_system/_component-doc-template.md is the rule; this is its only executable
form. Three callers share it so they can never disagree:

    vt docs [FILE...]     check files, or every component doc in the vault
    vt docs --hook        Claude Code PostToolUse hook: reads the hook JSON on
                          stdin, checks the edited file, exits 2 with the
                          violations on stderr so the editing session fixes them
    jobs/inventory.py     the daily sweep, the backstop for edits made outside
                          Edit/Write (a Bash sed or python rewrite never fires
                          the hook)

Why the hook exists: the standard was written down in three places and still
drifted, because a written rule depends on the editing session noticing it
applies. On 2026-09-23 a session restructured whatsapp-source without opening
the template, and whatsapp-bridge carried three false status claims above the
fold while passing every check. Findings that go to an Inbox note reach a
human days later; the session that caused them is gone. The hook reports to
the session that made the edit, while it can still fix it.
"""

import json
import re
import sys
from pathlib import Path

# The template's sections, in order. Prefix match: "Setup / rebuild" is Setup.
SECTION_ORDER = ["What it's for", "How it works", "Setup", "Config", "Gotchas",
                 "Known limitations", "Decisions", "History", "See also"]

# Template frontmatter: stage values it allows. `status:` belongs to tasks.
STAGES = {"built", "partial", "design-only", "superseded", "dead"}

# Deliberately exempt per SYSTEM.md: an agent prompt, an external-audience doc,
# generated pages and the two hubs.
EXEMPT_STEMS = {"mcp-instructions", "migration-guide", "maintenance-status",
                "fitness-sync-status", "flight-watch-status", "consistency-today", "SYSTEM", "README",
                # rewritten by ~/carmen/tools/downloads.py on every run (Nov 8)
                "carmen-downloads",
                # the WhatsApp bridge's system-prompt file, read at every turn
                "reply-style"}
# Folders of machine-written records, not docs: the maintenance brief writes a
# note a week into briefs/ and appends findings to alert-log/ (2026-10-03).
GENERATED_DIRS = {"briefs", "alert-log"}
EXEMPT_DIRS = {"skills", "proposals", "claude"} | GENERATED_DIRS

# Statements about the document's own past. Narrow on purpose: they match
# "this was resolved/superseded", never a rationale that mentions a date.
HISTORY_MARKERS = ("[!done]", "resolved 2026-", "resolved 2027-", "superseded the",
                   "used to sit here", "used to live", "drifted for")

# Callout lines allowed between the H1 and the first section. A short warning
# is fine; a build log is history sitting in the reading path.
MAX_PREAMBLE_CALLOUT_LINES = 12


def vault_of(path: Path) -> Path | None:
    """The vault a file belongs to, if it sits under <vault>/_system/."""
    for parent in path.parents:
        if parent.name == "_system" and (parent.parent / "CLAUDE.md").exists():
            return parent.parent
    return None


def is_component_doc(path: Path) -> bool:
    vault = vault_of(path)
    if vault is None or path.suffix != ".md":
        return False
    rel = path.relative_to(vault / "_system")
    if any(part in EXEMPT_DIRS for part in rel.parts):
        return False
    return path.stem not in EXEMPT_STEMS and not path.stem.startswith("_")


def is_generated(path: Path) -> bool:
    """A machine-written record under _system/, never a component doc."""
    return any(part in GENERATED_DIRS for part in path.parts)


def history_markers_above_fold(text: str) -> list[str]:
    """Markers of the doc's own past found above ## History. Applies to any
    _system doc, component or not."""
    cut = text.find("\n## History")
    above = text[:cut] if cut > 0 else text
    # Strip code first: maintenance.md quotes these markers to describe the
    # rule, and would otherwise be reported for its own definition.
    above = re.sub(r"```.*?```", "", above, flags=re.S)
    above = re.sub(r"`[^`\n]*`", "", above)
    return sorted({m for m in HISTORY_MARKERS if m in above.lower()})


def _frontmatter(text: str) -> dict[str, str] | None:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    out = {}
    for line in m.group(1).splitlines():
        if re.match(r"^[A-Za-z_][\w-]*:", line):
            k, v = line.split(":", 1)
            out[k.strip()] = v.split("#", 1)[0].strip()
    return out


def check_text(text: str) -> list[str]:
    """Every violation of the template in one component doc, as sentences."""
    found = []

    front = _frontmatter(text)
    if front is None:
        found.append("no frontmatter block at the top of the file")
        front = {}
    missing = [k for k in ("component", "stage", "verified") if not front.get(k)]
    if missing:
        found.append(f"frontmatter missing {', '.join(missing)} (template rule 1, 7)")
    if "status" in front:
        found.append("frontmatter has `status:`, which is reserved for tasks; use `stage:` (rule 7)")
    if front.get("stage") and front["stage"] not in STAGES:
        found.append(f"`stage: {front['stage']}` is not one of {', '.join(sorted(STAGES))}")

    heads = re.findall(r"^## (.+?)\s*$", text, re.M)
    unknown = [h for h in heads if not any(h.startswith(s) for s in SECTION_ORDER)]
    if unknown:
        found.append("sections the template does not have: "
                     + "; ".join(f'"{h}"' for h in unknown)
                     + ". Fold them into the template's sections, as ### subsections "
                       "where the content needs its own heading (rule 2)")
    known = [h for h in heads if h not in unknown]
    idx = [next(i for i, s in enumerate(SECTION_ORDER) if h.startswith(s)) for h in known]
    if idx != sorted(idx):
        found.append("sections out of the template's fixed order: "
                     + " → ".join(known) + f". Order is: {' → '.join(SECTION_ORDER)}")

    cut = text.find("\n## History")
    above = text[:cut] if cut > 0 else text
    status_lines = len(re.findall(r"^\*\*Status", above, re.M))
    if status_lines == 0:
        found.append("no bold **Status** line under the H1 (rule 4)")
    elif status_lines > 1:
        found.append(f"{status_lines} **Status** lines above History; status lives in exactly "
                     "two places, the frontmatter and the line under the H1 (rule 4)")

    h1 = re.search(r"^# .+$", text, re.M)
    first_h2 = re.search(r"^## ", text, re.M)
    if h1 and first_h2 and first_h2.start() > h1.end():
        preamble = text[h1.end():first_h2.start()]
        callout = sum(1 for line in preamble.splitlines() if line.startswith(">"))
        if callout > MAX_PREAMBLE_CALLOUT_LINES:
            found.append(f"{callout} lines of callout between the H1 and the first section; "
                         "build logs and resolved notes belong under ## History (rule 3)")

    markers = history_markers_above_fold(text)
    if markers:
        found.append(f"history above the fold ({', '.join(markers)}); move it under "
                     "## History (rule 3)")
    return found


def all_component_docs(vault: Path) -> list[Path]:
    return [p for p in sorted((vault / "_system").rglob("*.md")) if is_component_doc(p)]


def _hook() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # not ours to judge; never break the session over it
    tool_input = payload.get("tool_input") or {}
    raw = tool_input.get("file_path") or (payload.get("tool_response") or {}).get("filePath")
    if not raw:
        return 0
    path = Path(raw).expanduser()
    if not path.exists() or not is_component_doc(path):
        return 0
    problems = check_text(path.read_text(errors="ignore"))
    if not problems:
        return 0
    print(f"{path.name} breaks the component-doc standard "
          f"(_system/_component-doc-template.md). Fix before finishing, even if the "
          f"problem predates this edit:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    print("Move content, never delete it; keep headings other notes link to findable.",
          file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    if argv[:1] == ["--hook"]:
        return _hook()
    if argv:
        paths = [Path(a).expanduser().resolve() for a in argv]
    else:
        vault = next((v for v in (Path.home() / "Obsidian/<VAULT_NAME>",
                                  Path.home() / "vault") if (v / "_system").is_dir()), None)
        if vault is None:
            print("vt docs: no vault found", file=sys.stderr)
            return 2
        paths = all_component_docs(vault)
    bad = 0
    for path in paths:
        if not is_component_doc(path):
            print(f"skip  {path.name} (not a component doc)")
            continue
        problems = check_text(path.read_text(errors="ignore"))
        if problems:
            bad += 1
            print(f"FAIL  {path.name}")
            for p in problems:
                print(f"        - {p}")
        else:
            print(f"ok    {path.name}")
    print(f"\n{bad} of {len(paths)} doc(s) break the standard")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
