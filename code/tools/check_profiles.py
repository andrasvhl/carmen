#!/usr/bin/env python3
"""Enforce the profile conventions mechanically, because a written rule was not enough.

A profile states facts. Reasoning belongs in the note that acts on the facts, and the single
most reliable way a profile degrades is by accumulating conclusions next to the rows that
produced them. That happened twice in one day on 2026-08-28 — in `Health Profile`, then again
in `Identity Profile` immediately after the rule against it was written. Hence this script.

  check_profiles.py            # report
  check_profiles.py --strict   # exit 1 on any violation, for hooks or cron

Checks, in order of how often they actually bite:
  1. prose per section        <= 110 words   (reasoning creep)
  2. words per section        <= 500         (retrieval: a section should stand alone)
  3. required spine           read-trigger, Unknowns register, Maintenance, See also
  4. fact tables              Source and As of must be the last two columns
  5. every fact row           has a Source, unless it is an `— elicit` placeholder
  6. frontmatter              sub-profiles declare parent + domain + verified
  7. freshness                verified: within its shelf life
  8. no credentials           password / PIN / 2FA / recovery-code keys anywhere
"""
import glob, io, os, re, sys
from datetime import date, datetime

# Resolved by the shared config, not from this file's own location.
from runner.config import VAULT as _VAULT
VAULT = str(_VAULT)
os.chdir(VAULT)
PROSE_MAX, SECTION_MAX, SHELF_DAYS = 110, 500, 365
SKIP_SECTIONS = ("See also", "Maintenance", "Config", "Decisions", "History")
CRED = re.compile(r"^(password|passwort|passcode|pin|tan|2fa|otp|recovery.?code|seed.?phrase|api.?key|secret):", re.I)

def profiles():
    return ["Areas/Master Profile.md"] + sorted(glob.glob("Areas/*/*Profile*.md")) + ["Areas/People/People.md"]

def main():
    strict = "--strict" in sys.argv
    issues = []
    for p in profiles():
        if not os.path.exists(p):
            issues.append((p, "missing file")); continue
        s = io.open(p, encoding="utf-8").read()
        name = os.path.basename(p)[:-3]
        fm = s.split("---")[1] if s.startswith("---") else ""
        meta = dict(re.findall(r"^(\w+):[ \t]*(.*)$", fm, re.M))

        # 6. frontmatter
        if meta.get("type") == "sub-profile":
            for k in ("parent", "domain", "verified"):
                if not meta.get(k):
                    issues.append((name, f"frontmatter missing `{k}`"))
        # 7. freshness
        v = meta.get("verified", "")
        try:
            age = (date.today() - datetime.strptime(v.strip(), "%Y-%m-%d").date()).days
            if age > SHELF_DAYS:
                issues.append((name, f"verified: is {age} days old — re-check or say so"))
        except Exception:
            if meta.get("type") in ("sub-profile", "master-profile"):
                issues.append((name, "verified: missing or unparseable"))

        # 3. spine
        if "Read this when:" not in s:
            issues.append((name, "no `**Read this when:**` trigger line"))
        for need in ("Unknowns", "## Maintenance", "## See also"):
            if need not in s:
                issues.append((name, f"missing `{need}`"))

        # 8. credentials
        for i, ln in enumerate(s.split("\n"), 1):
            if CRED.match(ln.strip()):
                issues.append((name, f"line {i}: looks like a stored credential — never record these"))

        for sec in re.split(r"\n(?=## )", s):
            if not sec.startswith("## "):
                continue
            title = sec.split("\n")[0][3:].strip()
            if title.startswith(SKIP_SECTIONS):
                continue
            body = sec.split("\n")[1:]
            prose = sum(len(l.split()) for l in body if not l.strip().startswith("|"))
            total = len(sec.split())
            # 1 + 2
            if prose > PROSE_MAX:
                issues.append((name, f"§{title}: {prose} words of prose (max {PROSE_MAX}) — move the reasoning to the note that acts on it"))
            if total > SECTION_MAX:
                issues.append((name, f"§{title}: {total} words (max {SECTION_MAX}) — split the section"))
            # 4 + 5
            for ln in body:
                t = ln.strip()
                if not t.startswith("|") or set(t) <= set("|- "):
                    continue
                cells = [c.strip() for c in t.split("|")][1:-1]
                if t.startswith("| Fact ") or (cells and cells[0] in ("Fact", "Role", "Obligation", "Source", "Person", "Unknown", "If you need to know")):
                    if cells and cells[0] not in ("Unknown", "If you need to know") and cells[-2:] != ["Source", "As of"]:
                        issues.append((name, f"§{title}: table header must end `| Source | As of |`"))
                    continue
                if len(cells) >= 5 and "elicit" not in t and cells[-2] in ("", "—"):
                    issues.append((name, f"§{title}: `{cells[0][:34]}` has no Source"))

    if issues:
        print(f"{len(issues)} issue(s):\n")
        cur = None
        for f, m in issues:
            if f != cur:
                cur = f; print(f"  {f}")
            print(f"      · {m}")
        print("\nConventions: _system/_profile-template.md · _system/profile-convention.md")
    else:
        print(f"All {len(profiles())} profiles conform.")
    sys.exit(1 if (issues and strict) else 0)

if __name__ == "__main__":
    main()
