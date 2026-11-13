#!/usr/bin/env python3
"""Transcribe course videos offline and write the German text into vault notes.

Built 2026-09-02 for the <COACHING_PROJECT> programmes, whose lessons are ~95% video with
no captions and no worksheets. Wispr Flow was ruled out first: it is a dictation
app, microphone in, and its API is enterprise-gated and shaped for live 16 kHz
voice, not batch files. yt-dlp grabs the media, ffmpeg decodes it, whisper.cpp
transcribes it offline and for free, with no recording of a private session
leaving the machine.

Usage:
  python3 -m tools.transcribe doctor                  # is the toolchain ready
  python3 -m tools.transcribe do <spec> [<spec>...]   # transcribe, print
  python3 -m tools.transcribe run <manifest.json>     # transcribe + fill notes
  python3 -m tools.transcribe run <m.json> --dry-run  # what it would write
  python3 -m tools.transcribe holes <note.md>         # empty Transkript callouts
  python3 -m tools.transcribe status                  # what the cache holds
  python3 -m tools.transcribe prune                   # drop media, keep text

Or through the shared entry point:

  vt transcribe doctor

A <spec> is one of:

  wistia:cvy0yvjh9n              a Wistia hashed id
  ~/Downloads/lektion.mp4        a local file
  https://.../video.mp4          a direct media URL
  https://<COURSE_SITE>.../posts/1   a page, scraped for the Wistia id it embeds

A manifest is a JSON list mapping sources onto note callouts:

  [{"note": "Projects/<COACHING_PROJECT>/.../MODUL 1A.md",
    "heading": "Lektion 1 — Herzlich Willkommen",
    "source": "wistia:cvy0yvjh9n"}]

`note` is relative to the vault. The manifest exists because the mapping from a
video to the place it belongs is knowledge the tool cannot derive: the Kajabi
lesson pages are behind a login and name no Wistia id in any stable way. Writing
it down once makes the run reviewable before it touches a single note.

MAC-ONLY, deliberately. This breaks the standing rule in [[vps-vault-host]] that
unattended work must survive the MacBook being shut, and the exception was taken
knowingly on 2026-09-02: vault-host is a CX22, 2 vCPU and 3.7 GB RAM, where
large-v3 does not fit in memory at all and turbo runs below realtime — this
course would take 5-8 hours and pin the box. Transcription is a job you kick
off, not a job that must run while he travels. See `doctor`, which says so.
"""

import argparse
import json
import sys
from pathlib import Path

from runner import config

from . import engine, notes
from .sources import Source, SourceError, resolve


def _on_box() -> bool:
    return config.VAULT == config.HOME / "vault"


def _resolve_note(raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else config.VAULT / raw


def _load_manifest(path: Path) -> list[dict]:
    if not path.is_file():
        sys.exit(f"no such manifest: {path}")
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"manifest is not valid JSON: {exc}")
    if not isinstance(rows, list):
        sys.exit("manifest must be a JSON list of objects")
    for i, row in enumerate(rows, 1):
        missing = [k for k in ("note", "heading", "source") if not row.get(k)]
        if missing:
            sys.exit(f"manifest entry {i} is missing: {', '.join(missing)}")
    return rows


def _transcribe(source: Source, args) -> engine.Result:
    return engine.transcribe(source, model=args.model, language=args.language,
                             cookies=args.cookies, force=args.force_transcribe)


def cmd_doctor(args) -> int:
    print(f"vault    {config.VAULT}")
    print(f"machine  {'vault-host' if _on_box() else 'Mac'}")
    print(f"model    {engine.model_path(args.model)}")
    print(f"cache    {engine.CACHE}")
    problems = engine.doctor()
    if _on_box():
        print("\nNOTE: this is vault-host. Transcription is Mac-only by decision "
              "(2026-09-02):\n  2 vCPU / 3.7 GB cannot hold large-v3 and runs turbo "
              "below realtime.\n  Run it on the Mac.")
    if problems:
        print("\nnot ready:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nready.")
    return 0


def cmd_do(args) -> int:
    failed = 0
    for spec in args.specs:
        try:
            source = resolve(spec, cookies=args.cookies)
        except SourceError as exc:
            print(f"!! {spec}: {exc}", file=sys.stderr)
            failed += 1
            continue
        print(f"\n=== {source.label}  ({source.minutes})  [{source.key}]", flush=True)
        try:
            result = _transcribe(source, args)
        except (engine.EngineError, SourceError) as exc:
            print(f"!! {exc}", file=sys.stderr)
            failed += 1
            continue
        tag = "cached" if result.cached else f"{result.seconds:.0f}s"
        print(f"--- {len(result.text)} chars, {result.language}, {tag}\n")
        print(result.text)
    return 1 if failed else 0


def cmd_run(args) -> int:
    rows = _load_manifest(Path(args.manifest).expanduser())
    print(f"{len(rows)} entries from {args.manifest}"
          f"{'  (dry run)' if args.dry_run else ''}\n")

    written = skipped = failed = 0
    for i, row in enumerate(rows, 1):
        head = f"[{i}/{len(rows)}] {row['heading']}"
        try:
            source = resolve(row["source"], cookies=args.cookies)
        except SourceError as exc:
            print(f"{head}\n    !! source: {exc}")
            failed += 1
            continue

        note = _resolve_note(row["note"])
        print(f"{head}  ({source.minutes})", flush=True)

        try:
            result = _transcribe(source, args)
        except (engine.EngineError, SourceError) as exc:
            print(f"    !! transcribe: {exc}")
            failed += 1
            continue

        try:
            outcome = notes.fill(note, row["heading"], result.text,
                                 force=args.force_write, dry_run=args.dry_run)
        except notes.NoteError as exc:
            print(f"    !! note: {exc}")
            failed += 1
            continue

        tag = "cached" if result.cached else f"{result.seconds:.0f}s"
        print(f"    {outcome.status}: {outcome.detail}  [{result.language}, {tag}]")
        if outcome.ok:
            written += 1
        else:
            skipped += 1

    print(f"\n{written} written, {skipped} skipped, {failed} failed")
    if not args.dry_run and written:
        print("Media is still cached. `vt transcribe prune` reclaims the disk.")
    return 1 if failed else 0


def cmd_holes(args) -> int:
    total = 0
    for raw in args.notes:
        note = _resolve_note(raw)
        if not note.is_file():
            print(f"!! no such note: {note}", file=sys.stderr)
            continue
        holes = notes.empty_callouts(note)
        total += len(holes)
        print(f"\n{note.relative_to(config.VAULT) if note.is_relative_to(config.VAULT) else note}"
              f"  — {len(holes)} empty")
        for heading in holes:
            print(f"  {heading}")
    print(f"\n{total} empty Transkript callouts")
    return 0


def cmd_status(args) -> int:
    rows = engine.cached()
    if not rows:
        print("nothing transcribed yet.")
        return 0
    chars = sum(row["chars"] for row in rows)
    for row in rows:
        print(f"  {row['key']:<28} {row['chars']:>7} chars  "
              f"{row.get('model', '?'):<20} {row.get('at', '')}")
    print(f"\n{len(rows)} transcripts, {chars:,} chars, cache at {engine.CACHE}")
    return 0


def cmd_prune(args) -> int:
    files, freed = engine.prune_media()
    print(f"removed {files} media files, {freed / 1e6:.0f} MB. Transcripts kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    # The shared flags hang off every subcommand rather than off the top-level
    # parser, so `vt transcribe do --model large-v3-q5_0 <spec>` works. Declared
    # only on the parent, argparse rejects them in that position -- which is the
    # order the docs use and the order anyone would type. (Fixed 2026-09-02
    # after it silently failed eight re-transcriptions in a row.)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=engine.DEFAULT_MODEL,
                        help=f"whisper.cpp model (default: {engine.DEFAULT_MODEL})")
    common.add_argument("--language", default=engine.DEFAULT_LANGUAGE,
                        help=f"source language code, or 'auto' to detect "
                             f"(default: {engine.DEFAULT_LANGUAGE})")
    common.add_argument("--force-transcribe", action="store_true",
                        help="re-download and re-transcribe even if cached")
    common.add_argument("--force-write", action="store_true",
                        help="replace a Transkript callout that already has content")
    common.add_argument("--cookies", metavar="BROWSER", default=None,
                        help="read cookies from a browser (chrome, safari, firefox) "
                             "for sources behind a login")

    parser = argparse.ArgumentParser(
        prog="vt transcribe",
        description="Offline German speech-to-text for course videos, into vault notes.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", parents=[common], help="check the toolchain")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("do", parents=[common], help="transcribe sources and print the text")
    p.add_argument("specs", nargs="+")
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("run", parents=[common],
                       help="transcribe a manifest and fill the notes")
    p.add_argument("manifest")
    p.add_argument("--dry-run", action="store_true",
                   help="transcribe, report what would be written, change no note")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("holes", parents=[common],
                       help="list empty Transkript callouts in notes")
    p.add_argument("notes", nargs="+")
    p.set_defaults(func=cmd_holes)

    p = sub.add_parser("status", parents=[common], help="what the transcript cache holds")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("prune", parents=[common],
                       help="delete cached media, keep the transcripts")
    p.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
