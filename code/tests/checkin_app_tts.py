#!/usr/bin/env python3
"""Carmen speaks (Nov 8), offline: what is read aloud, which provider goes first, the cache, the
fallback, and /api/chat/tts through Starlette's test client over a fake chat and a fake network.
Nothing reaches OpenAI or ElevenLabs. Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_tts
"""
import asyncio
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# A scratch HOME and vault before the app is imported: its state paths are fixed at import.
_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="carmen-tts-"))
(_SCRATCH / "home").mkdir()
(_SCRATCH / "vault").mkdir()
os.environ["HOME"], os.environ["VAULT_PATH"] = str(_SCRATCH / "home"), str(_SCRATCH / "vault")

import jobs.checkin_app as app
import jobs.checkin_app.chat as chat_mod
import jobs.checkin_app.speech as speech
import jobs.checkin_app.web as web

speech.CACHE = _SCRATCH / "tts"
failures = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


# --- speakable -------------------------------------------------------------------------
md = ("## Plan\n**Done.** I booked [Dentist](obsidian://open?vault=x&file=y) for Tue (t-3d22d7).\n\n"
      "- Next: email Priya → `Inbox/foo.md`\n- [ ] Then: pay\n\n```py\nx = 1\n```\n"
      "See https://example.com and [[Inbox/Task|the task]] 🎉\n| a | b |\n|---|---|")
said = speech.speakable(md)
check("speakable keeps the words", "I booked Dentist for Tue" in said and "email Priya" in said and "the task" in said, said)
for junk in ("**", "t-3d22d7", "obsidian", "https", "x = 1", "Inbox/foo", "🎉", "|", "##", "[ ]", "→"):
    check(f"speakable drops {junk!r}", junk not in said, said)
long = ("This is a sentence that goes on. " * 300).strip()
cut = speech.speakable(long, limit=500)
check("speakable cuts at a sentence end", len(cut) <= 500 and cut.endswith("."), cut[-40:])
check("speakable of nothing is empty", speech.speakable("```\ncode only\n```") == "")

# --- providers -------------------------------------------------------------------------
ps = speech.providers_from_env({"OPENAI_API_KEY": "o"})
check("openai alone by default", [p.name for p in ps] == ["openai"] and ps[0].model == "gpt-4o-mini-tts")
ps = speech.providers_from_env({"OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e"})
check("openai first unless set", [p.name for p in ps] == ["openai", "elevenlabs"])
ps = speech.providers_from_env({"OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e", "CARMEN_TTS_PROVIDER": "elevenlabs",
                                "ELEVENLABS_VOICE_ID": "v1"})
check("one setting puts elevenlabs first", [p.name for p in ps] == ["elevenlabs", "openai"])
url, headers, body = ps[0].request("hi")
check("elevenlabs request shape", "/text-to-speech/v1" in url and headers.get("xi-api-key") == "e"
      and body == {"text": "hi", "model_id": "eleven_flash_v2_5"}, (url, body))
url, headers, body = ps[1].request("hi")
check("openai request shape", url == speech.OPENAI_URL and headers["Authorization"] == "Bearer o"
      and body["input"] == "hi" and body["response_format"] == "mp3" and "instructions" in body, body)
check("no key, no provider", speech.providers_from_env({}) == [])

# --- synthesize: fallback and cache ----------------------------------------------------
calls = []


async def fake_post(url, headers, body, timeout):
    calls.append(url)
    if "openai" in url:
        return 500, b"down"
    return 200, b"ID3" + b"\0" * 400


ps = speech.providers_from_env({"OPENAI_API_KEY": "o", "ELEVENLABS_API_KEY": "e"})
audio, name, cached = asyncio.run(speech.synthesize("Hello there.", ps, fake_post))
check("falls back to the next provider", name == "elevenlabs" and not cached and len(calls) == 2, (name, calls))
audio2, name2, cached2 = asyncio.run(speech.synthesize("Hello there.", ps, fake_post))
check("second time comes from the cache", cached2 and audio2 == audio and len(calls) == 2, (cached2, calls))


async def dead_post(url, headers, body, timeout):
    raise TimeoutError()


try:
    asyncio.run(speech.synthesize("Something new.", ps, dead_post))
    check("all providers down raises", False)
except speech.SpeechError as exc:
    check("all providers down raises", "TimeoutError" in str(exc), str(exc))

# --- reply_text ------------------------------------------------------------------------
state = {"messages": [{"role": "you", "text": "what's next", "voice": True},
                      {"role": "claude", "text": "kickoff", "hidden": True},
                      {"role": "claude", "text": "**Email** Priya."},
                      {"role": "note", "text": "a note"},
                      {"role": "claude", "text": "Then pay."}]}
check("reply_text takes only Claude's visible messages",
      speech.reply_text(state, [0, 1, 2, 3, 4, 99, -1]) == "**Email** Priya.\n\nThen pay.")

# --- the route -------------------------------------------------------------------------
from starlette.testclient import TestClient


class FakeHub:
    def state(self, key):
        return state if key == "task:t-abc123" else {"messages": []}

    async def housekeeping(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


chat_mod.Hub = FakeHub
spoken = []


async def route_post(url, headers, body, timeout):
    spoken.append(body.get("input") or body.get("text"))
    return 200, b"ID3" + b"\1" * 400


speech.httpx_post = route_post
app.config.load_env = lambda: {"CHECKIN_PASSWORD": "pw", "CHECKIN_SECRET": "s3cret", "OPENAI_API_KEY": "o"}
_card_id = getattr(app, "card_id", None)
with TestClient(app.make_app()) as client:
    r = client.get("/checkin/api/chat/tts?kind=task&id=t-abc123&i=2&i=4")
    check("tts refuses without login", r.status_code == 403, r.status_code)
    client.cookies.set(web.COOKIE, web.make_cookie("s3cret"))
    r = client.get("/checkin/api/chat/tts?kind=task&id=t-abc123&i=2&i=4")
    check("tts answers mp3", r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
          and r.content.startswith(b"ID3"), (r.status_code, r.text[:200]))
    check("tts voices the stored text, cleaned", spoken and spoken[-1] == "Email Priya. Then pay.", spoken)
    r = client.get("/checkin/api/chat/tts?kind=task&id=t-abc123&i=0")
    check("his own message is never voiced", r.status_code == 404, r.status_code)
    r = client.get("/checkin/api/chat/tts?kind=task&id=t-abc123&i=x")
    check("a bad index is refused", r.status_code == 400, r.status_code)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
