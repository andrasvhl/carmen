#!/usr/bin/env python3
"""Carmen through Siri (Nov 8), offline: the token, the spoken reply, waiting for a turn, and
/api/talk through Starlette's test client over a fake chat hub. Nothing reaches Claude.
Exit 1 if any check failed.

    .venv/bin/python -m tests.checkin_app_siri
"""
import asyncio
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

_SCRATCH = pathlib.Path(tempfile.mkdtemp(prefix="carmen-siri-"))
(_SCRATCH / "home").mkdir()
(_SCRATCH / "vault").mkdir()
os.environ["HOME"], os.environ["VAULT_PATH"] = str(_SCRATCH / "home"), str(_SCRATCH / "vault")

import jobs.checkin_app as app
import jobs.checkin_app.chat as chat_mod
import jobs.checkin_app.siri as siri

chat_mod.DIR = _SCRATCH / "chat"
siri.WAIT_S, siri.POLL_S = 1.0, 0.05
failures = []
TOKEN = "t" * 32


def check(name, cond, detail=""):
    print(("ok  " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        failures.append(name)


# --- token and mode --------------------------------------------------------------------
check("right token passes", siri.authorised(TOKEN, TOKEN))
check("wrong token fails", not siri.authorised("x" * 32, TOKEN))
check("no token configured fails", not siri.authorised("", "") and not siri.authorised("abc", "abc"))
check("live by default", siri.live({}) and not siri.live({"CARMEN_TALK_LIVE": "0"}))

# --- the spoken reply ------------------------------------------------------------------
st = {"busy": False, "approval": None, "messages": [
    {"role": "you", "text": "what is on"}, {"role": "claude", "text": "kick", "hidden": True},
    {"role": "claude", "text": "**Two things:** the report and a run. See [note](obsidian://x)."}]}
said, seen = siri.spoken_reply(st, 1)
check("reply is Claude's new words, speakable", said == "Two things: the report and a run. See note.", said)
check("seen moves to the end", seen == 3)
said2, seen2 = siri.spoken_reply(st, 3)
check("nothing new, not busy", said2 == siri.NOTHING and seen2 == 3, said2)
said3, _ = siri.spoken_reply({**st, "busy": True}, 3)
check("nothing new, busy: still working", said3 == siri.STILL_WORKING, said3)
said4, seen4 = siri.spoken_reply({**st, "approval": {"verb": "send an email"}}, 1)
check("approval waits in the app", "OK in the app to send an email" in said4 and seen4 == 1, said4)
said5, _ = siri.spoken_reply({**st, "busy": True}, 1)
check("partial reply says the rest is coming", said5.endswith("Still working on the rest."), said5)

# --- the route -------------------------------------------------------------------------
from starlette.testclient import TestClient

sent, mode = [], {"slow": False}


class FakeHub:
    def __init__(self):
        self.convs, self.busy_until = {}, {}

    def state(self, key):
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        loop_t = asyncio.get_event_loop().time() if self._loop_running() else 0
        if key in self.busy_until and loop_t >= self.busy_until[key]:
            del self.busy_until[key]
            c["messages"].append({"role": "claude", "text": "On it. **Done**: booked the run for 7."})
        return {"key": key, "title": c["title"], "messages": c["messages"], "busy": key in self.busy_until,
                "status": "", "approval": None}

    @staticmethod
    def _loop_running():
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def send(self, key, title, text, **kw):
        sent.append({"key": key, "title": title, "text": text, **kw})
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        c["title"] = c["title"] or title
        c["messages"].append({"role": "you", "text": text, "voice": True})
        self.busy_until[key] = asyncio.get_event_loop().time() + (5.0 if mode["slow"] else 0.2)
        return self.state(key)

    waiting, answered = [], []

    def pending(self):
        return list(self.waiting)

    def answer(self, req, allow):
        self.answered.append((req, allow))
        self.waiting.clear()
        return True

    async def housekeeping(self):
        await asyncio.sleep(3600)

    async def close(self):
        pass


chat_mod.Hub = FakeHub
app.config.load_env = lambda: {"CHECKIN_PASSWORD": "pw", "CHECKIN_SECRET": "s3cret", "CARMEN_TALK_TOKEN": TOKEN}

with TestClient(app.make_app()) as client:
    r = client.post("/checkin/api/talk", json={"thread": "siri-20261108", "text": "book a run"})
    check("no token: refused", r.status_code == 403, r.status_code)
    r = client.post("/checkin/api/talk", json={"thread": "siri-20261108", "text": "book a run"}, headers={"x-carmen-token": "nope" * 8})
    check("wrong token: refused", r.status_code == 403, r.status_code)
    H = {"x-carmen-token": TOKEN}
    r = client.post("/checkin/api/talk", json={"thread": "siri-20261108", "text": "book a run at seven"}, headers=H)
    check("talk answers", r.status_code == 200, (r.status_code, r.text[:300]))
    check("the reply is spoken text", r.json().get("reply") == "On it. Done: booked the run for 7.", r.json())
    s = sent[-1]
    check("goes to the Siri thread as a general chat", s["key"] == "general:siri-20261108" and s["kind"] == "general", s)
    check("live by default", s["test"] is False, s.get("test"))
    check("Claude is told it is Siri", "Talk mode via Siri" in s["prompt_text"] and "book a run at seven" in s["prompt_text"], s["prompt_text"])
    check("the thread is named for Siri", s["title"].startswith("Siri: book a run"), s["title"])
    mode["slow"] = True
    r = client.post("/checkin/api/talk", json={"thread": "siri-20261108", "text": "and plan my week"}, headers=H)
    check("slow turn: still working", r.json().get("reply") == siri.STILL_WORKING, r.json())
    r = client.post("/checkin/api/talk", json={"thread": "siri-20261108", "text": ""}, headers=H)
    check("empty text asks what's new (still busy)", r.json().get("reply") == siri.STILL_WORKING and len(sent) == 2, (r.json(), len(sent)))
    r = client.post("/checkin/api/talk", json={"bad": 1}, headers=H)
    check("no text and no thread: answers for the default thread", r.status_code == 200, r.status_code)
    r = client.post("/checkin/api/talk", content=b"not json", headers={**H, "content-type": "application/json"})
    check("bad body refused", r.status_code == 400, r.status_code)

# --- the iPhone app's background Talk (/api/talk/voice) ----------------------------------
import jobs.tg_bridge.media as media_mod
import jobs.checkin_app.speech as speech_mod


class FakeTranscriber:
    def __init__(self, *a, **k):
        pass

    async def transcribe(self, audio, name):
        if audio == b"ja":
            return "Ja, mach das.", "fake"
        return ("wie spät ist mein Lauf" if audio != b"silence" else ""), "fake"

    async def transcribe_detail(self, audio, name):
        if audio == b"noise":
            return "So basically.", "fake", [{"no_speech_prob": 0.9, "avg_logprob": -1.4}]
        if audio == b"ja-noisy":   # a short real answer Whisper is unsure about
            return "Ja.", "fake", [{"no_speech_prob": 0.7, "avg_logprob": -1.2}]
        text, prov = await self.transcribe(audio, name)
        return text, prov, [{"no_speech_prob": 0.05, "avg_logprob": -0.3}]


async def fake_synth(text, providers, **k):
    return b"ID3" + b"x" * 200, "fake", False

media_mod.Transcriber = FakeTranscriber
speech_mod.synthesize = fake_synth
siri.APP_WAIT_S = 1.0
mode["slow"] = False
with TestClient(app.make_app(), base_url="https://testserver") as client:
    r = client.post("/checkin/api/talk/voice?thread=talk-1", content=b"audio", headers={"content-type": "audio/mp4"})
    check("app talk: no cookie refused", r.status_code == 403, r.status_code)
    r = client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    check("app talk: login sets the cookie", "checkin" in client.cookies, (r.status_code, dict(client.cookies)))
    r = client.post("/checkin/api/talk/voice?thread=talk-1", content=b"audio", headers={"content-type": "audio/mp4"})
    j = r.json()
    check("app talk answers", r.status_code == 200, (r.status_code, r.text[:300]))
    check("app talk: heard is the transcript", j.get("heard") == "wie spät ist mein Lauf", j)
    check("app talk: reply spoken text", j.get("reply") == "On it. Done: booked the run for 7.", j)
    check("app talk: audio comes along", len(j.get("audio", "")) > 100 and j.get("busy") is False, j.get("busy"))
    s = sent[-1]
    check("app talk: the app note, its own thread", s["key"] == "general:talk-1" and "Carmen iPhone app" in s["prompt_text"], s)
    check("app talk: thread named Talk", s["title"].startswith("Talk: wie"), s["title"])
    n = len(sent)
    r = client.post("/checkin/api/talk/voice?thread=talk-1", content=b"", headers={"content-type": "audio/mp4"})
    check("app talk: empty body polls, sends nothing", r.status_code == 200 and len(sent) == n and r.json().get("heard") == "", r.json())

check("unheard: empty", siri.unheard("") and siri.unheard("  "))
check("unheard: ZDF hallucination", siri.unheard("Untertitel im Auftrag des ZDF, 2020"))
check("unheard: thanks for watching", siri.unheard("Thank you for watching!"))
check("heard: a real sentence", not siri.unheard("Wie spät ist mein Lauf morgen?"))
check("heard: short but real", not siri.unheard("Ja, mach das."))
check("app note: language and length", "language of his message" in siri.APP_NOTE and "two short" in siri.APP_NOTE)

check("end marker", siri.split_end("Bye! [end talk]") == ("Bye!", True) and siri.split_end("Good night! [end talk].") == ("Good night!", True) and not siri.split_end("I will end talk later")[1] and "[end talk]" in siri.APP_NOTE)
# --- the streamed Talk (/api/talk/stream) --------------------------------------------------
check("unfinished: trailing und", siri.unfinished("Ich wollte noch sagen und"))
check("unfinished: trailing ähm", siri.unfinished("Can you check my, ähm"))
check("unfinished: comma", siri.unfinished("Morgen um drei,"))
check("finished: a sentence", not siri.unfinished("Trag morgen um drei Leo ein."))
check("finished: a question", not siri.unfinished("And what about the run?"))
import json as _json
with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    r = client.post("/checkin/api/talk/stream?thread=st-1", content=b"audio", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    kinds = [x["t"] for x in rows]
    check("stream: heard first, done last", kinds[0] == "heard" and kinds[-1] == "done", kinds)
    check("stream: the reply is spoken", any(x["t"] == "say" and "booked the run" in x["text"] for x in rows), rows)
    check("stream: reply audio comes along", all(len(x.get("audio", "")) > 100 for x in rows if x["t"] == "say"), rows)
    check("stream: the streamed note goes to Claude", "spoken as you write it" in sent[-1]["prompt_text"], sent[-1]["prompt_text"][-200:])
    r = client.post("/checkin/api/talk/stream?thread=st-2", content=b"silence", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    check("stream: nothing heard ends quietly", rows[0] == {"t": "heard", "text": ""} and rows[-1]["t"] == "done", rows)

with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    FakeHub.waiting = [{"key": "general:st-3", "id": "r1", "verb": "use mcp__browser__browser_navigate",
                        "shown": "https://www.cvs.com", "why": "", "kind": "general", "thread": "st-3", "title": "T"}]
    n = len(sent)
    r = client.post("/checkin/api/talk/stream?thread=st-3", content=b"audio", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    check("voice approval: unclear words ask again", any(x["t"] == "say" and "OK" in x["text"] for x in rows)
          and rows[-1].get("approval") is True and len(sent) == n and not FakeHub.answered, rows)
    r = client.post("/checkin/api/talk/stream?thread=st-3", content=b"ja", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    check("voice approval: ja allows it", FakeHub.answered == [("r1", True)] and len(sent) == n, (FakeHub.answered, rows))
    check("voice approval: says so", any(x["t"] == "say" and "erlaubt" in x["text"] for x in rows), rows)

check("voice yes", siri.voice_verdict("Ja, mach das.") is True and siri.voice_verdict("Yes") is True)
check("voice no", siri.voice_verdict("Nein, lieber nicht") is False and siri.voice_verdict("no") is False)
check("voice unclear: both", siri.voice_verdict("ja nein") is None)
check("voice unclear: long", siri.voice_verdict("ja also ich weiß nicht so genau was du meinst") is None)
check("voice unclear: neither", siri.voice_verdict("wie spät ist es") is None)
line_de = siri.approval_line({"verb": "use mcp__browser__browser_navigate", "shown": '{"url": "https://www.cvs.com/x"}'}, True)
check("approval line names the tool and site", "browser navigate" in line_de and "cvs.com" in line_de and "ja oder nein" in line_de, line_de)

# --- the noise gate (Nov 9, street noise became turns) --------------------------------------
import io as _io
import wave as _wave
import jobs.checkin_app.noise as noise


def _wav(samples):
    b = _io.BytesIO()
    with _wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return b.getvalue()


check("noise: Whisper sure nobody spoke", noise.whisper_noise([{"no_speech_prob": 0.9, "avg_logprob": -1.5}]))
check("noise: one real segment keeps the clip", not noise.whisper_noise([{"no_speech_prob": 0.9, "avg_logprob": -1.5},
                                                                          {"no_speech_prob": 0.1, "avg_logprob": -0.2}]))
check("noise: looser after a question", not noise.whisper_noise([{"no_speech_prob": 0.7, "avg_logprob": -1.2}], loose=True))
check("noise: too little speech", noise.drop_reason(None, 100, False) == "vad_100ms" and noise.drop_reason(None, 200, True) == "")
check("noise: VAD unknown is not noise", noise.drop_reason(None, None, False) == "")
check("noise: asked after a question", noise.asked({"messages": [{"role": "claude", "text": "Soll ich das buchen?"}]})
      and not noise.asked({"messages": [{"role": "claude", "text": "Gebucht."}]}))
check("noise: not a WAV is not judged", noise.pcm16k(b"not a wav") is None)
import pwd as _pwd
_real = pathlib.Path(_pwd.getpwuid(os.getuid()).pw_dir) / ".local/state/automation/models/silero_vad.onnx"
if _real.exists():   # the suite runs with a scratch HOME: use the box's real model when it has one
    os.environ["CARMEN_VAD_MODEL"] = str(_real)
    noise._session.clear()
if noise._vad() is not None:
    import random as _r
    check("noise: silence has no speech", noise.speech_ms(_wav([0] * 16000)) == 0, noise.speech_ms(_wav([0] * 16000)))
    hiss = noise.speech_ms(_wav([_r.randint(-3000, 3000) for _ in range(16000)]))
    check("noise: hiss is not speech", hiss is not None and hiss < noise.MIN_SPEECH_MS, hiss)
else:
    check("noise: the Silero model loads on this box", False, str(noise.model_path()))

with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    n = len(sent)
    r = client.post("/checkin/api/talk/stream?thread=nz-1", content=b"noise", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    check("noise: the stream drops it silently", len(sent) == n and rows[0] == {"t": "heard", "text": ""}
          and not any(x["t"] == "say" for x in rows), rows)
    r = client.post("/checkin/api/talk/voice?thread=nz-1", content=b"noise", headers={"content-type": "audio/wav"})
    check("noise: the voice route drops it too", len(sent) == n and r.json().get("heard") == "", r.json())
    # A short "Ja." Whisper is unsure about counts right after Carmen asked something.
    for h_ in [o for o in __import__("gc").get_objects() if isinstance(o, FakeHub)]:
        h_.convs.setdefault("general:nz-2", {"title": "", "messages": []})["messages"].append(
            {"role": "claude", "text": "Soll ich den Lauf eintragen?"})
    r = client.post("/checkin/api/talk/stream?thread=nz-2", content=b"ja-noisy", headers={"content-type": "audio/wav"})
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    check("noise: a short ja after a question still counts", len(sent) == n + 1 and rows[0]["text"] == "Ja.", rows)

# A late background result is saved as its own message and never spoken as the answer.
with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    real_send = FakeHub.send

    def send_with_late(self, key, title, text, **kw):
        out = real_send(self, key, title, text, **kw)
        self.convs[key]["messages"].append({"role": "claude", "text": "Quick update: the agent finished.", "unprompted": True})
        return out
    FakeHub.send = send_with_late
    r = client.post("/checkin/api/talk/stream?thread=late-1", content=b"audio", headers={"content-type": "audio/wav"})
    FakeHub.send = real_send
    rows = [_json.loads(x) for x in r.text.splitlines() if x.strip()]
    says = [x["text"] for x in rows if x["t"] == "say"]
    check("late result: never spoken as the answer", says and all("Quick update" not in s for s in says)
          and any("booked the run" in s for s in says), says)

# --- the chat client reads the stream between turns (tg_bridge/agent.py, Nov 9) ----------------
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, TaskNotificationMessage
import jobs.tg_bridge.agent as agent_mod


def _res(text):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                         session_id="s", result=text)


def _say(text):
    return AssistantMessage(content=[TextBlock(text=text)], model="m")


class FakeSDK:
    def __init__(self):
        self.q = asyncio.Queue()
        self.replies = {}

    async def query(self, prompt):
        for m in self.replies.get(prompt, [_say("a:" + prompt), _res("a:" + prompt)]):
            await self.q.put(m)

    async def _gen(self):
        while True:
            yield await self.q.get()

    def receive_messages(self):
        return self._gen()


async def _drain_case():
    got = []
    spec = agent_mod.ClientSpec(claude_id="x", resume=False, cwd="/tmp", cli_path="/bin/false", append="",
                                allowed=[], disallowed=[], env={}, model=None, can_use_tool=None,
                                permission_mode="default", on_unprompted=got.append)
    c = agent_mod.SdkClient(spec)
    sdk = FakeSDK()
    c._task = asyncio.create_task(c._own_draining(sdk))

    async def turn(prompt):
        await c.query(prompt)
        return [e.text for e in [e async for e in c.events()] if e.kind == "text"]

    a1 = await turn("one")
    # A background agent finishes between turns: its answer is already in the stream.
    await sdk.q.put(_say("Quick update: the agent finished."))
    await sdk.q.put(_res("Quick update: the agent finished."))
    await asyncio.sleep(0.05)
    a2 = await turn("two")
    # The task notification is in, its answer still on the way when the next query comes.
    await sdk.q.put(TaskNotificationMessage(subtype="task_notification", data={}, task_id="t", status="completed",
                                            output_file="", summary="", uuid="u", session_id="s"))
    await asyncio.sleep(0.05)

    async def late():
        await asyncio.sleep(0.3)
        await sdk.q.put(_say("Second update."))
        await sdk.q.put(_res("Second update."))
    asyncio.create_task(late())
    a3 = await turn("three")
    c._cmds.put_nowait(None)
    await asyncio.wait_for(c._task, 2)
    return a1, a2, a3, got

a1, a2, a3, got = asyncio.run(_drain_case())
check("drain: first turn answers itself", a1 == ["a:one"], a1)
check("drain: a result between turns is kept apart", got[:1] == ["Quick update: the agent finished."] and a2 == ["a:two"], (got, a2))
check("drain: a query waits for an answer under way", a3 == ["a:three"] and got[1:] == ["Second update."], (got, a3))

line_b = siri.approval_line({"speak": "Click Next step on cvs.com, after entering First name", "verb": "use the browser"}, False)
check("browser approval is spoken readably", line_b == "I need your OK in the browser: Click Next step on cvs.com, after entering First name. Say yes or no.", line_b)

# --- Nov 11: never hand back after only the preamble -------------------------------------
# talk-20261109-1444: "I'm not sure what you mean by that." was held as half-said ("that"), so the
# app went back to listening after he had finished; and a final answer that repeated the first
# sentence was dropped as "already said".
check("finished: a sentence ending in 'that.'", not siri.unfinished("I'm not sure what you mean by that."))
check("finished: 'I think so.'", not siri.unfinished("I think so."))
check("unfinished: a trailing 'und.' is still held", siri.unfinished("Ich wollte noch sagen und."))
check("unfinished: 'that' without a full stop is still held", siri.unfinished("I want to say that"))
check("unsaid: a final repeating the preamble gives the answer",
      siri.unsaid("Okay, I'll check the session log. The fix is live.", ["Okay, I'll check the session log."]) == "The fix is live.",
      siri.unsaid("Okay, I'll check the session log. The fix is live.", ["Okay, I'll check the session log."]))
check("unsaid: a final already spoken as a block", siri.unsaid("The fix is live.", ["Okay.", "The fix is live."]) == "")
check("unsaid: nothing spoken yet", siri.unsaid("Done.", []) == "Done.")
check("unsaid: an unrelated final is said whole", siri.unsaid("Ist drin.", ["Okay, ich trag es ein."]) == "Ist drin.")

import time as _t
import jobs.checkin_app.talkcall as talkcall


class ScriptHub(FakeHub):
    """A turn on a timeline: [(seconds, {"said": [...], "busy": bool, "final": text}), ...]."""
    script = []

    def send(self, key, title, text, **kw):
        sent.append({"key": key, "title": title, "text": text, **kw})
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        c["messages"].append({"role": "you", "text": text, "voice": True})
        self.t0 = getattr(self, "t0", {})
        self.t0[key], self.done_ = _t.time(), set()
        return self.state(key)

    def state(self, key):
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        t0 = getattr(self, "t0", {}).get(key)
        el = _t.time() - t0 if t0 else 0.0
        cur = {"said": [], "busy": False}
        for i, (at, st_) in enumerate(self.script):
            if el >= at:
                cur = st_
                if st_.get("final") and (key, i) not in self.done_:
                    self.done_.add((key, i))
                    c["messages"].append({"role": "claude", "text": st_["final"]})
        return {"key": key, "title": c["title"], "messages": c["messages"], "busy": bool(cur.get("busy")),
                "status": "", "approval": None, "said": list(cur.get("said") or [])}


def _stream(client, thread, audio=b"audio"):
    r = client.post(f"/checkin/api/talk/stream?thread={thread}", content=audio, headers={"content-type": "audio/wav"})
    return [_json.loads(x) for x in r.text.splitlines() if x.strip()]


_orig_tr = FakeTranscriber.transcribe


async def _tr(self, audio, name):
    if audio == b"that":
        return "I'm not sure what you mean by that.", "fake"
    return await _orig_tr(self, audio, name)
FakeTranscriber.transcribe = _tr

chat_mod.Hub = ScriptHub
siri.APP_WAIT_S = 2.0
PRE = "Okay, I'll check the session log."
with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    # The preamble at once, a long tool step, then the final, which repeats the preamble.
    ScriptHub.script = [(0, {"said": [], "busy": True}), (0.1, {"said": [PRE], "busy": True}),
                        (0.9, {"said": [], "busy": False, "final": PRE + "\n\nThe reply fix is live and working."})]
    rows = _stream(client, "pre-1")
    says = [x["text"] for x in rows if x["t"] == "say"]
    check("preamble turn: the preamble, then the answer", says == [PRE, "The reply fix is live and working."], says)
    check("preamble turn: done comes last, after the answer, not busy",
          rows[-1]["t"] == "done" and rows[-1]["busy"] is False and [x["t"] for x in rows].index("done") == len(rows) - 1, rows)
    # Still working after the wait: done says busy, so the app hands off and polls.
    ScriptHub.script = [(0, {"said": [], "busy": True}), (0.1, {"said": [PRE], "busy": True})]
    rows = _stream(client, "pre-2")
    check("slow turn: done carries busy true after the preamble", rows[-1] == {**rows[-1], "t": "done", "busy": True}
          and [x["text"] for x in rows if x["t"] == "say"] == [PRE], rows)
    # A queued turn starts right after (said resets): its first block is still spoken.
    ScriptHub.script = [(0, {"said": ["A1 first block."], "busy": True}), (0.3, {"said": [], "busy": True}),
                        (0.5, {"said": ["B1 second turn."], "busy": True}),
                        (0.9, {"said": [], "busy": False, "final": "B1 second turn."})]
    rows = _stream(client, "pre-3")
    says = [x["text"] for x in rows if x["t"] == "say"]
    check("queued turn: its blocks are spoken once", says == ["A1 first block.", "B1 second turn."], says)
    # A turn that failed: its note is spoken instead of nothing.
    ScriptHub.script = [(0, {"said": [], "busy": True}), (0.2, {"said": [], "busy": False})]
    real_state = ScriptHub.state

    def state_with_note(self, key):
        st_ = real_state(self, key)
        if key in getattr(self, "t0", {}) and not st_["busy"] and not any(m.get("role") == "note" for m in st_["messages"]):
            self.convs[key]["messages"].append({"role": "note", "text": "The turn did not complete: timeout."})
        return real_state(self, key)
    ScriptHub.state = state_with_note
    rows = _stream(client, "pre-4")
    ScriptHub.state = real_state
    check("failed turn: its note is spoken", any(x["t"] == "say" and "did not complete" in x["text"] for x in rows), rows)
    # The held sentence of Nov 9 goes to Claude now.
    ScriptHub.script = [(0, {"said": [], "busy": True}), (0.2, {"said": [], "busy": False, "final": "I meant the warning."})]
    n = len(sent)
    rows = _stream(client, "pre-5", b"that")
    check("'... by that.' is not held as half-said", rows[0]["t"] == "heard" and len(sent) == n + 1, rows)

# --- Nov 11: resume instead of restart ------------------------------------------------------
now = _t.time()
chat_mod.save({"key": "general:talk-20261111-0900", "title": "Talk: Iceland", "messages": [
    {"role": "you", "text": "Iceland review", "at": talkcall._iso(now - 1900)},
    {"role": "claude", "text": "Two slides left.", "at": talkcall._iso(now - 1800)}]})
check("resume: a non-talk thread is never aliased", talkcall.resolve("siri-20261111-1000") == ("siri-20261111-1000", None))
check("resume: over 2 h since the last Talk starts fresh",
      talkcall.resolve("talk-20261111-2300", now=now + 3 * 3600) == ("talk-20261111-2300", None))
with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    ScriptHub.script = [(0, {"said": [], "busy": True}), (0.2, {"said": [], "busy": False, "final": "Weiter mit Iceland?"})]
    _stream(client, "talk-20261111-1000")
    s1 = sent[-1]
    check("resume: a new Talk within 2 h continues the last thread", s1["key"] == "general:talk-20261111-0900", s1["key"])
    check("resume: Claude is told once", "continues the conversation" in s1["prompt_text"] and "30 min ago" in s1["prompt_text"],
          s1["prompt_text"][-300:])
    _stream(client, "talk-20261111-1000")
    s2 = sent[-1]
    check("resume: later turns stay there, without the note",
          s2["key"] == "general:talk-20261111-0900" and "continues the conversation" not in s2["prompt_text"], s2)
    r = client.post("/checkin/api/talk/voice?thread=talk-20261111-1000", content=b"", headers={"content-type": "audio/mp4"})
    check("resume: the voice route applies the alias", r.status_code == 200, r.status_code)
    logged = [_json.loads(l) for l in app.taplog.LOG.read_text().splitlines() if '"talk_resume"' in l]
    check("resume: logged", logged and logged[-1]["requested"] == "talk-20261111-1000"
          and logged[-1]["thread"] == "talk-20261111-0900", logged)

    # --- Nov 11: the call-end recap -----------------------------------------------------------
    pushes = []
    talkcall.PUSH = lambda title, msg, prio: pushes.append((title, msg, prio))
    talkcall.ASK = lambda prompt, data: ("PUSH: Talk: edited Reply Leo, drafted Leo reply\n"
                                         "RECAP: Edited Reply Leo and drafted the reply to Leo.")
    key = "general:talk-20261111-0900"
    app.taplog.log_event("chat", key=key, tools=["Read", "mcp__gws__draft_gmail_message"], reply="Drafted the reply to Leo.", test=False)
    app.taplog.log_event("signal", signal="chat_delta", key=key, touched=["Inbox/Reply Leo.md"], delta={})
    r = client.post("/checkin/api/talk/end?thread=talk-20261111-1000")
    j = r.json()
    check("end: answers with the resolved thread, recap queued",
          r.status_code == 200 and j == {"ok": True, "thread": "talk-20261111-0900", "recap": "queued"}, j)
    for _ in range(60):
        if pushes:
            break
        _t.sleep(0.05)
    check("end: one push with the recap", len(pushes) == 1 and pushes[0][1].startswith("Talk: edited Reply Leo"), pushes)
    last = chat_mod.load(key)["messages"][-1]
    check("end: the recap is written at the end of the thread",
          last.get("role") == "note" and last.get("recap") and last["text"].startswith("Recap: Edited"), last)
    r = client.post("/checkin/api/talk/end?thread=talk-20261111-1000")
    _t.sleep(0.3)
    check("end: a second call does not push again", r.json().get("recap") == "done" and len(pushes) == 1, (r.json(), pushes))
    r = client.post("/checkin/api/talk/end", json={"thread": "talk-never-seen"})
    check("end: an unknown Talk is fine", r.status_code == 200 and r.json().get("recap") == "none", r.json())
    r = client.post("/checkin/api/talk/end")
    check("end: no thread is tolerated", r.status_code == 200 and r.json().get("ok") is False, r.text)
    client.cookies.clear()
    r = client.post("/checkin/api/talk/end?thread=talk-20261111-1000")
    check("end: no login refused", r.status_code == 403, r.status_code)

# The idle fallback (Siri, app builds without the end call): due 3 min after the last turn.
talkcall.touch("talk-20261111-1500", "talk-20261111-1500", src="app", now=_t.time() - 240)
talkcall.touch("talk-20261111-1501", "talk-20261111-1501", src="app", now=_t.time() - 30)
talkcall.touch("talk-20261111-1502", "talk-20261111-1502", src="app", now=_t.time() - 240, test=True)
due = dict(talkcall.due(lambda t: False))
check("idle: due after 3 min, not before, never for test turns",
      "talk-20261111-1500" in due and "talk-20261111-1501" not in due and "talk-20261111-1502" not in due, list(due))
check("idle: a busy thread waits", "talk-20261111-1500" not in dict(talkcall.due(lambda t: t == "talk-20261111-1500")))
n = len(pushes)
s = due["talk-20261111-1500"]
check("idle: claimed once", talkcall.claim("talk-20261111-1500", s["seg"]) and not talkcall.claim("talk-20261111-1500", s["seg"]))
out = asyncio.run(talkcall.deliver("talk-20261111-1500", s, []))
check("idle: nothing changed, no push", out == {"pushed": False} and len(pushes) == n, (out, pushes[n:]))
s2 = talkcall.touch("talk-20261111-1500", "talk-20261111-1500", src="app")
check("idle: a turn after the recap opens a new segment", s2["seg"] == s["seg"] + 1 and not s2.get("recapped"), s2)
talkcall.ASK = lambda prompt, data: (_ for _ in ()).throw(RuntimeError("haiku down"))
f = {"tasks": ["Reply Leo", "Iceland review", "Opal"], "notes": [], "drafts": 1, "sent": 0, "calendar": 0,
     "answered": [], "waiting": [{"verb": "send"}], "turns": 2, "replies": []}
push, note, how = talkcall.compose(f)
check("recap without the model: plain words", how == "plain" and push == "Talk: edited 3 tasks, drafted 1 email, 1 needs your OK"
      and note.startswith("Recap: Tasks: Reply Leo"), (push, note))
check("recap: an approval waiting counts as a change", talkcall.changed({**f, "tasks": [], "drafts": 0}))

# --- Nov 11: a turn that outlives its stream is spoken once (talk-20261111-1659) ---------------
# A tool turn ran past the stream's wait, he spoke again, and Talk said the same content four
# times: the next stream re-spoke the first turn's lines and answer, then its own near-duplicate,
# and the poll delivered both once more.
PLANS = {"Does the Do pane change that?": {"dur": 2.0, "said": [(0.05, "Let me check what the Do pane shows."),
                                                                   (1.4, "Still checking which you ticked.")],
                                           "answer": "Yes, it changes a lot: only ECON is left."},
         "Just report what's undone.": {"dur": 0.5, "said": [(0.05, "Okay, I'll read out Do.")],
                                        "answer": "Only the ECON step is left."}}


async def _tr2(self, audio, name):
    if audio == b"long":
        return "Does the Do pane change that?", "fake"
    if audio == b"again":
        return "Just report what's undone.", "fake"
    return await _tr(self, audio, name)
FakeTranscriber.transcribe = _tr2


class TurnHub(FakeHub):
    """chat.Hub's turn semantics: ids on both messages, one turn at a time, a queue that runs as the
    next turn (with before_queued), said owned by the running turn."""
    before_queued = None
    all_prompts = []

    def __init__(self):
        super().__init__()
        self.run, self.queue, self.n, self.prompts = {}, {}, {}, []

    def send(self, key, title, text, **kw):
        sent.append({"key": key, "title": title, "text": text, **kw})
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        if key in self.run:
            q_ = self.queue.setdefault(key, {"turn": None, "texts": []})
            if q_["turn"] is None:
                self.n[key] = q_["turn"] = self.n.get(key, 0) + 1
            q_["texts"].append(kw.get("prompt_text") or text)
            c["messages"].append({"role": "you", "text": text, "turn": q_["turn"]})
            return {**self.state(key), "sent_turn": q_["turn"]}
        self.n[key] = t = self.n.get(key, 0) + 1
        c["messages"].append({"role": "you", "text": text, "turn": t})
        self._start(key, t, text, kw.get("prompt_text") or text)
        return {**self.state(key), "sent_turn": t}

    def _start(self, key, t, text, prompt):
        self.prompts.append(prompt)
        TurnHub.all_prompts.append((key, prompt))
        self.run[key] = {"turn": t, "t0": _t.time(), "plan": next(v for k_, v in PLANS.items() if k_ in text)}

    def state(self, key):
        c = self.convs.setdefault(key, {"title": "", "messages": []})
        r = self.run.get(key)
        if r and _t.time() - r["t0"] >= r["plan"]["dur"]:
            c["messages"].append({"role": "claude", "text": r["plan"]["answer"], "turn": r["turn"]})
            del self.run[key]
            q_ = self.queue.pop(key, None)
            if q_:
                note = self.before_queued(key, r["turn"]) if self.before_queued else ""
                self._start(key, q_["turn"], q_["texts"][0], note + q_["texts"][0])
            r = self.run.get(key)
        said = [s_ for at, s_ in r["plan"]["said"] if _t.time() - r["t0"] >= at] if r else []
        return {"key": key, "title": c["title"], "messages": c["messages"], "busy": bool(r), "status": "",
                "approval": None, "said": said, "turn": r["turn"] if r else None}


import threading
chat_mod.Hub = TurnHub
LATE = "Yes, it changes a lot: only ECON is left."
with TestClient(app.make_app(), base_url="https://testserver") as client:
    client.post("/checkin/login", data={"password": "pw"}, follow_redirects=False)
    # A: he speaks again while the long turn runs; the open stream says the late answer once.
    siri.APP_WAIT_S = 0.6
    rows1 = _stream(client, "late-a", b"long")
    check("late A: the first stream hands off with busy true after its own line",
          rows1[-1]["t"] == "done" and rows1[-1]["busy"] is True
          and [x["text"] for x in rows1 if x["t"] == "say"] == ["Let me check what the Do pane shows."], rows1)
    siri.APP_WAIT_S = 6.0
    poll_out = {}
    th = threading.Thread(target=lambda: poll_out.update(r=client.post(
        "/checkin/api/talk/voice?thread=late-a", content=b"", headers={"content-type": "audio/wav"}).json()))
    th.start()
    _t.sleep(0.15)
    rows2 = _stream(client, "late-a", b"again")
    th.join(10)
    says2 = [x["text"] for x in rows2 if x["t"] == "say"]
    check("late A: the next stream says the late answer once, framed, then its own",
          says2 == ["About what you asked before: " + LATE, "Okay, I'll read out Do.", "Only the ECON step is left."], says2)
    check("late A: the late turn's own lines are never re-spoken",
          not any("Let me check" in s_ or "Still checking" in s_ for s_ in says2), says2)
    check("late A: the poll in flight says nothing (the stream did)",
          poll_out.get("r", {}).get("reply") == "" and poll_out["r"].get("busy") is False, poll_out)
    r = client.post("/checkin/api/talk/voice?thread=late-a", content=b"", headers={"content-type": "audio/wav"}).json()
    check("late A: a later poll says nothing again", "ECON" not in r.get("reply", ""), r)
    check("late A: the queued turn's prompt starts with the note that the late answer is read first",
          [k_ for k_, p_ in TurnHub.all_prompts if p_.startswith(siri.LATE_QUEUED_NOTE)] == ["general:late-a"],
          [(k_, p_[:80]) for k_, p_ in TurnHub.all_prompts])
    # B: nobody speaks; the poll delivers the late answer once, the next turn neither repeats it nor
    # gets it again, and Claude hears that it was delivered.
    siri.APP_WAIT_S = 0.6
    _stream(client, "late-b", b"long")
    siri.APP_WAIT_S = 6.0
    r = client.post("/checkin/api/talk/voice?thread=late-b", content=b"", headers={"content-type": "audio/wav"}).json()
    check("late B: the poll delivers the late answer, unframed, not busy", r.get("reply") == LATE and r.get("busy") is False, r)
    rows3 = _stream(client, "late-b", b"again")
    says3 = [x["text"] for x in rows3 if x["t"] == "say"]
    check("late B: the next stream does not say it again", not any("changes a lot" in s_ for s_ in says3)
          and says3 == ["Okay, I'll read out Do.", "Only the ECON step is left."], says3)
    check("late B: Claude is told the earlier answer reached him late", siri.LATE_TOLD_NOTE in sent[-1]["prompt_text"],
          sent[-1]["prompt_text"][-300:])
    r = client.post("/checkin/api/talk/voice?thread=late-b", content=b"", headers={"content-type": "audio/wav"}).json()
    check("late B: a poll afterwards repeats nothing", "ECON" not in r.get("reply", "") and "changes a lot" not in r.get("reply", ""), r)
_k = "general:talk-hints-1"
_t0 = _t.time()
app.taplog.log_event("chat", key=_k, tools=["Edit"], hints=["/home/<user>/vault/Inbox/Reply Leo.md"], reply="Done.", test=False)
app.taplog.log_event("signal", signal="chat_delta", key=_k, touched=["Inbox/Reply Leo.md", "_system/checkin-app.md"], delta={})
_f = talkcall.facts(_k, _t0 - 5, [])
check("recap: a note no tool of the turn named is not counted (an edit from the Mac)",
      _f["tasks"] == ["Reply Leo"] and _f["notes"] == [], (_f["tasks"], _f["notes"]))
check("turn_answer: a recap note is never an answer",
      siri.turn_answer([{"role": "note", "text": "Recap: x", "turn": 3, "recap": True}], 3) == ("", False))
check("turn_answer: a failed turn's note is its answer",
      siri.turn_answer([{"role": "note", "text": "The turn did not complete.", "turn": 3}], 3) == ("The turn did not complete.", True))

print(f"\n{len(failures)} failed" if failures else "\nall passed")
sys.exit(1 if failures else 0)
