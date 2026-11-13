"""Fetch, decode and transcribe. Everything cached, everything resumable.

Layout under ~/.local/state/automation/transcribe/<key>/

    audio.wav       16 kHz mono PCM, what whisper.cpp actually wants
    transcript.txt  the German text
    meta.json       label, spec, model, duration, when

A run that dies halfway is expected to be common: 27 lessons over a home
connection. Every stage checks for its own output first, so re-running is
cheap and safe rather than a fresh download of the whole course.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from runner import config

from .sources import Source, SourceError, ytdlp_base

CACHE = config.STATE_DIR / "transcribe"
MODEL_DIR = config.HOME / ".local" / "share" / "automation" / "whisper"

# large-v3-turbo is the deliberate default, chosen 2026-09-02: on the M4 it
# runs several times faster than large-v3 with German quality close enough
# that the difference does not survive a human reading the callout. Escalate
# with --model large-v3 when a transcript reads badly.
DEFAULT_MODEL = "large-v3-turbo-q5_0"

# `auto` since 2026-09-02. The model is multilingual — the same weights cover
# ~99 languages — so defaulting to German was a limit this tool invented rather
# than one whisper has. Detection measured at p > 0.999 on en/de/fr samples.
# Pin with --language <code> when a source is short or accented enough that a
# misdetection would be worse than being wrong about the language up front.
DEFAULT_LANGUAGE = "auto"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin"

WRAP = 92

# Only the HuggingFace model fetch uses this; every media fetch is yt-dlp's.
UA = "vt-transcribe/1.0"


class EngineError(RuntimeError):
    pass


@dataclass
class Result:
    source: Source
    text: str
    seconds: float
    cached: bool
    language: str = "?"     # what was actually spoken, detected or pinned


def model_path(name: str = DEFAULT_MODEL) -> Path:
    return MODEL_DIR / f"ggml-{name}.bin"


def ensure_model(name: str = DEFAULT_MODEL, *, quiet: bool = False) -> Path:
    """Models are not in git and not in the vault: 574 MB of binary is neither.

    They are re-downloadable, so the cache is the right home and a missing one
    is a fetch rather than an error.
    """
    dest = model_path(name)
    if dest.is_file() and dest.stat().st_size > 1_000_000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = MODEL_URL.format(name)
    if not quiet:
        print(f"  fetching model {name} (once) ...", flush=True)
    part = dest.with_suffix(".part")
    _download(url, part)
    part.rename(dest)
    return dest


def doctor() -> list[str]:
    """What is missing, in the order it would bite. Empty list means ready."""
    problems = []
    for binary, formula in (("yt-dlp", "yt-dlp"), ("ffmpeg", "ffmpeg"),
                            ("whisper-cli", "whisper-cpp")):
        if not shutil.which(binary):
            problems.append(f"{binary} not on PATH — brew install {formula}")
    if not model_path().is_file():
        problems.append(f"model {DEFAULT_MODEL} not downloaded — any run fetches it")
    return problems


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1 << 20)
    tmp.rename(dest)


def _run(argv: list[str], *, what: str) -> subprocess.CompletedProcess:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise EngineError(f"{what} failed:\n  " + "\n  ".join(tail))
    return proc


def audio(source: Source, *, cookies: str | None = None, force: bool = False) -> Path:
    """One 16 kHz mono wav per source, which is the only thing whisper.cpp wants.

    yt-dlp fetches and hands off to ffmpeg in a single pass, so there is no
    intermediate mp4 sitting in the cache. `-S +size` takes the smallest
    rendition on offer — 1.7 MB against 111 MB for the Wistia original, and
    whisper hears no difference between them.
    """
    slot = CACHE / source.key
    slot.mkdir(parents=True, exist_ok=True)
    wav = slot / "audio.wav"
    if wav.is_file() and wav.stat().st_size > 0 and not force:
        return wav

    if source.path:
        _run(["ffmpeg", "-nostdin", "-y", "-i", str(source.path),
              "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
             what="ffmpeg decode")
        return wav

    _run(ytdlp_base(cookies) + [
        "-S", "+size",
        "-x", "--audio-format", "wav",
        "--postprocessor-args", "ExtractAudio:-ac 1 -ar 16000 -c:a pcm_s16le",
        "--force-overwrites",
        "-o", str(slot / "audio.%(ext)s"),
        source.url,
    ], what="yt-dlp extract")

    if not wav.is_file():
        raise EngineError(f"yt-dlp produced no audio for {source.label}")
    return wav


def _reflow(raw: str) -> str:
    """whisper.cpp -otxt emits one line per segment, mid-sentence breaks and all.

    Joining them back into paragraphs and wrapping at a fixed width makes the
    callout readable and the diff stable. Blank lines in the raw output are the
    only paragraph signal there is, so they are kept.
    """
    paragraphs, buffer = [], []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            buffer.append(line)
        elif buffer:
            paragraphs.append(" ".join(buffer))
            buffer = []
    if buffer:
        paragraphs.append(" ".join(buffer))
    wrapped = ["\n".join(textwrap.wrap(p, WRAP)) for p in paragraphs if p]
    return "\n\n".join(wrapped).strip()


def transcribe(source: Source, *, model: str = DEFAULT_MODEL,
               language: str = DEFAULT_LANGUAGE, cookies: str | None = None,
               force: bool = False, quiet: bool = False) -> Result:
    slot = CACHE / source.key
    slot.mkdir(parents=True, exist_ok=True)
    out = slot / "transcript.txt"

    if out.is_file() and out.read_text(encoding="utf-8").strip() and not force:
        was = "?"
        meta = slot / "meta.json"
        if meta.is_file():
            try:
                was = json.loads(meta.read_text(encoding="utf-8")).get("language", "?")
            except json.JSONDecodeError:
                pass
        return Result(source, out.read_text(encoding="utf-8").strip(), 0.0, True, was)

    started = time.time()
    ensure_model(model, quiet=quiet)
    wav = audio(source, cookies=cookies, force=force)

    stem = slot / "whisper"
    proc = _run(["whisper-cli",
          "-m", str(model_path(model)),
          "-f", str(wav),
          "-l", language,
          "-otxt", "-of", str(stem),
          # No -np: it suppresses the "auto-detected language" line on stderr,
          # which is the only place whisper.cpp reports what it heard. The rest
          # of the noise is discarded; only that one line is parsed.
          "-nt",
          "-t", str(min(8, os.cpu_count() or 4))],
         what="whisper-cli")

    spoken = language
    if language == "auto":
        found = re.search(r"auto-detected language:\s*(\w+)", proc.stderr or "")
        spoken = found.group(1) if found else "?"

    produced = stem.with_suffix(".txt")
    if not produced.is_file():
        raise EngineError(f"whisper produced no transcript for {source.label}")
    text = _reflow(produced.read_text(encoding="utf-8"))
    if not text:
        raise EngineError(f"empty transcript for {source.label} — check the audio")
    out.write_text(text + "\n", encoding="utf-8")
    produced.unlink(missing_ok=True)

    elapsed = time.time() - started
    (slot / "meta.json").write_text(json.dumps({
        "spec": source.spec,
        "label": source.label,
        "duration": source.duration,
        "model": model,
        "language": spoken,
        "language_mode": language,
        "seconds": round(elapsed, 1),
        "at": time.strftime("%Y-%m-%dT%H:%M"),
        "chars": len(text),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return Result(source, text, elapsed, False, spoken)


def cached() -> list[dict]:
    rows = []
    if not CACHE.is_dir():
        return rows
    for slot in sorted(CACHE.iterdir()):
        meta = slot / "meta.json"
        txt = slot / "transcript.txt"
        if not txt.is_file():
            continue
        row = {"key": slot.name, "chars": len(txt.read_text(encoding="utf-8"))}
        if meta.is_file():
            try:
                row.update(json.loads(meta.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
        rows.append(row)
    return rows


def prune_media() -> tuple[int, int]:
    """Drop the decoded audio, keep the transcripts. A wav is ~2 MB a minute."""
    freed = files = 0
    if not CACHE.is_dir():
        return 0, 0
    for slot in CACHE.iterdir():
        for name in ("audio.wav",):
            blob = slot / name
            if blob.is_file():
                freed += blob.stat().st_size
                blob.unlink()
                files += 1
    return files, freed
