#!/usr/bin/env python3
"""The Chat tab and Talk (Nov 8), offline: thread ids and titles, the thread list, and the routes
(send, voice with talk=1, threads) through Starlette's test client over a fake chat hub and a fake
transcriber. Nothing reaches Claude, Groq or OpenAI. Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_general
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="carmen-general-"))
(_SCRATCH / "home").mkdir()
(_SCRATCH / "vault").mkdir()
os.environ["HOME"], os.environ["VAULT_PATH"] = str(_SCRATCH / "home"), str(_SCRATCH / "vault")

import jobs.checkin_app as app
import jobs.checkin_app.chat as chat_mod
import jobs.checkin_app.web as web
from jobs.tg_bridge import media

chat_mod.DIR = _SCRATCH / "chat"
failures = []


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


# --- ids, titles, the list -------------------------------------------------------------
check("thread id keeps letters, digits, dashes", chat_mod.thread_id("g20261108-101500") == "g20261108-101500")
check("thread id strips the rest", chat_mod.thread_id("../x y:z") == "xyz")
check("no thread id is main", chat_mod.thread_id("") == "main" and chat_mod.thread_id("::") == "main")
check("thread id at most 40", len(chat_mod.thread_id("a" * 99)) == 40)
check("title is the first words", chat_mod.thread_title("what is on   today") == "what is on today")
long_title = chat_mod.thread_title("word " * 40)
check("long title cut at a word", len(long_title) <= 61 and long_title.endswith("…"), long_title)
check("image-only title", chat_mod.thread_title("", True) == "Screenshot")

chat_mod.save({"key": "general:g1", "title": "First", "messages": [{"role": "you", "text": "a", "at": "2026-11-08T09:00:00-08:00"}]})
chat_mod.save({"key": "general:g2", "title": "Second", "messages": [{"role": "you", "text": "b", "at": "2026-11-08T10:00:00-08:00"}]})
chat_mod.save({"key": "general:g3", "title": "Empty", "messages": [{"role": "claude", "text": "x", "hidden": True, "at": "2026-11-08T11:00:00-08:00"}]})
chat_mod.save({"key": "task:t-abc123", "title": "A task", "messages": [{"role": "you", "text": "c", "at": "2026-11-08T12:00:00-08:00"}]})
ts = chat_mod.threads()
check("threads: general only, newest first, none empty", [t["id"] for t in ts] == ["g2", "g1"], ts)

# --- the routes ------------------------------------------------------------------------
from starlette.testclient import TestClient

sent = []


class FakeHub:
    def state(self, key):
        return {"key": key, "title": chat_mod.load(key).get("title", ""), "messages": chat_mod.load(key)["messages"],
                "busy": False, "status": "", "approval": None}

    def send(self, key, title, text, **kw):
        sent.append({"key": key, "title": title, "text": text, **kw})
        conv = chat_mod.load(key)
        conv.update(title=title or conv.get("title", ""), kind=kw.get("kind"))
        conv["messages"].append({"role": "you", "text": text, "at": "2026-11-08T13:00:00-08:00"})
        chat_mod.save(conv)
        return self.state(key)

    async def housekeeping(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass

    async def stop(self, key):
        pass


chat_mod.Hub = FakeHub


class FakeTranscriber:
    def __init__(self, *a, **k):
        pass

    async def transcribe(self, audio, filename):
        return "what is on today", "fake"


media.Transcriber = FakeTranscriber
app.config.load_env = lambda: {"CHECKIN_PASSWORD": "pw", "CHECKIN_SECRET": "s3cret"}

with TestClient(app.make_app()) as client:
    check("threads refuse without login", client.get("/checkin/api/chat/threads").status_code == 403)
    client.cookies.set(web.COOKIE, web.make_cookie("s3cret"))
    r = client.post("/checkin/api/chat", json={"op": "send", "kind": "general", "id": "g9../x", "text": "Hello Carmen, plan my week please", "test": True})
    check("general send answers", r.status_code == 200, (r.status_code, r.text[:300]))
    s = sent[-1] if sent else {}
    check("general send keys on the thread", s.get("key") == "general:g9x", s.get("key"))
    check("general send has no card context", s.get("context", "") == "" and s.get("kind") == "general", s)
    check("general thread is named by its first message", s.get("title") == "Hello Carmen, plan my week please", s.get("title"))
    r = client.post("/checkin/api/chat", json={"op": "send", "kind": "general", "id": "g9x", "text": "and next week?", "test": True})
    check("a thread keeps its first title", sent[-1]["title"] == "Hello Carmen, plan my week please", sent[-1]["title"])
    r = client.post("/checkin/api/chat", json={"op": "send", "kind": "general", "id": "g9x", "text": "  ", "test": True})
    check("empty general message refused", r.status_code == 400, r.status_code)
    r = client.get("/checkin/api/chat?kind=general&id=g9x")
    check("general chat reads back", r.status_code == 200 and r.json()["key"] == "general:g9x", r.text[:200])
    r = client.get("/checkin/api/chat/threads")
    ids = [t["id"] for t in r.json().get("threads", [])]
    check("the new thread is listed first", r.status_code == 200 and ids[:1] == ["g9x"], ids)

    r = client.post("/checkin/api/chat/voice?kind=general&id=g9x&test=1&talk=1", content=b"\0" * 5000,
                    headers={"content-type": "audio/mp4"})
    check("talk voice answers", r.status_code == 200, (r.status_code, r.text[:300]))
    s = sent[-1]
    check("talk voice asks for a short spoken reply", "Talk mode" in (s.get("prompt_text") or "") and s.get("voice") is True, s.get("prompt_text"))
    check("talk voice shows his words only", s.get("text") == "what is on today", s.get("text"))
    r = client.post("/checkin/api/chat/voice?kind=general&id=g9x&test=1", content=b"\0" * 5000,
                    headers={"content-type": "audio/mp4"})
    check("a plain voice message is not talk mode", "Talk mode" not in (sent[-1].get("prompt_text") or ""), sent[-1].get("prompt_text"))

page = (pathlib.Path(__file__).resolve().parents[1] / "jobs" / "checkin_app" / "page.html").read_text()
for needle in ('["chat", "Chat"]', "function mountGeneral", "function talkStart", "talk=1", "api/chat/threads"):
    check(f"page has {needle}", needle in page)

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
