"""Carmen speaks (Nov 8): a Claude reply in the chat read aloud.

owner decision, Nov 8: "implement the voice mode". He already talks to Carmen (chat_voice: the
recording is transcribed by the bridge's Transcriber); this is the way back. The page asks for
`/api/chat/tts` with the chat and the index of Claude's message(s); the server reads the text
from the stored chat (never text the page sends, so the endpoint can only voice what Claude
said), turns the markdown into something worth hearing (`speakable`), and asks a provider for
mp3. Owns nothing but a cache of the audio, by hash, in STATE_DIR/checkin_app/tts, pruned after
7 days.

Providers are swappable by one setting in ~/.automation.env:
  CARMEN_TTS_PROVIDER   openai (default) | elevenlabs
  CARMEN_TTS_VOICE      OpenAI voice (default "marin")
  CARMEN_TTS_MODEL      OpenAI model (default "gpt-4o-mini-tts")
  ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ELEVENLABS_MODEL (default "eleven_flash_v2_5")
The chosen provider goes first; the other follows only if its key is set (as the
Transcriber does: the first that fails hands over, no second try on the same one).
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from runner import config

CACHE = config.STATE_DIR / "checkin_app" / "tts"
CACHE_DAYS = 7
MAX_CHARS = 3800          # OpenAI's speech input cap is 4096; ElevenLabs Flash takes more
TIMEOUT_S = 45.0

OPENAI_URL = "https://api.openai.com/v1/audio/speech"
ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128"
OPENAI_INSTRUCTIONS = ("You are Carmen, a calm, warm personal assistant. Speak naturally and a little "
                       "briskly, like a friend reading a short note aloud. Keep the language of the text.")

# Talk mode (Nov 8): the Chat tab's hands-free conversation. Every reply is heard, not read, and
# the next turn waits on it, so the reply is short and spoken.
TALK_NOTE = ("\n[Talk mode: the owner is in a spoken, hands-free conversation and hears your reply read aloud. "
             "Answer in one to three short spoken sentences, like a person on a call: no lists, headings, links, "
             "tables, code or task ids. Do the work first if it is quick, then say the result. If it needs more "
             "than a minute, say what you will do and that the details will be in the chat.]")

# post(url, headers, json_body, timeout) -> (status, bytes)
Post = Callable[[str, dict, dict, float], Awaitable[tuple[int, bytes]]]


class SpeechError(Exception):
    pass


async def httpx_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, bytes]:
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
    return resp.status_code, resp.content


# --- what is worth hearing --------------------------------------------------------------

_FENCE = re.compile(r"```.*?```", re.S)
_MDLINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_WIKI = re.compile(r"\[\[([^\]|]*\|)?([^\]]*)\]\]")
_URL = re.compile(r"https?://\S+|obsidian://\S+|\bfile\?p=\S+")
_TASKID = re.compile(r"\(?\b[t]-[0-9a-f]{6}\b\)?|\(?\bev-[0-9a-f]{8}\b\)?")
_PATH = re.compile(r"`[^`]*[/.][^`]*`")      # backticked paths and file names say nothing aloud
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


def speakable(text: str, limit: int = MAX_CHARS) -> str:
    """Claude's markdown as plain sentences: no code, links read as their words, no task ids,
    paths, URLs, bullets, headings or emphasis marks. Cut at a sentence end within `limit`."""
    t = _FENCE.sub(" ", text or "")
    t = _WIKI.sub(lambda m: m.group(2), t)
    t = _MDLINK.sub(lambda m: m.group(1), t)
    t = _URL.sub("", t)
    t = _TASKID.sub("", t)
    t = _PATH.sub("", t)
    t = t.replace("`", "")
    t = _EMOJI.sub("", t)
    lines = []
    for line in t.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)                 # headings
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+(?:\[[ xX]\]\s*)?", "", line)   # bullets, checkboxes
        line = re.sub(r"^\s*>\s?", "", line)                         # quotes
        line = re.sub(r"^\s*\|.*\|\s*$", "", line)                   # table rows
        line = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", line)
        line = line.replace("→", ", ").replace("·", ", ").replace("—", ", ").replace(" – ", ", ")
        line = re.sub(r"[ \t]+", " ", line).strip(" ,")
        if line and not re.fullmatch(r"[-=_*|: ]+", line):
            if not re.search(r"[.!?:;,]$", line):
                line += "."
            lines.append(line)
    t = " ".join(lines)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)
    t = re.sub(r"([.,;:!?])\1+", r"\1", t)
    t = re.sub(r"\(\s*\)", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        t = cut[:end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0] + "."
    return t


# --- providers -------------------------------------------------------------------------

@dataclass
class Provider:
    name: str
    key: str
    voice: str
    model: str

    def request(self, text: str) -> tuple[str, dict, dict]:
        if self.name == "elevenlabs":
            return (ELEVEN_URL.format(voice=self.voice),
                    {"xi-api-key": self.key, "accept": "audio/mpeg"},
                    {"text": text, "model_id": self.model})
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "mp3"}
        if "gpt-4o" in self.model:
            body["instructions"] = OPENAI_INSTRUCTIONS
        return OPENAI_URL, {"Authorization": f"Bearer {self.key}"}, body


def providers_from_env(env: dict | None = None) -> list[Provider]:
    e = env if env is not None else config.load_env()
    get = lambda k, d="": (e.get(k) or d).strip()
    openai = Provider("openai", get("OPENAI_API_KEY"), get("CARMEN_TTS_VOICE", "marin"),
                      get("CARMEN_TTS_MODEL", "gpt-4o-mini-tts"))
    eleven = Provider("elevenlabs", get("ELEVENLABS_API_KEY"),
                      get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),   # "Rachel", a stock voice
                      get("ELEVENLABS_MODEL", "eleven_flash_v2_5"))
    order = [eleven, openai] if get("CARMEN_TTS_PROVIDER", "openai").lower() == "elevenlabs" else [openai, eleven]
    return [p for p in order if p.key]


def cache_path(p: Provider, text: str) -> Path:
    h = hashlib.sha256(f"{p.name}|{p.model}|{p.voice}|{text}".encode()).hexdigest()[:32]
    return CACHE / f"{h}.mp3"


def prune(now: float | None = None) -> None:
    if not CACHE.is_dir():
        return
    cutoff = (now or time.time()) - CACHE_DAYS * 86400
    for f in CACHE.glob("*.mp3"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def synthesize(text: str, providers: list[Provider], post: Post | None = None,
                     timeout: float = TIMEOUT_S) -> tuple[bytes, str, bool]:
    """(mp3, provider name, from cache). Raises SpeechError when nothing could speak it."""
    post = post or httpx_post   # looked up per call, so a test can stand in for the network
    if not text.strip():
        raise SpeechError("nothing to read aloud")
    if not providers:
        raise SpeechError("no speech key is configured (OPENAI_API_KEY or ELEVENLABS_API_KEY)")
    for p in providers:
        f = cache_path(p, text)
        if f.is_file() and f.stat().st_size > 0:
            f.touch()
            return f.read_bytes(), p.name, True
    errors = []
    for p in providers:
        url, headers, body = p.request(text)
        try:
            status, audio = await post(url, headers, body, timeout)
        except Exception as exc:
            errors.append(f"{p.name}: {type(exc).__name__}")
            continue
        if status != 200 or len(audio) < 100:
            errors.append(f"{p.name}: HTTP {status}")
            continue
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            tmp = cache_path(p, text).with_suffix(".part")
            tmp.write_bytes(audio)
            tmp.replace(cache_path(p, text))
            prune()
        except OSError:
            pass
        return audio, p.name, False
    raise SpeechError("speech failed (" + "; ".join(errors) + ")")


def reply_text(state: dict, indexes: list[int]) -> str:
    """The text of Claude's messages at these indexes in a chat's state, joined; anything that is
    not Claude's (his messages, notes, hidden kickoffs) is skipped."""
    msgs = state.get("messages") or []
    parts = []
    for i in indexes:
        if 0 <= i < len(msgs):
            m = msgs[i]
            if m.get("role") == "claude" and not m.get("hidden") and (m.get("text") or "").strip():
                parts.append(m["text"])
    return "\n\n".join(parts)
