"""Turn a source spec into a URL yt-dlp understands, plus the metadata we need.

yt-dlp does the grabbing. Adopted 2026-09-02, replacing a hand-rolled Wistia
JSON client: yt-dlp ships WistiaIE/WistiaChannelIE/WistiaPlaylistIE plus ~1800
other extractors, so the next course being on Vimeo or a bare CDN costs nothing.
The tradeoff taken knowingly is a dependency whose extractors break and want an
occasional `brew upgrade yt-dlp`.

Only two things are done here that yt-dlp does not:

  wistia:<hashedId>   sugar, because that is how the course inventory is written
  <local path>        handed to ffmpeg directly; yt-dlp adds nothing to a file
                      already on disk

Everything else is passed through verbatim, page URLs included.
"""

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

WISTIA_EMBED = "https://fast.wistia.net/embed/medias/{}"


class SourceError(RuntimeError):
    pass


@dataclass
class Source:
    key: str            # stable cache key, safe as a directory name
    label: str          # human name for logs and meta.json
    url: str | None     # what yt-dlp should fetch, or None when local
    path: Path | None   # local media, or None when remote
    duration: float | None
    spec: str

    @property
    def minutes(self) -> str:
        if not self.duration:
            return "?"
        return f"{int(self.duration // 60)}:{int(self.duration % 60):02d}"


def _slug(text: str) -> str:
    out = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return out[:60] or "media"


def ytdlp_base(cookies: str | None = None) -> list[str]:
    """The flags every yt-dlp call in this tool shares."""
    if not shutil.which("yt-dlp"):
        raise SourceError("yt-dlp not on PATH — brew install yt-dlp")
    argv = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress"]
    if cookies:
        # For Kajabi-style pages that gate the embed behind a login. The Wistia
        # media itself is unprotected, so this is rarely needed.
        argv += ["--cookies-from-browser", cookies]
    return argv


def _probe(url: str, cookies: str | None) -> dict:
    proc = subprocess.run(ytdlp_base(cookies) + ["--dump-single-json", "--skip-download", url],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        raise SourceError("yt-dlp could not read the source:\n  " + "\n  ".join(tail))
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError(f"yt-dlp returned unreadable JSON: {exc}") from exc
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            raise SourceError(f"{url} is an empty playlist")
        # --no-playlist should have prevented this; if a page really holds
        # several videos, taking the first silently would be the wrong answer.
        if len(entries) > 1:
            raise SourceError(
                f"{url} holds {len(entries)} videos; list them as separate sources")
        data = entries[0]
    return data


def resolve(spec: str, *, cookies: str | None = None) -> Source:
    """One spec in, one Source out. Raises SourceError with a usable message."""
    spec = spec.strip()
    if not spec:
        raise SourceError("empty source spec")

    if spec.startswith("wistia:"):
        url = WISTIA_EMBED.format(spec.split(":", 1)[1].strip())
    elif spec.startswith(("http://", "https://")):
        url = spec
    else:
        path = Path(spec).expanduser()
        if not path.is_file():
            raise SourceError(f"no such file: {path}")
        return Source(key=f"file-{_slug(path.stem)}", label=path.stem,
                      url=None, path=path, duration=None, spec=str(path))

    info = _probe(url, cookies)
    extractor = _slug(info.get("extractor_key") or info.get("extractor") or "media")
    ident = _slug(str(info.get("id") or _slug(url)))
    # Wistia's title is the uploader's original filename, path and extension
    # included. Stripping it is cosmetic but the label ends up in every log line.
    title = Path(str(info.get("title") or ident)).stem.replace("_", " ").strip()
    return Source(key=f"{extractor}-{ident}", label=title or ident, url=url,
                  path=None, duration=info.get("duration"), spec=spec)
