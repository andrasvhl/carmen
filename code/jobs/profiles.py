"""Weekly: is the profile layer being filled in, or is it just a schema?

Seven concerns in one job. Most of it wraps tools that already exist -
`vt profiles`, `vt backlinks`, `vt people` - rather than reimplementing them,
so there is one definition of each rule.

The one that matters most is the elicit count. Every other check asks whether
a fact went stale; that one asks whether the layer was ever actually filled
in. A profile of empty fields is worse than no profile, because it looks like
coverage.

    ssh vault-host '~/automation/bin/run-job profiles --dry-run'
"""

import re
import subprocess
from pathlib import Path

from runner import config
from runner.job import Job, log, IMPORTANT, ROUTINE, YOU

REPO = Path(__file__).resolve().parent.parent

# Baselines recorded 2026-08-28. These should FALL. A flat count means the
# elicitation never happened.
ELICIT_BASELINE = 54
INFERRED_BASELINE = 3
# People notes are built to grow, so the aggregate rising is correct. Judge
# per-note emptiness instead.
PERSON_EMPTY_AFTER = 8


def main() -> int:
    with Job("profiles", description="Profile layer health",
             watches=("profile-prose-creep", "profile-template-conformance",
                      "profile-router-integrity",
                      "profile-elicit-count", "person-elicit-count",
                      "profile-inferred-count")) as job:

        _check_conventions(job)
        _check_router_integrity(job)
        _check_people(job)
        _check_elicit(job)
        _check_inferred(job)
        _check_person_notes(job)

        log(f"profiles: {len([c for c in job.checks if c.ok])}/{len(job.checks)} checks passed")
    return 0


def _vt(*args) -> tuple[int, str]:
    """Run a vault tool. One definition of each rule, not a reimplementation."""
    try:
        r = subprocess.run([str(REPO / "bin" / "vt"), *args],
                           capture_output=True, text=True, timeout=180)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as exc:
        return 1, f"{type(exc).__name__}: {exc}"


def _check_conventions(job: Job) -> None:
    """`vt profiles --strict` enforces prose caps, the required spine,
    Source/As of columns, sourced rows, frontmatter and stored credentials."""
    rc, out = _vt("profiles", "--strict")
    tail = out.strip().split("\n")[-1] if out else "(no output)"
    ok = rc == 0
    job.check("profiles conform to their template", ok=ok,
              tier=IMPORTANT, owner=YOU, key="profile-conventions",
              covers="profile-prose-creep",
              detail=f"`vt profiles --strict` exited {rc}: {tail[:200]}")
    # The same command covers the spine and column rules, so it answers the
    # template-conformance concern too rather than running twice.
    job.check("profile spine and columns intact", ok=ok,
              tier=IMPORTANT, owner=YOU, key="profile-template",
              covers="profile-template-conformance",
              detail=f"same run of `vt profiles --strict`: {tail[:200]}")
    job.record("profiles_conform", "yes" if ok else "no")


def _check_router_integrity(job: Job) -> None:
    """A sub-profile missing from the Master Profile router is unreachable; a
    router row with no file is a dead link. Both fail silently."""
    rc, out = _vt("backlinks", "Master Profile")
    linked = set(re.findall(r"^\s+(\S.*?\.md)\s", out, re.M))
    # "Course Archive" is excluded for the same reason inventory skips it: it is
    # machine-written by `vt canvas`, and a Canvas page called "LinkedIn
    # Profiles.md" matched *Profile*.md and was reported as a sub-profile that
    # does not link back to the router. The suggested fix -- add a Master
    # Profile backlink -- would have written into generated material that the
    # next `vt canvas` run overwrites. Found 2026-09-16, after the check had
    # been failing since at least 09-07.
    on_disk = {str(p.relative_to(config.VAULT))
               for p in (config.VAULT / "Areas").rglob("*Profile*.md")
               if p.name != "Master Profile.md"
               and "Course Archive" not in p.parts}
    missing = sorted(on_disk - linked)
    job.record("sub_profiles_on_disk", len(on_disk))
    job.check("every sub-profile links to the Master Profile", ok=not missing,
              tier=IMPORTANT, owner=YOU, key="router-integrity",
              covers="profile-router-integrity",
              detail=f"{len(missing)} profile(s) do not link back: "
                     f"{', '.join(missing[:4])} - unreachable from the router")


def _check_people(job: Job) -> None:
    """Records inner and close counts only. The size check (inner + close <= 12)
    was switched off 2026-09-27 at the owner's call, when he named 13 inner and 20
    close on purpose; `people-crm-circles` is therefore unwatched, not covered."""
    for circle in ("inner", "close"):
        rc, out = _vt("people", "--circle", circle)
        job.record(f"people_{circle}", len(re.findall(r"^\s{2}\S", out, re.M)))


def _count(pattern: str, *paths: str) -> int:
    total = 0
    for rel in paths:
        for p in (config.VAULT / rel).rglob("*.md") if (config.VAULT / rel).is_dir() \
                else [config.VAULT / rel]:
            if p.exists():
                total += len(re.findall(pattern, p.read_text(errors="ignore")))
    return total


def _check_elicit(job: Job) -> None:
    """Should fall over time. Flat means the layer is a schema, not a profile."""
    n = _count("— elicit", "Areas/Master Profile.md") + sum(
        len(re.findall("— elicit", p.read_text(errors="ignore")))
        for p in (config.VAULT / "Areas").rglob("*Profile*.md"))
    job.record("elicit_placeholders", n)
    job.record("elicit_baseline", ELICIT_BASELINE)
    job.check("elicit count is falling", ok=n <= ELICIT_BASELINE,
              tier=ROUTINE, owner=YOU, key="elicit-count",
              covers="profile-elicit-count",
              detail=f"{n} placeholders against a baseline of {ELICIT_BASELINE}. "
                     f"Rising means new gaps opened faster than they were filled")


def _check_inferred(job: Job) -> None:
    """An `inferred` fact is Claude's conclusion about its own user,
    unconfirmed. It reads as authoritative and is not."""
    n = sum(len(re.findall(r"\binferred\b", p.read_text(errors="ignore")))
            for p in (config.VAULT / "Areas").rglob("*.md"))
    job.record("inferred_rows", n)
    job.check("inferred facts are not accumulating", ok=n <= INFERRED_BASELINE + 2,
              tier=ROUTINE, owner=YOU, key="inferred-count",
              covers="profile-inferred-count",
              detail=f"{n} inferred rows against a baseline of {INFERRED_BASELINE}. "
                     f"Each should carry an Unknowns row until confirmed")


def _check_person_notes(job: Job) -> None:
    """Deliberately per-note, never a total: the people database is built to
    grow, so the aggregate rising is the system working."""
    people = sorted((config.VAULT / "Areas" / "People").glob("*.md"))
    stale = []
    for p in people:
        if p.name.startswith("_") or p.stem == "People":
            continue
        if len(re.findall("— elicit", p.read_text(errors="ignore"))) >= PERSON_EMPTY_AFTER:
            stale.append(p.stem)
    job.record("person_notes", len(people))
    job.record("person_notes_mostly_empty", len(stale))
    job.check("no person note is still mostly empty", ok=not stale,
              tier=ROUTINE, owner=YOU, key="person-elicit",
              covers="person-elicit-count",
              detail=f"{len(stale)} note(s) have {PERSON_EMPTY_AFTER}+ unfilled fields: "
                     f"{', '.join(stale[:5])}. New people starting empty is correct; "
                     f"staying empty for months is not")


if __name__ == "__main__":
    raise SystemExit(main())
