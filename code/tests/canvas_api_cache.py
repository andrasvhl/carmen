#!/usr/bin/env python3
"""The API cache: replay fidelity, and every one of its four defences.

Runs against a stubbed transport and a scratch cache directory, so it touches
neither Canvas nor the vault.

    ~/automation/.venv/bin/python -m tests.canvas_api_cache

The defences are the point. A cache miss must stay fatal rather than becoming an
empty section, 401 must never be cached, and the stale-cache refusal must name a
--max-age that actually works.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import io, json, pathlib, sys, tempfile, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas import cache as cache_mod
from tools.canvas.api import Client, Unavailable, CanvasError

TMP = pathlib.Path(tempfile.mkdtemp())
ROOT = TMP / "canvas-api"

class FakeResp:
    def __init__(self, body, link=""):
        self._b = body.encode(); self.headers = {"Link": link}
    def read(self, n=-1): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False

def make_client(*, record=True, replay=False, script=None):
    c = Client("canvas.stanford.edu", "tok",
               cache=cache_mod.Cache(root=ROOT, record=record, replay=replay))
    calls = []
    def fake_open(url, **kw):
        calls.append(url)
        outcome = (script or {}).get(url, ("ok", '{"id": 1, "name": "hello"}', ""))
        if outcome[0] == "ok":
            return FakeResp(outcome[1], outcome[2])
        if outcome[0] == "unavailable":
            raise Unavailable(url, outcome[1])
        raise CanvasError(f"{url} boom")
    c._open = fake_open
    c.calls = calls
    return c

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

# 1. record then replay: same body, zero calls
c1 = make_client()
got1 = c1.get("courses/900102")
check("live fetch returns the body", got1 == {"id": 1, "name": "hello"})
check("live fetch made 1 request", len(c1.calls) == 1)

c2 = make_client(replay=True)
got2 = c2.get("courses/900102")
check("replay returns the identical body", got2 == got1, repr(got2))
check("replay made 0 requests", len(c2.calls) == 0, str(c2.calls))
check("replay counted a cache hit", c2.cache.hits == 1)

# 2. entry lands in the course's own directory, with a readable url + timestamp
files = list((ROOT / "900102").glob("*.json"))
check("entry filed under the course id", len(files) == 1, str(files))
entry = json.loads(files[0].read_text())
check("entry records the url", entry["url"].endswith("/courses/900102"), entry["url"])
check("entry records fetched_at", bool(cache_mod._parse(entry["fetched_at"])), entry["fetched_at"])

# 3. pagination replays through the Link chain
u1 = "https://canvas.stanford.edu/api/v1/courses/9/pages?per_page=100"
u2 = "https://canvas.stanford.edu/api/v1/courses/9/pages?page=2"
script = {u1: ("ok", '[{"a": 1}]', f'<{u2}>; rel="next"'), u2: ("ok", '[{"a": 2}]', "")}
c3 = make_client(script=script)
live = list(c3.paginate("courses/9/pages"))
check("live pagination follows Link", live == [{"a": 1}, {"a": 2}], str(live))
check("live pagination made 2 requests", len(c3.calls) == 2)
c4 = make_client(replay=True)
replayed = list(c4.paginate("courses/9/pages"))
check("replayed pagination is identical", replayed == live, str(replayed))
check("replayed pagination made 0 requests", len(c4.calls) == 0)

# 4. a 404 tab replays as unavailable -> try_paginate reproduces the status string
uq = "https://canvas.stanford.edu/api/v1/courses/9/quizzes?per_page=100"
c5 = make_client(script={uq: ("unavailable", 404)})
items, status = c5.try_paginate("courses/9/quizzes")
check("live disabled tab -> unavailable:404", (items, status) == ([], "unavailable:404"), status)
c6 = make_client(replay=True)
items, status = c6.try_paginate("courses/9/quizzes")
check("replayed disabled tab -> unavailable:404", (items, status) == ([], "unavailable:404"), status)
check("replayed disabled tab made 0 requests", len(c6.calls) == 0)

# 5. 401 is never cached -- the dead-token poison case
u401 = "https://canvas.stanford.edu/api/v1/courses/77/modules?per_page=100"
c7 = make_client(script={u401: ("unavailable", 401)})
try: list(c7.paginate("courses/77/modules"))
except Unavailable: pass
check("401 is not written to the cache", not (ROOT / "77").exists(), str(list(ROOT.iterdir())))

# 6. a miss is fatal and is NOT swallowed by try_paginate into an empty section
c8 = make_client(replay=True)
try:
    items, status = c8.try_paginate("courses/12345/assignments")
    check("miss must not become a status string", False, f"got ({items}, {status})")
except cache_mod.Miss:
    check("miss raises Miss through try_paginate", True)
check("Miss is not a CanvasError", not issubclass(cache_mod.Miss, CanvasError))

# 7. a transient CanvasError is not cached (a re-run must be able to succeed)
uerr = "https://canvas.stanford.edu/api/v1/courses/55/files?per_page=100"
c9 = make_client(script={uerr: ("error", None)})
items, status = c9.try_paginate("courses/55/files")
check("live hard error -> error status", status.startswith("error:"), status)
check("hard error is not cached", not (ROOT / "55").exists())

# 8. download refuses in replay mode
c10 = make_client(replay=True)
try:
    c10.download("https://x/y", str(TMP / "z.pdf"))
    check("download refuses under replay", False)
except cache_mod.Offline as e:
    check("download refuses under replay", True, str(e))

# 9. age guard
view = cache_mod.survey(root=ROOT)
check("survey counts entries", view["entries"] >= 4, str(view["entries"]))
check("fresh cache passes the age check", cache_mod.check_age(view, 7) is None)
old_view = dict(view, oldest=cache_mod._now() - dt.timedelta(days=30, minutes=90))
msg = cache_mod.check_age(old_view, 7)
check("stale cache is refused", msg is not None and "30.1 days old" in msg, str(msg))
# The suggestion has to actually work: 30.06 days is not accepted by --max-age 30.
suggested = float(msg.split("--max-age ")[1].split()[0])
check("the suggested --max-age actually passes",
      cache_mod.check_age(old_view, suggested) is None, f"suggested {suggested}")
check("empty cache is refused", cache_mod.check_age({"entries": 0}, 7) is not None)

# 10. record=False writes nothing
before = sum(1 for _ in ROOT.rglob("*.json"))
c11 = make_client(record=False)
c11.get("courses/999/brand-new")
after = sum(1 for _ in ROOT.rglob("*.json"))
check("--no-cache records nothing", before == after, f"{before} -> {after}")

# 11. course_key routing
check("course_key finds the course", cache_mod.course_key("https://h/api/v1/courses/12/pages") == "12")
check("course_key falls back to _shared", cache_mod.course_key("https://h/api/v1/files/99") == "_shared")

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
