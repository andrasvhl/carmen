"""Canvas API responses on disk, so re-deriving notes costs no requests.

One principle, applied twice in this package: cache the expensive upstream
step, make everything downstream free to redo. `extract` caches pypdf's raw
parse; this caches Canvas's raw JSON. The links pass has always worked this
way, which is why "regenerating reading notes costs no requests" is true for
readings and used to be false for everything Canvas serves.

The cost this removes is real and measured: four full pulls on 2026-09-01 came
to roughly 12,000 requests against Stanford's servers, every one of them to fix
a problem that was entirely local - a rename, a link target, a folder name.
The module-alias work is the same shape again, which is why this comes first.

**The danger is the one this component keeps hitting.** A stale cache
regenerating stale notes while reporting a clean run looks exactly like
"nothing changed" - the same signature as the expired token, the empty
`--gated` worklist, and the fix that was never deployed. Four defences, and
the age guard is the load-bearing one:

  1. **A replay states its age, always.** `pull --from-cache` prints the date
     range of what it is about to replay before it replays it, and refuses
     outright past `--max-age` (7 days by default). There is no quiet path.
  2. **A miss is fatal, not empty.** `Miss` deliberately does *not* inherit
     from `CanvasError`, because `Client.try_paginate` turns a `CanvasError`
     into a status string and an empty section - which under `--prune` would
     delete every note in it. A miss has to unwind the run instead.
  3. **401 is never cached.** A dead token 401s on everything; caching that
     would bake "this course is unavailable" into every future replay, and
     `--prune` would then act on it. 403 and 404 are cached, because a
     concluded course genuinely does have its Quizzes tab turned off and a
     replay has to reproduce that.
  4. **A replay never touches `last_pull`.** It records `last_replay`, so
     `vt canvas status` keeps reporting when Canvas was actually asked.

Unlike the PDF cache, whose sha256 key makes a stale entry structurally
impossible, this is keyed on a URL whose content genuinely changes underneath
it. Hence the age guard here and none there.
"""

import datetime as dt
import hashlib
import json
import math
import os
import re
import urllib.parse
from typing import Any, Iterator

from runner import config

CACHE_DIR = config.STATE_DIR / "canvas-api"

# Refuse to replay anything older than this unless told otherwise. A week is
# well inside a quarter: course content moves, and a replay that silently
# served month-old modules would regenerate month-old notes.
DEFAULT_MAX_AGE_DAYS = 7

# Cached alongside the body so a replay reproduces pagination exactly: page 2's
# URL is itself a cache key, so following `Link: rel="next"` off a cached page 1
# walks the cached chain with no special casing.
_COURSE_IN_URL = re.compile(r"/courses/(\d+)")

# Only these. See defence 3 in the module docstring: 401 is the dead-token
# signal and must never become a cached fact about a course.
CACHEABLE_STATUSES = (403, 404)


class Miss(Exception):
    """This URL has never been recorded, and we are replaying rather than fetching.

    NOT a `CanvasError`, and that is the whole point. `try_paginate` catches
    `CanvasError` and returns `([], "error:...")`, which renders as a section
    with nothing in it - and `--prune` then deletes the notes that section used
    to hold. A miss must stop the run where it stands.
    """

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"not in the API cache: {url}\n"
                         "The cache holds only what a live pull has recorded. Run "
                         "`vt canvas pull` for this course once without --from-cache.")


class Offline(Exception):
    """A binary download was attempted during a cache replay.

    Nothing downloads the file bytes from cache - they are already in the vault
    under `_files/`, keyed on Canvas's `updated_at` plus size, so a replay of an
    already-archived course never asks. Reaching here means Canvas is offering a
    file this machine has not got, which a replay cannot supply and must not
    pretend to.
    """


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp() -> str:
    return _now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def course_key(url: str) -> str:
    """Which course's directory this URL belongs in.

    Most Canvas paths carry `/courses/<id>/`. The ones that do not - the
    enrolment list, and `files/<id>` metadata looked up by file id alone - go
    to `_shared`, so a per-course age report still covers everything a pull of
    that course replays.
    """
    m = _COURSE_IN_URL.search(urllib.parse.urlsplit(url).path)
    return m.group(1) if m else "_shared"


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


class Cache:
    """One directory of recorded responses, in either recording or replay mode.

    `record=True` writes every response through as it is fetched: that is the
    default for an ordinary pull, so the cache is a by-product of work already
    being done rather than a step anyone has to remember.

    `replay=True` never touches the network. The two are mutually exclusive in
    practice - a replay has nothing new to record.
    """

    def __init__(self, root=CACHE_DIR, *, record: bool = True, replay: bool = False):
        self.root = root
        self.record = record and not replay
        self.replay = replay
        self.hits = 0
        self.stored = 0

    # ---- paths ------------------------------------------------------------

    def path_for(self, url: str):
        return self.root / course_key(url) / f"{_key(url)}.json"

    # ---- reads ------------------------------------------------------------

    def load(self, url: str) -> dict[str, Any] | None:
        path = self.path_for(url)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # an unreadable entry is a miss, never a crash
        if not isinstance(entry, dict) or "outcome" not in entry:
            return None
        # Defence 3, applied on read as well as on write: an entry written by
        # an older version of this file could hold a 401. Treat it as absent.
        if entry.get("outcome") == "unavailable" and entry.get("status") == 401:
            return None
        self.hits += 1
        return entry

    # ---- writes -----------------------------------------------------------

    def store_ok(self, url: str, body: str, link: str) -> None:
        self._write(url, {"outcome": "ok", "body": body, "link": link})

    def store_unavailable(self, url: str, status: int) -> None:
        if status not in CACHEABLE_STATUSES:
            return
        self._write(url, {"outcome": "unavailable", "status": status})

    def _write(self, url: str, entry: dict[str, Any]) -> None:
        if not self.record:
            return
        entry["url"] = url          # for reading the cache by eye
        entry["fetched_at"] = _stamp()
        path = self.path_for(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(tmp, path)   # atomic: a killed run leaves no half entry
        except OSError:
            return                  # caching is an optimisation, never a requirement
        self.stored += 1


# ---- age, which is the whole safety story ---------------------------------

def entries(root=CACHE_DIR, course: str | None = None) -> Iterator[tuple[str, dict]]:
    """Every cached entry, optionally for one course directory."""
    if not root.exists():
        return
    dirs = [root / course] if course else sorted(
        p for p in root.iterdir() if p.is_dir())
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.json")):
            try:
                yield d.name, json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue


def survey(root=CACHE_DIR, courses: list[str] | None = None) -> dict[str, Any]:
    """Counts, bytes and the age range of what is cached.

    `oldest` is the number that matters: a replay is only as trustworthy as its
    stalest entry, not its freshest. Reporting the newest would be the flattering
    number and the wrong one.
    """
    per: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    if root.exists():
        wanted = set(courses) if courses else None
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            if wanted is not None and d.name not in wanted and d.name != "_shared":
                continue
            n = 0
            stamps: list[dt.datetime] = []
            size = 0
            for path in d.glob("*.json"):
                try:
                    size += path.stat().st_size
                except OSError:
                    continue
                n += 1
                try:
                    entry = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                when = _parse(entry.get("fetched_at", ""))
                if when:
                    stamps.append(when)
            if not n:
                continue
            total_bytes += size
            per[d.name] = {
                "entries": n,
                "bytes": size,
                "oldest": min(stamps) if stamps else None,
                "newest": max(stamps) if stamps else None,
            }
    allstamps = [v["oldest"] for v in per.values() if v["oldest"]]
    newest = [v["newest"] for v in per.values() if v["newest"]]
    return {
        "courses": per,
        "bytes": total_bytes,
        "entries": sum(v["entries"] for v in per.values()),
        "oldest": min(allstamps) if allstamps else None,
        "newest": max(newest) if newest else None,
    }


def age_days(when: dt.datetime | None) -> float | None:
    if when is None:
        return None
    return (_now() - when).total_seconds() / 86400.0


def check_age(view: dict[str, Any], max_age_days: float) -> str | None:
    """The refusal message, or None when the cache is fresh enough.

    Returning a message rather than raising keeps the decision - and the exact
    dates - in the caller's output, where the owner can see what it is refusing
    and how stale it actually is.
    """
    if not view["entries"]:
        return ("nothing is cached yet. The cache is a by-product of an ordinary "
                "`vt canvas pull`, so run one live first.")
    age = age_days(view["oldest"])
    if age is None:
        return "cached entries carry no timestamp, so their age cannot be established."
    if age > max_age_days:
        # Ceiling, not round: the displayed age is truncated to one decimal, so
        # suggesting `--max-age 41` for an entry 41.05 days old names an escape
        # hatch that refuses again. An off-by-a-rounding in a message is still a
        # message that lies about what will work.
        suggestion = math.ceil(age)
        return (f"the oldest cached response is {age:.1f} days old "
                f"({view['oldest']:%Y-%m-%d %H:%M} UTC), past the {max_age_days:g}-day "
                f"limit.\nReplaying it would regenerate notes from stale Canvas content "
                f"and report a clean run.\nEither run a live `vt canvas pull` to refresh "
                f"the cache, or pass --max-age {suggestion} to accept that age "
                f"deliberately.")
    return None
