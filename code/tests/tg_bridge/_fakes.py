"""Fakes shared by the Telegram bridge tests. No network, no SDK, no token.

  FakeTransport  the Bot API wire: records every call, answers from a script
  FakeAgent      a ClientFactory whose clients replay scripted Event streams
  make_bridge()  the whole Bridge wired to both, in a scratch directory
"""
import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from jobs.tg_bridge import media
from jobs.tg_bridge.__main__ import Bridge
from jobs.tg_bridge.agent import AgentPool, Event
from jobs.tg_bridge.approvals import Approvals
from jobs.tg_bridge.inbox import Inbox
from jobs.tg_bridge.sessions import SessionStore
from jobs.tg_bridge.telegram import Bot

OWNER = 4242
STRANGER = 777
TOKEN = "123456:SECRET-TOKEN-abcdef"


class Checks:
    def __init__(self, title: str):
        self.fails: list[str] = []
        print(f"\n{title}")

    def __call__(self, label: str, ok, detail="") -> bool:
        ok = bool(ok)
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if not ok and detail else ""))
        if not ok:
            self.fails.append(label)
        return ok


async def fast_sleep(seconds: float) -> None:
    fast_sleep.calls.append(seconds)
    await asyncio.sleep(0)


fast_sleep.calls = []


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple[str, dict, dict | None]] = []
        self.script: dict[str, list] = {}
        self.files: dict[str, bytes] = {}
        self.next_id = 5000
        self.log: list[str] = []          # shared ordering log with FakeAgent

    async def post(self, method, data, files, timeout):
        self.calls.append((method, dict(data), files))
        if method == "sendMessage":
            self.log.append(f"send:{data.get('text', '')[:40]}")
        queue = self.script.get(method)
        if queue:
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                reply = reply(data)
            if reply is not None:
                return reply
        if method in ("sendMessage", "sendDocument"):
            self.next_id += 1
            return {"ok": True, "result": {"message_id": self.next_id,
                                           "chat": {"id": data.get("chat_id")}}}
        if method == "getMe":
            return {"ok": True, "result": {"id": 123456, "is_bot": True, "username": "vaultbot"}}
        if method == "getFile":
            fid = data["file_id"]
            return {"ok": True, "result": {"file_id": fid, "file_size": 4096,
                                           "file_path": f"files/{fid}.oga"}}
        return {"ok": True, "result": True}

    async def fetch(self, file_path, max_bytes, timeout):
        return self.files.get(file_path, b"\x01" * 4096)

    async def close(self):
        pass

    def sent(self, method: str = "sendMessage") -> list[dict]:
        return [d for m, d, _ in self.calls if m == method]

    def texts(self) -> list[str]:
        return [d.get("text", "") for d in self.sent()]


class FakeClient:
    def __init__(self, agent: "FakeAgent", spec):
        self.agent = agent
        self.spec = spec
        self.prompt = ""
        self.interrupted = asyncio.Event()
        self.closed = False
        self.stderr = []

    async def connect(self):
        if self.agent.connect_error:
            err = self.agent.connect_error(self.spec)
            if err:
                raise err

    async def query(self, prompt):
        self.prompt = prompt
        self.agent.prompts.append(prompt)
        self.agent.transport_log.append(f"prompt:{prompt[-40:]}")

    async def events(self):
        a = self.agent
        a.active += 1
        a.max_active = max(a.max_active, a.active)
        try:
            async for event in a.behaviour(self, self.prompt):
                yield event
        finally:
            a.active -= 1

    async def interrupt(self):
        self.agent.interrupts += 1
        self.interrupted.set()

    async def disconnect(self, *, force=False):
        self.closed = True


async def answer_behaviour(client, prompt):
    await asyncio.sleep(client.agent.delay)
    last = prompt.strip().splitlines()[-1] if prompt.strip() else ""
    yield Event("text", text=f"answer to: {last}")
    yield Event("result", text=f"answer to: {last}", data={"is_error": False, "subtype": "success"})


async def interruptible_behaviour(client, prompt):
    """Runs until interrupted, then ends the way the CLI does (spike 6)."""
    yield Event("tool", name="Bash", id="t1", data={"command": "/home/<user>/automation/bin/vt todos"})
    try:
        await asyncio.wait_for(client.interrupted.wait(), 5)
    except asyncio.TimeoutError:
        pass
    yield Event("result", text="", data={"is_error": True, "subtype": "error_during_execution",
                                         "terminal_reason": "aborted_streaming"})


class FakeAgent:
    def __init__(self, transport_log: list[str] | None = None):
        self.specs = []
        self.clients: list[FakeClient] = []
        self.prompts: list[str] = []
        self.active = 0
        self.max_active = 0
        self.interrupts = 0
        self.delay = 0.05
        self.behaviour = answer_behaviour
        self.connect_error = None
        self.transport_log = transport_log if transport_log is not None else []

    def __call__(self, spec):
        self.specs.append(spec)
        client = FakeClient(self, spec)
        self.clients.append(client)
        return client


def fake_stt(text="Okay, kurz: email Quinn about Oslo", fail=()):
    calls = []

    async def post(url, headers, data, files, timeout):
        calls.append((url, data, files))
        if any(f in url for f in fail):
            return 500, {"error": "boom"}
        return 200, {"text": text}

    post.calls = calls
    return post


FAST_LIMITS = {"fresh": 2.0, "resume": 2.0, "between": 2.0, "tool": 2.0, "turn": 10.0,
               "tick": 0.01, "stop_grace": 1.0}


def make_bridge(*, limits=None, approval_timeout=1.0, stt=None, tmp=None, status_line=True):
    tmp = pathlib.Path(tmp or tempfile.mkdtemp())
    vault = tmp / "vault"
    (vault / "_system" / "telegram-bridge").mkdir(parents=True, exist_ok=True)
    transport = FakeTransport()
    bot = Bot(TOKEN, transport, sleep=fast_sleep, transport_factory=lambda: transport)
    inbox = Inbox(tmp / "tg.sqlite")
    store = SessionStore(tmp / "tg.sqlite")
    approvals = Approvals(bot, OWNER, timeout=approval_timeout)
    fake = FakeAgent(transport.log)
    pool = AgentPool(store, fake, cwd=vault, cli_path="/usr/bin/false",
                     style_path=vault / "_system" / "telegram-bridge" / "reply-style.md",
                     approver=approvals.request, projects_dir=tmp / "projects", env={},
                     limits={**FAST_LIMITS, **(limits or {})})
    transcriber = media.Transcriber(
        [media.Provider("groq", media.GROQ_URL, media.GROQ_MODEL, "gk"),
         media.Provider("openai", media.OPENAI_URL, media.OPENAI_MODEL, "ok")],
        post=stt or fake_stt())
    bridge = Bridge(bot, inbox, store, pool, approvals, transcriber, owner_id=OWNER,
                    vault=vault, mail=False, debounce=0.05, album_window=0.08,
                    split_paste_window=0.1, status_delay=0.0, status_line=status_line)
    bot.username = "vaultbot"
    # Never the real state dir: brief fixes would land in ~/.local/state.
    from jobs.tg_bridge import proactive
    bridge.fixes = proactive.FixLog(tmp / "brief-fixes.json")
    return bridge, transport, fake, tmp


_uid = [100]


def update(text=None, *, mid=None, chat=OWNER, sender=OWNER, chat_type="private", thread=None,
           uid=None, **extra) -> dict:
    _uid[0] += 1
    uid = uid or _uid[0]
    msg = {"message_id": mid or uid + 10000, "date": 1790000000,
           "chat": {"id": chat, "type": chat_type},
           "from": {"id": sender, "is_bot": False, "first_name": "A"}}
    if text is not None:
        msg["text"] = text
    if thread:
        msg["message_thread_id"] = thread
        msg["is_topic_message"] = True
    msg.update(extra)
    return {"update_id": uid, "message": msg}


def callback(data, *, sender=OWNER, chat=OWNER, message_id=1, uid=None) -> dict:
    _uid[0] += 1
    return {"update_id": uid or _uid[0], "callback_query": {
        "id": f"cb{_uid[0]}", "from": {"id": sender}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}


async def wait_for(predicate, timeout=3.0):
    end = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.005)
    return True
