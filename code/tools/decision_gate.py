#!/usr/bin/env python3
"""The decision gate: no rule the owner decided changes without its reason being seen (Nov 11).

owner decision, Nov 11: "regressions happening, often because you don't remember the original rule why
things were implemented (and sometimes its right to change them, but you need to know the
context)". Each decided rule has a row in one of two tables: Carmen's (D-n, checkin-app.md
§ Decisions) and the server's (S-n, vps-vault-host.md § Decisions). The code that implements it
carries a bare mark (`# D-n`; `// D-n` in a page's script; `<!-- D-n -->` in prompt text), and
tests/checkin_decisions.py checks it both ways: the rule holds, and the rejected alternative fails.

What the gate refuses (exit 1):
  - a change to a decision's test that does not name its id with "<id> row updated" or
    "<id> row unchanged: <why>"
  - "<id> row updated" when the row reads exactly as at the last land or deploy (the edit has
    not reached this machine yet: pull the doc, Sync, and retry), or when there is no such row
  - one message saying both "row updated" and "row unchanged" for the same id
  - a decision doc that cannot be read (fail closed), or a duplicate id
  - landing the branch a pending row names, while that branch lacks the row's test or mark
A row may be pending (Nov 12): its Code cell starts `pending: <branch>, <Mon D>`, for a rule
written in the vault before its code lands. For 48 h the tests and the morning check accept it
without a test or mark; after that the morning check flags it. Only landing the named branch is
refused until the branch carries the row's test and mark; other work lands as usual.
What it only warns about: a change within NEAR lines of a mark. It may or may not change the rule;
the warning prints the row so whoever changes it sees the reason.

    python3 -m tools.decision_gate range <base> <head>     the commits about to be deployed or landed
    python3 -m tools.decision_gate staged <commit-msg-file>  a commit being made (the commit-msg hook)
    python3 -m tools.decision_gate stamp                   record the rows as landed (after a pass)
    python3 -m tools.decision_gate new D|S                 the next free id
    python3 -m tools.decision_gate list                    every decision: rule, marks, test, last commit
    python3 -m tools.decision_gate audit                   the morning check (exit 1 on a real break)

Stdlib only.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEAR = 3
ID = r"[DS]-\d+"
# A mark is a comment that is only ids: "# D-14 D-6", "<!-- D-22 -->".
MARK = re.compile(rf"^\s*(?:#|//|<!--)\s*((?:{ID})(?:[\s,]+{ID})*)\s*(?:-->)?\s*$")
D_TEST = re.compile(rf'(?:^#\s*|decide\(")({ID})\b')
TEST_FILE = "tests/checkin_decisions.py"
CODE_EXT = (".py", ".html", ".js")
PROMPT_EXT = (".md", ".txt")
_VAULTS = [os.path.expanduser("~/vault"), os.path.expanduser("~/Obsidian/<VAULT_NAME>")]
TABLES = {"D": ("DECISIONS_DOC", "_system/checkin-app.md"), "S": ("DECISIONS_DOC_S", "_system/vps-vault-host.md")}


class DocMissing(RuntimeError):
    pass


PENDING = re.compile(r"^pending:\s*([\w./-]+)\s*(?:,\s*([A-Z][a-z]{2})\s+(\d{1,2}))?")
PENDING_H = 48
_MON = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def _pacific_today():
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date()


def pending(r: dict, today=None) -> dict:
    """{id: (branch, since date or None, fresh)} for rows whose Code cell starts `pending:`.
    Fresh: dated, and no more than PENDING_H hours old by date (today or yesterday, or two days back)."""
    import datetime as dt
    today = today or _pacific_today()
    out = {}
    for did, row in r.items():
        m = PENDING.match(row.get("code", ""))
        if not m:
            continue
        since = None
        if m.group(2) in _MON:
            try:
                since = dt.date(today.year, _MON[m.group(2)], int(m.group(3)))
                if since > today:
                    since = since.replace(year=today.year - 1)
            except ValueError:
                since = None
        out[did] = (m.group(1), since, since is not None and (today - since).days * 24 <= PENDING_H)
    return out


def _git(*args, cwd: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def doc_path(prefix: str) -> str:
    env, rel = TABLES[prefix]
    for d in [os.environ.get(env, "")] + [os.path.join(v, rel) for v in _VAULTS]:
        if d and os.path.isfile(d):
            return d
    raise DocMissing(f"{rel} cannot be read, so the {prefix}-n decisions cannot be checked (set {env})")


def _table(path: str, prefix: str) -> tuple[list, set]:
    """([(id, {column: cell}, raw line)], retired ids) of one doc's table, columns by name."""
    head, out, retired, in_retired = None, [], set(), False
    for line in open(path, encoding="utf-8"):
        if line.startswith("#"):
            in_retired = line.strip().lower() == "### retired" and head is not None
            continue
        if in_retired:
            m = re.match(rf"^\s*-\s*\**({prefix}-\d+)\b", line)
            if m:
                retired.add(m.group(1))
            continue
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")] if line.startswith("| ") else []
        if cells[:1] == ["ID"]:
            head = [c.lower() for c in cells]
        elif head and cells and re.fullmatch(rf"{prefix}-\d+", cells[0]) and len(cells) == len(head):
            out.append((cells[0], dict(zip(head, cells)), line.rstrip("\n")))
    return out, retired


def table_rows(prefixes=("D", "S")) -> tuple[dict, set, list]:
    """({id: {column: cell, "_line": raw}}, retired ids, duplicate ids). Raises DocMissing."""
    rows, retired, dups = {}, set(), []
    for p in prefixes:
        found, ret = _table(doc_path(p), p)
        retired |= ret
        for did, cells, raw in found:
            if did in rows:
                dups.append(did)
            rows[did] = {**cells, "_line": raw}
    return rows, retired, dups


def rows() -> dict:
    """{id: its rule}. Raises DocMissing when a decision doc cannot be read (fail closed)."""
    return {k: v.get("rule", "") for k, v in table_rows()[0].items()}


# ---------------------------------------------------------------- row hashes
def _state() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")) / "automation" / "decisions" / "row_hashes.json"


def _hash(line: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", line.strip()).encode()).hexdigest()[:16]


def _stamps() -> list:
    """[{"commit": sha, "rows": {id: hash}}], oldest first. The flat form of Nov 12 morning had no
    commit and is ignored (it made a deploy after a land compare the range with its own rows)."""
    try:
        d = json.loads(_state().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return d.get("stamps", []) if isinstance(d, dict) else []


def stored_hashes(exclude: set | None = None) -> dict:
    """The rows as recorded at the last land or deploy before the commits being checked: the newest
    stamp whose commit is not one of them (a deploy right after a land compares with the rows before
    that land, not after it)."""
    for st in reversed(_stamps()):
        if not exclude or st.get("commit") not in exclude:
            return st.get("rows", {})
    return {}


def stamp(commit: str | None = None) -> int:
    r = table_rows()[0]
    commit = commit or _git("rev-parse", "HEAD", cwd=str(ROOT)).strip()
    st = [x for x in _stamps() if x.get("commit") != commit][-49:]
    st.append({"commit": commit, "rows": {k: _hash(v["_line"]) for k, v in r.items()}})
    p = _state()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"stamps": st}, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)
    return len(r)


# ---------------------------------------------------------------- diffs
def _hunks(diff: str):
    """{path: [(old_start, old_len, new_start, new_len)]} of a -U0 diff."""
    path, out = None, {}
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            p = line[4:].strip()
            path = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            if path:
                out.setdefault(path, [])
        elif line.startswith("--- ") and path is None:
            p = line[4:].strip()
            if p != "/dev/null":
                path = p[2:] if p.startswith("a/") else p
                out.setdefault(path, [])
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                out[path].append((int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)))
        elif line.startswith("diff --git"):
            path = None
    return out


def marks_in(text: str) -> list[tuple[int, str]]:
    """[(line, id)] for every mark in a file's text (1-based lines)."""
    out = []
    for k, l in enumerate((text or "").split("\n"), 1):
        m = MARK.match(l)
        if m:
            out += [(k, did) for did in re.findall(ID, m.group(1))]
    return out


def _ids_near(text: str, changed: list[int]) -> dict:
    marks = marks_in(text)
    out = {}
    for n in changed:
        for k, did in marks:
            if abs(k - n) <= NEAR:
                out.setdefault(did, n)
    return out


def _test_ids(text: str, changed: list[int]) -> dict:
    """In the decision tests, each changed line belongs to the id block above it."""
    lines = (text or "").split("\n")
    out = {}
    for n in changed:
        for k in range(min(n, len(lines)) - 1, -1, -1):
            m = D_TEST.search(lines[k])
            if m:
                out.setdefault(m.group(1), n)
                break
    return out


def _gated(path: str) -> bool:
    return path.endswith(CODE_EXT) or path.startswith("bin/") or (path.startswith("jobs/") and path.endswith(PROMPT_EXT))


def touched(diff: str, old_of, new_of) -> tuple[dict, dict]:
    """({id: "path:line"} of decision tests changed, {id: "path:line"} of code changed near a mark)."""
    tests, near = {}, {}
    for path, hunks in _hunks(diff).items():
        if not _gated(path):
            continue
        new_lines = [n for _, _, ns, nl in hunks for n in range(ns, ns + nl) if nl]
        old_lines = [n for os_, ol, _, _ in hunks for n in range(os_, os_ + ol) if ol]
        new, old = new_of(path), old_of(path)
        if path == TEST_FILE:
            found, into = {**_test_ids(old, old_lines), **_test_ids(new, new_lines)}, tests
        else:
            found, into = {**_ids_near(old, old_lines), **_ids_near(new, new_lines)}, near
        for did, n in found.items():
            into.setdefault(did, f"{path}:{n}")
    return tests, near


def _key(did: str):
    return (did[0], int(did.split("-")[1]))


def problems(found, message: str, near: dict | None = None, exclude: set | None = None) -> tuple[list[str], list[str]]:
    """(refusals, warnings). found: the decision tests changed, {id: where}."""
    try:
        r, _, dups = table_rows()
    except DocMissing as e:
        return [str(e) + ". Refusing: pull the doc (Obsidian Sync) and retry."], []
    msg, bad, warn = message or "", [], []
    if dups:
        bad.append(f"duplicate decision ids in the tables: {', '.join(sorted(set(dups), key=_key))}")
    hashes = stored_hashes(exclude)
    for did in sorted(found, key=_key):
        named = re.search(rf"\b{did}\b", msg)
        said = re.search(rf"\b{did} row (updated|unchanged:\s*\S)", msg)
        if not (named and said):
            rule = r.get(did, {}).get("rule", "(no row)")
            bad.append(f"{did} ({found[did]}) {rule[:160]}\n    This changes the decision's test. Read its row (why, his words), "
                       f"then name {did} in the commit message with the new reason and '{did} row updated' "
                       f"(row edited in its table) or '{did} row unchanged: <why>'.")
    for did in sorted(set(re.findall(rf"\b({ID}) row updated", msg)), key=_key):
        if re.search(rf"\b{did} row unchanged", msg):
            bad.append(f"{did}: the message says both 'row updated' and 'row unchanged'. Say which.")
        elif did not in r:
            bad.append(f"{did}: the message says its row was updated, but no table has a {did} row")
        elif hashes.get(did) == _hash(r[did]["_line"]):
            bad.append(f"{did}: the message says its row was updated, but the row reads as it did at the last land. "
                       f"If you edited it, the edit has not reached this machine: pull the doc (Obsidian Sync) and retry.")
    for did in sorted(set(re.findall(rf"\b({ID}) row unchanged", msg)), key=_key):
        if did in r and did in hashes and hashes[did] != _hash(r[did]["_line"]) and not re.search(rf"\b{did} row updated", msg):
            warn.append(f"{did}: the message says its row is unchanged, but the row differs from the last land.")
    for did in sorted(near or {}, key=_key):
        if did in found or re.search(rf"\b{did}\b", msg):
            continue
        warn.append(f"{did} ({near[did]}) is next to this change: {r.get(did, {}).get('rule', '(no row)')[:160]}\n"
                    f"    If the change alters the rule, name {did} with the new reason and its row.")
    return bad, warn


def check_range(base: str, head: str, cwd: str | None = None) -> list[str]:
    bad, warn = range_result(base, head, cwd)
    return bad


def range_result(base: str, head: str, cwd: str | None = None):
    diff = _git("diff", "-U0", base, head, cwd=cwd)
    msg = _git("log", "--format=%B", f"{base}..{head}", cwd=cwd)
    tests, near = touched(diff, lambda p: _git("show", f"{base}:{p}", cwd=cwd), lambda p: _git("show", f"{head}:{p}", cwd=cwd))
    inside = set(_git("rev-list", f"{base}..{head}", cwd=cwd).split())
    bad, warn = problems(tests, msg, near, inside)
    return bad + pending_for(head, cwd), warn


def pending_for(branch: str, cwd: str | None = None) -> list[str]:
    """Refusals for landing a branch that a pending row names while it lacks that row's test or mark."""
    try:
        r = table_rows()[0]
    except DocMissing:
        return []
    out = []
    for did, (br, _, _) in pending(r).items():
        if br != branch:
            continue
        has_test = f'decide("{did}"' in _git("show", f"{branch}:{TEST_FILE}", cwd=cwd)
        has_mark = bool(_git("grep", "-l", "-E", rf"^\s*(#|//|<!--)\s*({ID}[\s,]+)*{did}\b", branch, "--", cwd=cwd).strip())
        if not (has_test and has_mark):
            out.append(f"{did} is pending on {branch}, which lacks its {'test' if not has_test else 'mark'}: add the "
                       f"check to {TEST_FILE} and the bare mark at the code, then set the row's Code cell to that code.")
    return out


def staged_result(msg_file: str, cwd: str | None = None):
    diff = _git("diff", "--cached", "-U0", cwd=cwd)
    msg = Path(msg_file).read_text(encoding="utf-8", errors="replace") if msg_file else ""
    tests, near = touched(diff, lambda p: _git("show", f"HEAD:{p}", cwd=cwd), lambda p: _git("show", f":{p}", cwd=cwd))
    return problems(tests, msg, near)


def check_staged(msg_file: str, cwd: str | None = None) -> list[str]:
    return staged_result(msg_file, cwd)[0]


# ---------------------------------------------------------------- ids, overview, audit
def code_files(root: Path = ROOT) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if not p.is_file() or rel.startswith((".git", ".venv")) or "__pycache__" in p.parts or rel == TEST_FILE:
            continue
        if rel.startswith("tests/"):
            continue
        if _gated(rel):
            out.append(p)
    return out


def all_marks(root: Path = ROOT) -> dict:
    """{id: ["path:line", ...]} for every bare mark in the code."""
    out = {}
    for p in code_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for k, did in marks_in(text):
            out.setdefault(did, []).append(f"{p.relative_to(root).as_posix()}:{k}")
    return out


def prose_marks(root: Path = ROOT) -> list[str]:
    """Marks with words after the id: the rule's words live in its row only."""
    out = []
    for p in code_files(root):
        try:
            lines = p.read_text(encoding="utf-8").split("\n")
        except (UnicodeDecodeError, OSError):
            continue
        for k, l in enumerate(lines, 1):
            if re.match(rf"^\s*(?:#|//|<!--)\s*{ID}\b", l) and not MARK.match(l):
                out.append(f"{p.relative_to(root).as_posix()}:{k}")
    return out


def test_ids(root: Path = ROOT) -> set:
    try:
        return set(re.findall(rf'decide\("({ID})"', (root / TEST_FILE).read_text(encoding="utf-8")))
    except OSError:
        return set()


def new_id(prefix: str, root: Path = ROOT) -> str:
    r, retired, _ = table_rows()
    used = set(r) | retired | set(all_marks(root)) | test_ids(root)
    n = max([int(d.split("-")[1]) for d in used if d.startswith(prefix + "-")] or [0]) + 1
    return f"{prefix}-{n}"


def overview(root: Path = ROOT) -> list[dict]:
    r, retired, _ = table_rows()
    marks, tests = all_marks(root), test_ids(root)
    out = []
    for did in sorted(r, key=_key):
        last = subprocess.run(["git", "log", "-1", "-E", f"--grep=\\b{did}\\b", "--format=%h %ad %s", "--date=format-local:%b %d"],
                              cwd=str(root), capture_output=True, text=True, env={**os.environ, "TZ": "America/Los_Angeles"}).stdout.strip()
        rule = r[did].get("rule", "")
        out.append({"id": did, "table": "Carmen" if did[0] == "D" else "server", "rule": rule if len(rule) <= 90 else rule[:87] + "...",
                    "marks": marks.get(did, []), "test": did in tests, "last_commit": last})
    return out


_CODE_REF = re.compile(r"`([\w./-]+\.(?:py|html|js|md|txt))`|`(bin/[\w.-]+)`")


def _resolve(ref: str, root: Path):
    for base in ("", "jobs/checkin_app/", "jobs/"):
        p = root / (base + ref)
        if p.is_file():
            return p
    for v in _VAULTS:
        p = Path(v) / "_system" / ref
        if p.is_file():
            return p
    return None


def audit(root: Path = ROOT, vault: str | None = None) -> list[str]:
    """The morning check: [problem] for a real break, [] when all holds."""
    try:
        r, retired, dups = table_rows()
    except DocMissing as e:
        return [str(e)]
    out = [f"duplicate id {d}" for d in sorted(set(dups), key=_key)]
    out += [f"{d} is both a row and retired" for d in sorted(set(r) & retired, key=_key)]
    vault = vault or next((v for v in _VAULTS if os.path.isdir(v)), "")
    for p in TABLES.values():
        stem = Path(p[1]).stem
        out += [f"a conflict copy of {stem}.md: {Path(c).name}" for c in glob.glob(os.path.join(vault, "_system", f"{stem}*conflict*"))]
    marks, tests = all_marks(root), test_ids(root)
    pend = pending(r)
    for did, (br, since, fresh) in sorted(pend.items(), key=lambda kv: _key(kv[0])):
        if not fresh:
            out.append(f"{did} has been pending on {br} since {since or 'an undated day'} (over {PENDING_H} h): land its "
                       f"code, test and mark, or take the row out")
    for did, where in sorted(marks.items(), key=lambda kv: _key(kv[0])):
        if did not in r:
            out.append(f"{did} marked at {where[0]} has no row" + (" (it is retired: remove the mark)" if did in retired else ""))
        elif did not in tests:
            out.append(f"{did} has no test in {TEST_FILE}")
    for did in sorted(tests - set(r), key=_key):
        out.append(f"{did} has a test but no row" + (" (retired: remove the test)" if did in retired else ""))
    for did in sorted(set(r) - tests - {d for d, v in pend.items() if v[2]}, key=_key):
        out.append(f"{did} has a row but no test")
    out += [f"a mark with words after its id: {w} (the words belong in the row)" for w in prose_marks(root)]
    for did in sorted(set(r) - set(pend), key=_key):
        for a, b in _CODE_REF.findall(r[did].get("code", "")):
            p = _resolve(a or b, root)
            if p is None:
                out.append(f"{did}'s row names {a or b}, which does not exist")
                continue
            if p.suffix in (".md",) or "skills/" in p.as_posix():
                continue   # vault files carry no marks
            if not any(d == did for _, d in marks_in(p.read_text(encoding="utf-8", errors="replace"))):
                out.append(f"{did}'s row names {a or b}, which has no {did} mark")
    known = set(r) | retired
    for doc in sorted(glob.glob(os.path.join(vault, "_system", "*.md"))):
        try:
            text = open(doc, encoding="utf-8").read()
        except OSError:
            continue
        for did in sorted(set(re.findall(rf"\(({ID})\)", text)) - known, key=_key):
            out.append(f"{Path(doc).name} points to ({did}), which no table has")
    return out


def main(argv: list[str]) -> int:
    cmd = argv[:1]
    if cmd == ["range"] and len(argv) == 3:
        bad, warn = range_result(argv[1], argv[2])
    elif cmd == ["staged"] and len(argv) == 2:
        bad, warn = staged_result(argv[1])
    elif cmd == ["stamp"]:
        try:
            print(f"decision rows recorded: {stamp()}")
            return 0
        except DocMissing as e:
            print(f"decision gate: {e}", file=sys.stderr)
            return 1
    elif cmd == ["new"] and len(argv) == 2 and argv[1].upper() in TABLES:
        print(new_id(argv[1].upper()))
        return 0
    elif cmd == ["list"]:
        for o in overview():
            print(f"{o['id']:<5} {o['table']:<7} {'test' if o['test'] else 'NO TEST':<7} {o['rule']}\n"
                  f"      marks: {', '.join(o['marks']) or 'none'}\n      last: {o['last_commit'] or 'no commit names it'}")
        return 0
    elif cmd == ["audit"]:
        bad = audit()
        print("\n".join(bad) if bad else "decisions: all consistent")
        return 1 if bad else 0
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for w in warn:
        print("decision gate (note): " + w, file=sys.stderr)
    if bad:
        print("decision gate: refused:\n" + "\n".join("  " + b for b in bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
