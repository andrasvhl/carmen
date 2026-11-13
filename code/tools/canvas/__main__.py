#!/usr/bin/env python3
"""Archive Stanford Canvas courses into the vault, verbatim and incrementally.

Replaces the browser-driven `canvas-export` skill, which could only run with a
Chrome window open, kept no state, and wrote a model-authored summary rather
than the content.

Setup, once:

  1. Mint a token at https://canvas.stanford.edu/profile/settings -> New Access
     Token. Stanford caps the lifetime at 90 days, so record the expiry.
  2. Add three lines to ~/.automation.env (mode 0600):

       CANVAS_HOST=canvas.stanford.edu
       CANVAS_TOKEN=<the token>
       CANVAS_TOKEN_EXPIRES=YYYY-MM-DD

Usage:
  python3 -m tools.canvas courses                 # enumerate, write nothing
  python3 -m tools.canvas plan                    # counts + bytes, no download
  python3 -m tools.canvas pull [--course ID]      # fetch, incremental
  python3 -m tools.canvas pull --dry-run          # what a pull would do
  python3 -m tools.canvas pull --from-cache       # re-derive notes locally, 0 requests
  python3 -m tools.canvas cache                   # what is cached, and how old it is
  python3 -m tools.canvas extract [--force]       # binaries -> text notes
  python3 -m tools.canvas links --plan            # outbound URLs by host, fetch nothing
  python3 -m tools.canvas links                   # fetch the readings a course links to
  python3 -m tools.canvas links --gated           # what needs your login, for a browser pass
  python3 -m tools.canvas adopt                   # match hand-saved PDFs to their readings
  python3 -m tools.canvas alias                   # module folder names, raw and pretty
  python3 -m tools.canvas mine                    # text beside your own coursework
  python3 -m tools.canvas order                   # teach flexplorer the Canvas order
  python3 -m tools.canvas status                  # what is archived, and staleness
  python3 -m tools.canvas fetch-files             # rsync binaries from vault-host

Or through the shared entry point, identical on the Mac and the box:

  vt canvas courses
"""

import argparse
import datetime as dt
import json
import pathlib
import shlex
import os
import subprocess
import sys

from runner import config

from . import (archive, cache as cache_mod, links as links_mod,
               order as order_mod, state as state_mod)
from .lock import Busy, canvas_lock
from .layout import course_dir, group_of, locate
from .api import CanvasError, Client, Unavailable

TOKEN_WARN_DAYS = 14


def _on_box() -> bool:
    """True when this is vault-host rather than the Mac.

    The two machines have different jobs here: the box pulls, the Mac is where
    the binaries are actually opened. Knowing which one we are on is what lets
    a pull end by telling you the one remaining step instead of leaving it to
    memory. `runner.config` already resolves the vault per machine.
    """
    return config.VAULT == config.HOME / "vault"


def _client(verbose: bool = False, *, record: bool = True,
            replay: bool = False) -> Client:
    env = config.load_env()
    token = env.get("CANVAS_TOKEN") or os.environ.get("CANVAS_TOKEN", "")
    host = env.get("CANVAS_HOST") or os.environ.get("CANVAS_HOST", "canvas.stanford.edu")
    if not token and not replay:
        sys.exit("CANVAS_TOKEN is not set. See the module docstring: mint a token at\n"
                 f"  https://{host}/profile/settings -> New Access Token\n"
                 f"then add CANVAS_HOST / CANVAS_TOKEN / CANVAS_TOKEN_EXPIRES to "
                 f"{config.ENV_FILE} (mode 0600).")
    if not replay:
        _warn_expiry(env)
    # A replay makes no requests, so it needs no live token - which is the
    # point at graduation, when the token dies and the archive must still be
    # re-derivable from what was recorded.
    return Client(host, token, verbose=verbose,
                  cache=cache_mod.Cache(record=record, replay=replay))


def _warn_expiry(env: dict[str, str]) -> None:
    """A silently expired token looks exactly like 'nothing changed'.

    Stanford caps access tokens at 90 days. An incremental run against a dead
    token would report zero new items and exit 0, which is the single most
    dangerous shape a job in this repo can have.
    """
    raw = env.get("CANVAS_TOKEN_EXPIRES", "").strip()
    if not raw:
        print("warning: CANVAS_TOKEN_EXPIRES is not set, so nothing can warn you "
              "before the 90-day token dies.", file=sys.stderr)
        return
    try:
        expires = dt.date.fromisoformat(raw)
    except ValueError:
        print(f"warning: CANVAS_TOKEN_EXPIRES={raw!r} is not an ISO date.", file=sys.stderr)
        return
    left = (expires - dt.date.today()).days
    if left < 0:
        sys.exit(f"CANVAS_TOKEN expired on {expires}. Mint a new one and update "
                 f"{config.ENV_FILE}; a run against a dead token reports 'no changes'.")
    if left <= TOKEN_WARN_DAYS:
        print(f"warning: Canvas token expires in {left} day(s), on {expires}.", file=sys.stderr)


def _select(client: Client, wanted: list[str] | None) -> list[dict]:
    return _pick(archive.list_courses(client), wanted)


def _pick(courses: list[dict], wanted: list[str] | None) -> list[dict]:
    if not wanted:
        return courses
    ids = {str(w) for w in wanted}
    picked = [c for c in courses if str(c["id"]) in ids]
    missing = ids - {str(c["id"]) for c in picked}
    if missing:
        sys.exit(f"no enrolment found for course id(s): {', '.join(sorted(missing))}")
    return picked


# ---- commands -------------------------------------------------------------

def cmd_courses(args) -> int:
    client = _client(args.verbose)
    courses = archive.list_courses(client)
    print(f"{len(courses)} course(s)\n")
    print(f"{'ID':>8}  {'Term':<24} {'Code':<20} Name")
    for c in courses:
        print(f"{c['id']:>8}  {archive.term_of(c)[:24]:<24} "
              f"{(c.get('course_code') or '')[:20]:<20} {c.get('name') or ''}")
    return 0


def cmd_plan(args) -> int:
    client = _client(args.verbose)
    courses = _select(client, args.course)
    total_bytes = total_files = 0
    print(f"{'ID':>8}  {'Files':>6} {'Size':>10}  {'Pages':>6} {'Assign':>6} {'Disc':>6}  Name")
    for c in courses:
        p = archive.plan_course(client, c)
        total_bytes += p["bytes"]
        total_files += p["files"]
        print(f"{c['id']:>8}  {p['files']:>6} {_human(p['bytes']):>10}  "
              f"{str(p['pages']):>6} {str(p['assignments']):>6} {str(p['discussions']):>6}  "
              f"{c.get('name') or ''}")
    print(f"\n{len(courses)} course(s), {total_files} file(s), {_human(total_bytes)} to download.")
    print(f"Binaries land in {archive.FILES_ROOT}, carried by Obsidian Sync "
          "(Plus, `unsupported` file types on, excluded on the phone only).")
    return 0


def cmd_pull(args) -> int:
    if args.from_cache and not _gate_cache(args):
        return 2
    client = _client(args.verbose, record=not args.no_cache, replay=args.from_cache)
    everything = archive.list_courses(client)
    courses = _pick(everything, args.course)
    data = state_mod.load()
    # Enrolment groups need the whole listing (a quarter splits if ANY of its
    # courses is waitlisted or dropped), so this runs over every enrolment even
    # when one course was asked for. It moves folders, never recreates them.
    homes = archive.homes(everything, archive.dropped(data, everything))
    blocked = _reconcile_homes(data, everything, homes, dry_run=args.dry_run)
    if not args.dry_run:
        state_mod.save(data)
    puller = archive.Puller(client, data, only=args.only, force=args.force,
                            dry_run=args.dry_run, verbose=args.verbose, homes=homes)
    for i, c in enumerate(courses, 1):
        print(f"[{i}/{len(courses)}] {c.get('name')} ({c['id']})")
        if str(c["id"]) in blocked:
            msg = f"course {c['id']}: {blocked[str(c['id'])]}"
            puller.problems.append(msg)
            print(f"  ! {blocked[str(c['id'])]}; skipping this course")
            continue
        if not args.dry_run:
            err = _follow_rename(data, c, homes.get(str(c["id"])))
            if err:
                puller.problems.append(f"course {c['id']}: {err}")
                print(f"  ! {err}; skipping this course")
                continue
        try:
            puller.pull(c)
        except CanvasError as e:
            puller.problems.append(f"course {c['id']}: {e}")
            print(f"  ! {e}")
        if args.prune and not args.dry_run:
            folder = archive.course_folder(c)
            gone = archive.prune(course_dir(archive.ROOT, folder, homes.get(str(c["id"]))),
                                 archive.FILES_ROOT / folder,
                                 puller.seen, puller.known)
            if gone:
                print(f"  pruned {len(gone)} orphan(s)")
                for g in gone[:5]:
                    print(f"    - {g.split('Course Archive/')[-1]}")
                if len(gone) > 5:
                    print(f"    ... and {len(gone) - 5} more")
        if not args.dry_run:
            state_mod.save(data)   # checkpoint per course, not at the end

    verb = "would write" if args.dry_run else "wrote"
    source = (f"{client.cache.hits} cached response(s), 0 API request(s)"
              if args.from_cache else f"{client.requests} API request(s)")
    print(f"\n{verb} {puller.written} note(s), {puller.unchanged} unchanged, "
          f"{puller.downloaded} file(s) {'would be ' if args.dry_run else ''}downloaded "
          f"({_human(puller.bytes)}), {source}.")
    if args.from_cache:
        # Said again at the end, because the age printed before the run has
        # scrolled off by now and this is the line that gets pasted into a note.
        print(f"REPLAY: notes were re-derived from the API cache recorded "
              f"{args.cache_age}. Canvas was not asked, so nothing here reflects "
              "any change made upstream since then.")
    if puller.problems:
        print(f"\n{len(puller.problems)} problem(s):")
        for p in puller.problems[:20]:
            print(f"  - {p}")
    if not args.dry_run:
        print("\nNext: `vt canvas extract` to turn the binaries into searchable text,"
              "\n      then `vt canvas links` for the readings the course points at.")
        if _on_box() and puller.downloaded:
            print(f"\nObsidian Sync carries all of it, the {puller.downloaded} downloaded "
                  "file(s) included, provided `unsupported` is in the sync file-type list "
                  "and you are on Sync Plus. Nothing to copy by hand.")
    return 0


def _gate_cache(args) -> bool:
    """Print what the cache holds, and refuse to replay it if it is stale.

    This is the whole safety story of `--from-cache`, and it runs before a
    client exists so a stale cache costs nothing. The failure it exists to
    prevent is this component's signature one: a run that regenerates notes
    from old content and reports success, indistinguishable from a run that
    found nothing to change.
    """
    only = [str(c) for c in args.course] if args.course else None
    view = cache_mod.survey(courses=only)
    if only:
        # `_shared` is always included, so "some entries exist" is not the same
        # as "this course was ever recorded". Say which one is missing here
        # rather than letting the replay get partway in and then hit a Miss.
        never = [c for c in only if c not in view["courses"]]
        if never:
            print(f"refusing to replay: no cached responses for course(s) "
                  f"{', '.join(never)}.\nRun `vt canvas pull --course "
                  f"{never[0]}` once live to record them.", file=sys.stderr)
            return False
    if view["entries"]:
        oldest, newest = view["oldest"], view["newest"]
        span = (f"{oldest:%Y-%m-%d %H:%M}" if oldest else "?")
        if newest and oldest and newest.date() != oldest.date():
            span += f" to {newest:%Y-%m-%d %H:%M}"
        age = cache_mod.age_days(oldest)
        args.cache_age = f"{span} UTC ({age:.1f} days old)" if age is not None else span
        print(f"Replaying {view['entries']} cached response(s) across "
              f"{len(view['courses'])} course director(ies), {_human(view['bytes'])}.")
        print(f"Recorded {args.cache_age}. Canvas will not be contacted.\n")
    else:
        args.cache_age = "never"
    problem = cache_mod.check_age(view, args.max_age)
    if problem:
        print(f"refusing to replay: {problem}", file=sys.stderr)
        return False
    if args.prune:
        print("--prune is on during a replay: notes will be deleted on the "
              "authority of cached responses, not live ones. That is only safe "
              "because the age check above passed.\n")
    return True


def cmd_cache(args) -> int:
    """What the API cache holds, and how old it is.

    Age is the only number that matters here, so it is what this sorts by and
    leads with. Oldest first, because a replay is only as good as its stalest
    entry.
    """
    view = cache_mod.survey()
    if not view["entries"]:
        print(f"nothing cached yet at {cache_mod.CACHE_DIR}.\n"
              "The cache fills itself during an ordinary `vt canvas pull`.")
        return 0
    names = {cid: rec.get("name", "") for cid, rec in state_mod.load()["courses"].items()}
    names.setdefault("_shared", "")
    names["_shared"] = "(enrolment list and file metadata)"
    floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)

    print(f"{'Course':>8}  {'Entries':>7} {'Size':>9}  {'Age':>7}  {'Recorded':<17}  Name")
    for cid, rec in sorted(view["courses"].items(),
                           key=lambda kv: kv[1]["oldest"] or floor):
        age = cache_mod.age_days(rec["oldest"])
        age_s = f"{age:.1f}d" if age is not None else "?"
        when = f"{rec['oldest']:%Y-%m-%d %H:%M}" if rec["oldest"] else "?"
        print(f"{cid:>8}  {rec['entries']:>7} {_human(rec['bytes']):>9}  "
              f"{age_s:>7}  {when:<17}  {names.get(cid, '')}")

    oldest_age = cache_mod.age_days(view["oldest"])
    tail = f", oldest {oldest_age:.1f} days old" if oldest_age is not None else ""
    print(f"\n{view['entries']} entry(ies), {_human(view['bytes'])}{tail}.")
    print(f"cache: {cache_mod.CACHE_DIR}")
    print(f"\n`vt canvas pull --from-cache` replays these and refuses anything past "
          f"{cache_mod.DEFAULT_MAX_AGE_DAYS} days old.")
    return 0


def cmd_extract(args) -> int:
    data = state_mod.load()
    # Adoption used to happen inside run_extract. It is a reading concern, not a
    # binary one, so it lives in links now - but `extract` still triggers it,
    # because "drop the file, run extract" is the documented muscle memory.
    if not args.course:
        adopted = links_mod.adopt(archive.ROOT, data, verbose=args.verbose)
        for m in adopted["matched"]:
            print(f"adopted {m['file']} -> {m['title']}")
        for u in adopted["unmatched"]:
            print(f"not adopted: {u['file']} — {u['reason']}")
    filt = {str(c) for c in args.course} if args.course else None
    tally = archive.run_extract(data, course_filter=filt, force=args.force,
                                reparse=args.reparse, ocr=args.ocr, verbose=args.verbose)
    state_mod.save(data)
    if not tally:
        print("nothing to extract — run `vt canvas pull` first.")
        return 0
    for status, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"{n:>6}  {status}")
    if tally.get("no-text-layer"):
        print(f"\n{tally['no-text-layer']} file(s) have no text layer (scans). "
              "Their notes say so rather than being silently empty."
              + ("" if args.ocr else " `--ocr` reads them."))
    missing = [s for s in tally if s.startswith("missing:")]
    if missing:
        print("\nInstall the missing parsers:\n"
              "  ~/automation/.venv/bin/pip install -r ~/automation/requirements.txt")
    return 0


def cmd_mine(args) -> int:
    """Extract text beside every file in a `_mine/` folder.

    His own coursework, which Canvas never held and which therefore exists
    nowhere else. Kept out of the tool's file records on purpose: that is where
    `prune` gets its whitelist, so recording these would make them deletable.
    """
    data = state_mod.load()
    tally = archive.run_extract_mine(data, force=args.force, verbose=args.verbose)
    # `canvas.json` is not locked and every save rewrites the whole file, so a
    # long-running pass here would discard whatever another session wrote while
    # it ran. This pass owns exactly one key, so re-read immediately before
    # saving and carry only that key across. It does not make the file safe for
    # concurrent writers in general - it makes THIS command safe to run while
    # someone else is retrieving readings.
    fresh = state_mod.load()
    fresh["mine"] = data.get("mine", {})
    state_mod.save(fresh)
    dirs = archive.mine_dirs()
    if not tally:
        print(f"nothing to extract across {len(dirs)} folder(s).")
        return 0
    for status, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"{n:>6}  {status}")
    print(f"\n{len(dirs)} `_mine/` folder(s). The text sits beside each file as "
          "`<name>.<ext>.md`,\nso the archive can answer from your own work the same "
          "way it answers from Canvas's.")
    return 0


def _rewrite_state_paths(data: dict, swaps: list[tuple[str, str]]) -> None:
    """Rewrite every stored path under `old` to sit under `new`, in place.

    State holds absolute and vault-relative note paths, so a moved folder has to
    be followed here or the next pull treats every note as missing and prune
    loses track of what the tool wrote. `old + "/"` (or an exact tail) only, so
    one course name that prefixes another can never be touched.
    """
    def fix(v):
        if isinstance(v, str):
            for old, new in swaps:
                if (old + "/") in v:
                    v = v.replace(old + "/", new + "/")
                if v.endswith(old):
                    v = v[: -len(old)] + new
            return v
        if isinstance(v, dict):
            return {fix(k) if isinstance(k, str) and "/" in k else k: fix(x)
                    for k, x in v.items()}
        if isinstance(v, list):
            return [fix(x) for x in v]
        return v
    data["courses"] = fix(data["courses"])
    data["readings"] = fix(data.get("readings", {}))


def _vt_move(src: pathlib.Path, dst: pathlib.Path) -> str | None:
    """`vt move` one folder, so every link to it is rewritten. None on success."""
    vault = config.VAULT
    vt = pathlib.Path(__file__).resolve().parents[2] / "bin" / "vt"
    r = subprocess.run([str(vt), "move", "--no-git",
                        str(src.relative_to(vault)), str(dst.relative_to(vault))],
                       capture_output=True, text=True)
    if r.returncode != 0 or not dst.is_dir():
        return r.stderr.strip() or r.stdout.strip() or "move failed"
    return None


def _reconcile_homes(data: dict, listing: list[dict], homes: dict[str, str | None],
                     *, dry_run: bool) -> dict[str, str]:
    """Move each listed course into its enrolment group, and record the status.

    Returns {course id: reason} for any course that could not be put where it
    belongs, which the pull must then skip: pulling it would write a second
    copy at the new path. A course whose folder does not exist yet, or exists
    only under a previous name, is left to the pull and `_follow_rename`.

    Same machinery as a rename: `vt move`, so his own notes and every link to
    the course go with it, then the stored paths follow.
    """
    root, vault = archive.ROOT, config.VAULT
    blocked: dict[str, str] = {}
    left: set[pathlib.Path] = set()
    # Dropped (or a waitlist he left): Canvas stops listing the course at all.
    # A current-quarter one goes to Dropped/ (2026-09-28); its archive is only
    # moved, never pruned or deleted. Older quarters are left alone.
    gone = archive.dropped(data, listing)
    todo = [(str(c["id"]), archive.course_folder(c), archive.enrollment_of(c))
            for c in listing]
    todo += [(cid, folder, "dropped") for cid, folder in gone.items()]
    for cid, folder, status in todo:
        rec = data.get("courses", {}).get(cid)
        if rec is not None and status:
            was = rec.get("enrollment")
            if was and was != status:
                print(f"enrolment changed: {folder}: {was} -> {status}")
            if not dry_run:
                rec["enrollment"] = status
        src = locate(root, folder)
        dst = course_dir(root, folder, homes.get(cid))
        if not src.is_dir() or src == dst:
            continue
        if dst.exists():
            blocked[cid] = (f"both {src.relative_to(vault)} and "
                            f"{dst.relative_to(vault)} exist; merge by hand")
            print(f"! {blocked[cid]}")
            continue
        print(f"{'would move' if dry_run else 'move'}  {src.relative_to(root)}\n"
              f"      -> {dst.relative_to(root)}")
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        err = _vt_move(src, dst)
        if err:
            blocked[cid] = f"could not move to {dst.relative_to(vault)}: {err}"
            print(f"! {blocked[cid]}")
            continue
        # Per move, so state matches the disk even if a later move fails.
        _rewrite_state_paths(data, [(str(src.relative_to(vault)),
                                     str(dst.relative_to(vault)))])
        if group_of(src):
            left.add(src.parent)
    # A quarter that folded back leaves empty Enrolled/ or Waitlisted/ behind.
    # rmdir only ever removes an EMPTY directory; anything in it keeps it.
    for d in left:
        try:
            d.rmdir()
        except OSError:
            pass
    # A course from an older quarter that Canvas no longer lists: not a drop,
    # so it is not relabelled. Nothing of his is deleted: say so, once a pull.
    listed = {str(c["id"]) for c in listing}
    for cid, rec in data.get("courses", {}).items():
        if rec.get("enrollment") and cid not in listed and cid not in gone:
            print(f"note: {rec.get('folder')} ({cid}) was {rec['enrollment']} and is no "
                  f"longer listed on Canvas; its folder is left where it is.")
    return blocked


def _follow_rename(data: dict, course: dict, group: str | None) -> str | None:
    """A course's folder name changed on Canvas: move its folders, don't recreate.

    This used to pull into the new name and then `rmtree` the old folder, which
    deleted anything of the owner's inside it - the one case where his own notes
    were not safe. Moving keeps them, and the whitelist prune afterwards removes
    only what the tool itself wrote and no longer produces.
    """
    rec = data.get("courses", {}).get(str(course["id"])) or {}
    previous, folder = rec.get("folder"), archive.course_folder(course)
    if not previous or previous == folder:
        return None
    vault = config.VAULT
    swaps = []
    src, dst = locate(archive.ROOT, previous), course_dir(archive.ROOT, folder, group)
    if src.is_dir() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        err = _vt_move(src, dst)
        if err:
            return f"could not move {previous!r} to {folder!r}: {err}"
        swaps.append((str(src.relative_to(vault)), str(dst.relative_to(vault))))
    fsrc, fdst = archive.FILES_ROOT / previous, archive.FILES_ROOT / folder
    if fsrc.is_dir() and not fdst.exists():
        os.rename(fsrc, fdst)  # binaries: nothing links to these paths but state
        swaps.append((str(fsrc.relative_to(vault)), str(fdst.relative_to(vault))))
    _rewrite_state_paths(data, swaps)
    # The rewrite rebuilds data["courses"], so `rec` is a stale copy by now.
    data["courses"][str(course["id"])]["folder"] = folder
    print(f"  followed a rename: {previous!r} -> {folder!r}")
    return None


def cmd_refresh(args) -> int:
    """The whole refresh in one command: pull, extract, links, worklist.

    Five commands in a fixed order, one of which (extract) could never be
    skipped, was the documented procedure and the thing that went wrong: a
    step forgotten, or two run at once and one's state save lost. This runs
    them in sequence, for one set of courses, and stops at the first failure.
    `order` is the one step it cannot do - it writes `.obsidian/`, which does
    not reach vault-host - so it ends by saying so.
    """
    from .layout import group_key, quarter_of
    client = _client(args.verbose)
    everything = archive.list_courses(client)
    if args.course:
        picked = _select(client, args.course)
    elif args.all:
        picked = everything
    else:
        # The current quarter: the newest term among the enrolments, read
        # from Canvas rather than state, so a course added since the last
        # run (CS 351Z was) is included without being named.
        by_q = {}
        for c in everything:
            by_q.setdefault(quarter_of(archive.course_folder(c)), []).append(c)
        terms = sorted((q for q in by_q if q != "Program"), key=group_key)
        if not terms:
            print("no current-quarter courses found; pass --course or --all.")
            return 1
        picked = by_q[terms[0]]
        print(f"refreshing {terms[0]}: {len(picked)} course(s)")
    ids = [str(c["id"]) for c in picked]
    g = ["-v"] if args.verbose else []

    def step(name: str, argv: list[str]) -> bool:
        print(f"\n== {name}")
        rc = main(g + argv)
        if rc:
            print(f"\nstopped: `{' '.join(argv[:1])}` exited {rc}. Nothing after it ran.")
        return rc == 0

    pull = ["pull", "--prune"] + [a for i in ids for a in ("--course", i)]
    if not step("pull", pull):
        return 1
    if not step("extract", ["extract"] + (["--ocr"] if not args.no_ocr else [])):
        return 1
    # Folder names after the pull, which may just have followed a rename.
    data = state_mod.load()
    folders = [data["courses"][i]["folder"] for i in ids
               if data["courses"].get(i, {}).get("folder")]
    course_args = [a for f in folders for a in ("--course", f)]
    if not step("links", ["links", "--prune"] + course_args):
        return 1
    step("gated", ["links", "--gated"] + course_args)
    print("\nLast step, on the Mac: `vt canvas order`, then Cmd+R in Obsidian.")
    return 0


def cmd_regroup(args) -> int:
    """Put every course folder where the layout says it belongs. Writes no notes.

    Two jobs, both moves only: a course still flat from before the quarter
    folders goes into its quarter (the 2026-09-23 one-off), and a listed
    course goes into its enrolment group - `Fall 2026/Enrolled/`,
    `Waitlisted/`, or `Dropped/` for a current-quarter course Canvas no longer
    lists - which asks Canvas for the enrolment list and nothing else.
    Every pull does the second job too; this exists to do it on its own, with
    a dry run, and without touching any note's content.

    Each move is a `vt move`, so every wikilink to the course in the rest of the
    vault is rewritten with it, and state paths are rewritten to match -
    otherwise the next pull would treat every note as missing and `--prune`
    would see the moved copies as strangers. Binaries under `_files/` do not
    move.
    """
    root = archive.ROOT
    vault = config.VAULT
    client = _client(args.verbose)
    listing = archive.list_courses(client)
    data = state_mod.load()
    homes = archive.homes(listing, archive.dropped(data, listing))
    listed = {archive.course_folder(c) for c in listing}

    blocked = _reconcile_homes(data, listing, homes, dry_run=args.dry_run)

    # Flat leftovers Canvas no longer lists: quarter folder, unsplit.
    from .layout import course_dirs
    moves = []
    for d in course_dirs(root):
        if d.parent != root or d.name in listed:
            continue
        dst = course_dir(root, d.name, None)
        if dst.exists():
            print(f"skip {d.name!r}: {dst.relative_to(vault)} already exists")
            continue
        moves.append((d, dst))
    for d, dst in moves:
        print(f"{'would move' if args.dry_run else 'move'}  {d.name}\n    -> {dst.parent.name}/")
    if args.dry_run:
        return 0

    done = []
    for d, dst in moves:
        err = _vt_move(d, dst)
        if err:
            print(f"FAILED moving {d.name}: {err}")
            break
        done.append((d, dst))
    _rewrite_state_paths(data, [(str(d.relative_to(vault)), str(dst.relative_to(vault)))
                                for d, dst in done])
    state_mod.save(data)
    print(f"\nstate saved; {len(blocked)} course(s) could not be placed.")
    print("Next, on the Mac: `vt canvas order`, then Cmd+R.")
    return 0 if not blocked and len(done) == len(moves) else 1


def cmd_alias(args) -> int:
    """Show, or set, the pretty folder name for each of a course's modules.

    Canvas module names are freeform per instructor, so a code rule that handles
    one convention keeps meeting new ones. The map is the supported way to
    rename a module folder; renaming one in Finder is not, and is destructive -
    see `archive.Puller._relocate_modules`.

    `--load` takes the JSON a model wrote after reading the module list, which
    is the whole intended workflow: propose once per course, review, store,
    and let the next pull relocate the folders itself.
    """
    data = state_mod.load()
    courses = data.get("courses", {})
    if not courses:
        print("nothing archived yet — run `vt canvas pull` first.")
        return 0

    if args.load:
        try:
            incoming = json.loads(open(args.load, encoding="utf-8").read())
        except (OSError, ValueError) as e:
            sys.exit(f"could not read {args.load}: {e}")
        if not isinstance(incoming, dict):
            sys.exit("expected {course id: {raw module name: folder name}}")
        changed = 0
        for cid, mapping in incoming.items():
            rec = courses.get(str(cid))
            if rec is None:
                sys.exit(f"no archived course with id {cid}")
            if not isinstance(mapping, dict):
                sys.exit(f"course {cid}: expected an object of module -> name")
            known = set(rec.get("module_dirs", {})) | _raw_modules(rec)
            for raw, pretty in mapping.items():
                if known and raw not in known:
                    # A typo in a raw name would store an entry that matches
                    # nothing and silently do nothing, which is the failure
                    # shape this component keeps producing.
                    sys.exit(f"course {cid}: {raw!r} is not a module of this "
                             f"course. Run `vt canvas alias --course {cid}` to "
                             f"see the exact names.")
                if not str(pretty).strip():
                    sys.exit(f"course {cid}: {raw!r} was given an empty name")
            rec.setdefault("module_aliases", {}).update(
                {k: str(v).strip() for k, v in mapping.items()})
            changed += len(mapping)
        state_mod.save(data)
        print(f"stored {changed} alias(es) across {len(incoming)} course(s).")
        print("\nNothing has moved yet. The next pull relocates the folders and "
              "fixes every\nstored path in step:")
        print("  vt canvas pull --course <id> --from-cache")
        print("then `vt canvas order` on the Mac, and Cmd+R.")
        return 0

    wanted = {str(c) for c in args.course} if args.course else None
    for cid, rec in sorted(courses.items(), key=lambda kv: kv[1].get("name", "")):
        if wanted and cid not in wanted:
            continue
        raws = sorted(set(rec.get("module_dirs", {})) | _raw_modules(rec))
        if not raws:
            continue
        aliases = rec.get("module_aliases") or {}
        print(f"\n{cid}  {rec.get('name', '')}")
        for raw in raws:
            mark = "*" if raw in aliases else " "
            print(f"  {mark} {archive.module_dir(raw, aliases):<52}  {raw}")
    print("\n  * = aliased. The right-hand column is the raw Canvas name, which "
          "is the key to set.")
    print("  Set them with: vt canvas alias --load <file.json>   "
          "# {course id: {raw: folder name}}")
    return 0


def _raw_modules(rec: dict) -> set[str]:
    """Raw Canvas module names this course is known to have.

    From the file records, which store the raw name against each binary. It is
    the only place state keeps them, and it is enough to name the modules
    without asking Canvas again.
    """
    return {f.get("module") for f in rec.get("files", {}).values() if f.get("module")}


def cmd_adopt(args) -> int:
    """Adopt the PDFs saved by hand into `_manual/`, matching each to its reading.

    Prints the matching before it is trusted. Every silent-success failure this
    component has produced was caught by someone reading output, and a wrong
    match here would file a case under the wrong reading while reporting a
    clean run.
    """
    root = archive.ROOT
    if not root.exists():
        print("nothing archived yet — run `vt canvas pull` first.")
        return 0
    data = state_mod.load()

    if args.assign:
        url, file = args.assign
        res = links_mod.adopt_as(root, data, url, file, dry_run=args.dry_run)
        if "error" in res:
            print(f"not assigned: {res['error']}")
            return 1
        print(f"{'would assign' if args.dry_run else 'assigned'} {res['file']}\n"
              f"    -> {res['title']}\n       {res['course']}\n"
              f"       {res['url']}\n       {res['chars']:,} chars, matched by hand")
        if not args.dry_run:
            state_mod.save(data)
        return 0

    made = links_mod.ensure_dropzones(root, data, dry_run=args.dry_run)
    if made:
        print(f"{'would create' if args.dry_run else 'created'} "
              f"{len(made)} missing drop folder(s):")
        for c in made:
            print(f"  {archive.FILES_ROOT}/{c}/_manual/")
        print()

    if args.source:
        src = pathlib.Path(os.path.expanduser(args.source))
        if not src.is_dir():
            sys.exit(f"{src} is not a directory")
        staged = links_mod.adopt_from(root, data, src, dry_run=args.dry_run,
                                      verbose=args.verbose)
        if not args.dry_run:
            state_mod.save(data)
        verb = "would file" if args.dry_run else "filed"
        if staged["placed"]:
            print(f"{verb} {len(staged['placed'])} file(s) from {src}:\n")
            for m in staged["placed"]:
                print(f"  {m['file']}")
                print(f"    -> {m['title']}")
                print(f"       {m['course']}")
                print(f"       {m['score']:.2f} confidence · {m['why']}")
        if staged["skipped"]:
            print(f"\n{len(staged['skipped'])} file(s) left where they are:\n")
            for u in staged["skipped"]:
                print(f"  {u['file']}\n    {u['reason']}")
        if not staged["scanned"]:
            print(f"nothing to file in {src}.")
            return 0
        if args.dry_run:
            return 0
        print("\nNow adopting them into their readings.\n")

    result = links_mod.adopt(root, data, dry_run=args.dry_run, verbose=args.verbose)
    if not args.dry_run:
        state_mod.save(data)

    if not result["scanned"]:
        print("no new files in any `_manual/` folder.\n"
              "Save the gated readings from a signed-in browser into the folders above; "
              "the filename does not matter.")
        return 0

    verb = "would adopt" if args.dry_run else "adopted"
    if result["matched"]:
        print(f"{verb} {len(result['matched'])} reading(s):\n")
        for m in result["matched"]:
            print(f"  {m['file']}")
            print(f"    -> {m['title']}")
            print(f"       {m['course']}")
            print(f"       {m['score']:.2f} confidence · {m['why']} · {m['chars']:,} chars")
    if result["unmatched"]:
        print(f"\n{len(result['unmatched'])} file(s) not adopted:\n")
        for u in result["unmatched"]:
            print(f"  {u['file']}\n    {u['course']}\n    {u['reason']}")
        print("\nNothing was guessed. A file that matches nothing stays where it is; "
              "re-run after\nchecking it is the reading you meant to save.")
    if result["matched"] and not args.dry_run:
        print("\nThe text is now in the reading's own note, in the session that "
              "assigned it.\nRun `vt canvas links --gated` to see what is left.")
    return 0


def cmd_links(args) -> int:
    """Fetch the readings a course points at, off Canvas.

    A separate pass from `pull` on purpose: one course links out ~50 times, so
    this is thousands of requests against other people's servers and needs its
    own rate limiting, its own plan step, and its own state.
    """
    root = archive.ROOT
    if not root.exists():
        print("nothing archived yet — run `vt canvas pull` first.")
        return 0
    filt = {c for c in args.course} if args.course else None

    if args.gated:
        rows = links_mod.gated(state_mod.load(),
                               set(args.course) if args.course else None)
        if not rows:
            print("nothing gated — either nothing has been fetched yet, or everything came through.")
            return 0
        print(f"{len(rows)} reading(s) need a logged-in browser:\n")
        course = None
        for r in rows:
            here = r["courses"][0] if r["courses"] else "(no course)"
            if here != course:
                course = here
                print(f"\n  {course}")
            print(f"    {r['status']:16s} {r['title'][:50]:<50} {r['url']}")
        print("\nOpen these in Chrome where you are already signed in, save each as PDF into")
        print(f"  {archive.FILES_ROOT}/<Course>/_manual/")
        print("then re-run `vt canvas extract`.")
        return 0

    result = links_mod.run(root, course_filter=filt, force=args.force,
                           limit=args.limit, delay=args.delay,
                           plan_only=args.plan, prune=args.prune,
                           verbose=args.verbose)
    if "error" in result:
        sys.exit(result["error"])
    if result.get("plan"):
        print(f"{result['unique']} unique outbound URL(s)\n")
        for bucket, n in sorted(result["buckets"].items(), key=lambda kv: -kv[1]):
            print(f"{n:>6}  {bucket}")
        print("\ntop hosts:")
        for host, n in sorted(result["hosts"].items(), key=lambda kv: -kv[1])[:20]:
            print(f"{n:>6}  {host}")
        print(f"\nAt {args.delay}s between requests to a host, a full fetch is roughly "
              f"{result['buckets'].get('fetch', 0) * args.delay / 60:.0f} minute(s).")
        return 0

    print(f"{result['unique']} unique URL(s), {result['requests']} request(s) made\n")
    for status, n in sorted(result["tally"].items(), key=lambda kv: -kv[1]):
        if status != "gated":
            print(f"{n:>6}  {status}")
    if result["tally"].get("gated"):
        print(f"\n{result['tally']['gated']} reading(s) could not be archived without your "
              "login. None of them stored a paywall stub as if it were the article.")
        print("See `vt canvas links --gated` for the worklist.")
    return 0


def cmd_order(args) -> int:
    """Write Canvas's own ordering into flexplorer.

    The vault forbids numeric filename prefixes for ordering, so the order has
    to go into the plugin. Reads it from each course's Modules.md rather than
    the state file, because markdown syncs and state does not.
    """
    orders, pins = order_mod.plan_orders(archive.ROOT)
    if not orders:
        print("nothing archived yet.")
        return 0
    result = order_mod.apply(orders, pins, dry_run=args.dry_run)
    if "error" in result:
        sys.exit(result["error"])
    verb = "would set" if args.dry_run else "set"
    print(f"{verb} order on {result['folders']} folder(s) "
          f"({result['changed']} changed), pinned {result['pinned']} course anchor(s).")
    print(f"flexplorer now holds {result['total_entries']} entries.")
    if result["backup"]:
        print(f"backup: {result['backup']}")
        print("\nReload Obsidian (Cmd+R) or the running plugin overwrites this "
              "from memory.\nThe iPhone picks it up via the installed-community-plugins "
              "sync toggle.")
    return 0


def cmd_status(args) -> int:
    data = state_mod.load()
    courses = data.get("courses", {})
    if not courses:
        print("nothing archived yet.")
        return 0
    print(f"{'ID':>8}  {'Notes':>6} {'Files':>6} {'Text':>6}  {'Last pull':<21} Course")
    for cid, rec in sorted(courses.items(), key=lambda kv: kv[1].get("name", "")):
        files = rec.get("files", {})
        done = sum(1 for f in files.values() if f.get("extracted") == "ok")
        print(f"{cid:>8}  {len(rec.get('items', {})):>6} {len(files):>6} {done:>6}  "
              f"{(rec.get('last_pull') or '—')[:19]:<21} {rec.get('name', '')}")
    stale = [rec for rec in courses.values()
             if any(v != "ok" and v != "empty" for v in rec.get("sections", {}).values())]
    if stale:
        print(f"\n{len(stale)} course(s) have a section that was unavailable — "
              "see each course's `_sync-log.md`.")
    print(f"\nstate: {state_mod.STATE_FILE}")
    return 0


def cmd_fetch_files(args) -> int:
    """Bootstrap or fallback. Obsidian Sync normally carries these.

    Not part of the normal run. Sync Plus with `unsupported` in the file-type
    list moves the binaries along with everything else; this exists for seeding
    a machine quickly, or for when Sync is down. It only ever adds.
    """
    if _on_box():
        sys.exit("This is vault-host, which already has the files. Run this on the Mac.")
    # "Course Archive" has a space and the remote half of an rsync path is
    # expanded by the REMOTE shell, so it has to be quoted for that shell.
    # --protect-args would do this, but macOS ships openrsync, which has no
    # such flag; quoting works on both.
    remote = "vault/Areas/MBA/Course Archive/_files/"
    src = f"{args.host}:{shlex.quote(remote)}"
    dest = str(archive.FILES_ROOT) + "/"

    # Ask first, so "nothing has been pulled yet" reads as that rather than as
    # a wall of rsync errors about a missing directory.
    probe = subprocess.run(["ssh", args.host, f"test -d {shlex.quote(remote)}"],
                           capture_output=True)
    if probe.returncode != 0:
        print(f"{args.host} has no archive yet at ~/{remote}\n"
              "Run `vt canvas pull` on the box first; there is nothing to fetch.")
        return 0

    os.makedirs(dest, exist_ok=True)
    # No --delete: this only ever adds. Removing a file here because the box
    # no longer has it would be the archive quietly losing material, which is
    # the opposite of the point.
    cmd = (["rsync", "-avh", "--partial"]
           + (["--dry-run"] if args.dry_run else []) + [src, dest])
    print(" ".join(cmd), flush=True)
    rc = subprocess.call(cmd)
    if rc == 0 and not args.dry_run:
        print(f"\nBinaries are now at {dest}")
        print("Embeds in the Materials notes will render on this machine from here on.")
    return rc


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


# Commands that never write canvas.json or the archive. Everything else takes the
# Canvas lock (tools/canvas/lock.py), because a second writer silently discards
# the first one's state save.
_READ_ONLY = {"courses", "plan", "status", "cache", "order", "fetch-files"}


def _writes_state(args) -> bool:
    if args.cmd in _READ_ONLY or getattr(args, "dry_run", False):
        return False
    if args.cmd == "links" and (getattr(args, "plan", False) or getattr(args, "gated", False)):
        return False
    if args.cmd == "alias" and not getattr(args, "load", None):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vt canvas", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("courses", help="list every enrolment, write nothing").set_defaults(fn=cmd_courses)

    p = sub.add_parser("plan", help="counts and bytes per course, download nothing")
    p.add_argument("--course", action="append", help="course id (repeatable)")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("pull", help="fetch content and binaries into the vault")
    p.add_argument("--course", action="append", help="course id (repeatable)")
    p.add_argument("--only", action="append", choices=archive.SECTIONS,
                   help="restrict to one section (repeatable)")
    p.add_argument("--force", action="store_true", help="ignore state, rewrite everything")
    p.add_argument("--prune", action="store_true",
                   help="delete notes and files this run did not produce "
                        "(never touches Readings/ or _manual/)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--from-cache", action="store_true",
                   help="replay recorded API responses instead of fetching; makes "
                        "no requests and needs no live token")
    p.add_argument("--max-age", type=float, default=cache_mod.DEFAULT_MAX_AGE_DAYS,
                   metavar="DAYS",
                   help=f"how stale a cached response may be before --from-cache "
                        f"refuses (default {cache_mod.DEFAULT_MAX_AGE_DAYS})")
    p.add_argument("--no-cache", action="store_true",
                   help="do not record this run's responses")
    p.set_defaults(fn=cmd_pull, cache_age="never")

    p = sub.add_parser("extract", help="turn downloaded binaries into text notes")
    p.add_argument("--course", action="append")
    p.add_argument("--force", action="store_true",
                   help="rewrite every note, re-deriving text from the parse cache")
    p.add_argument("--reparse", action="store_true",
                   help="ignore the PDF parse cache and run pypdf again "
                        "(only needed when the parser itself changed)")
    p.add_argument("--ocr", action="store_true",
                   help="OCR scanned PDFs (no-text-layer) with tesseract; slow, cached")
    p.set_defaults(fn=cmd_extract)

    p = sub.add_parser("mine",
                       help="extract text beside your own files in each course's _mine/")
    p.add_argument("--force", action="store_true", help="rewrite every note")
    p.set_defaults(fn=cmd_mine)

    p = sub.add_parser("refresh",
                       help="pull, extract (with OCR), links, gated - for the current "
                            "quarter, or --course / --all")
    p.add_argument("--course", action="append", help="course id (repeatable)")
    p.add_argument("--all", action="store_true", help="every enrolment, past quarters too")
    p.add_argument("--no-ocr", action="store_true", help="skip OCR of new scans")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("regroup",
                       help="move course folders into their quarter and enrolment group; no notes written")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_regroup)

    p = sub.add_parser("alias",
                       help="show or set the folder name for each of a course's modules")
    p.add_argument("--course", action="append", help="course id (repeatable)")
    p.add_argument("--load", metavar="FILE",
                   help="store a JSON map of {course id: {raw module name: folder name}}")
    p.set_defaults(fn=cmd_alias)

    p = sub.add_parser("adopt",
                       help="match PDFs saved by hand in _manual/ to the readings they answer")
    p.add_argument("--dry-run", action="store_true",
                   help="show the matching without writing anything")
    p.add_argument("--from", dest="source", metavar="DIR",
                   help="first sort loose files from DIR (e.g. ~/Downloads) into the "
                        "right course's _manual/, matching against the whole worklist")
    p.add_argument("--as", dest="assign", nargs=2, metavar=("URL", "FILE"),
                   help="assign FILE (in a course's _manual/) to the gated reading at URL, "
                        "for readings whose title no text can match")
    p.set_defaults(fn=cmd_adopt, source=None, assign=None)

    p = sub.add_parser("links", help="fetch the readings a course links out to")
    p.add_argument("--course", action="append", help="course folder name (repeatable)")
    p.add_argument("--plan", action="store_true", help="count URLs by host, fetch nothing")
    p.add_argument("--gated", action="store_true", help="list what needs a logged-in browser")
    p.add_argument("--force", action="store_true", help="refetch even what is cached")
    p.add_argument("--prune", action="store_true",
                   help="delete reading notes this run did not write")
    p.add_argument("--limit", type=int, help="stop after this many new fetches")
    p.add_argument("--delay", type=float, default=links_mod.DEFAULT_DELAY,
                   help="seconds between requests to one host (default 1.0)")
    p.set_defaults(fn=cmd_links)

    p = sub.add_parser("order", help="write Canvas's module order into flexplorer")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_order)

    sub.add_parser("cache", help="what the API cache holds, and how old it is"
                   ).set_defaults(fn=cmd_cache)

    sub.add_parser("status", help="what is archived, and what failed").set_defaults(fn=cmd_status)

    p = sub.add_parser("fetch-files",
                       help="fallback: rsync binaries from vault-host (Sync normally does this)")
    p.add_argument("--host", default="vault-host")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_fetch_files)

    args = ap.parse_args(argv)
    try:
        if not _writes_state(args):
            return args.fn(args)
        try:
            with canvas_lock(f"vt canvas {args.cmd}"):
                return args.fn(args)
        except Busy as who:
            print(f"another Canvas run holds the lock ({who}); nothing was changed. "
                  "Try again when it finishes.", file=sys.stderr)
            return 2
    except cache_mod.Miss as e:
        # Deliberately fatal. The alternative - treating a miss as an empty
        # section - is how `--prune` would delete real notes on the strength of
        # a cache gap. See cache.py, defence 2.
        sys.exit(f"cache miss, run stopped: {e}")
    except Unavailable as e:
        sys.exit(f"Canvas refused: {e}. If this is a 401, the token is wrong or expired.")
    except KeyboardInterrupt:
        print("\ninterrupted — state was checkpointed after each course, so re-running resumes.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
