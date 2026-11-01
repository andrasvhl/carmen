"""Follow the readings a course links out to, and archive their text too.

A Canvas course is half its own material and half a reading list pointing
somewhere else. One course in the archive links out ~50 times, so across the
MBA this is thousands of URLs - which is why it is a separate pass with its own
state, its own `--plan`, and its own rate limiting, rather than something the
Canvas pull does inline.

The hard part is not fetching. It is **not lying about what came back.** Around
a third of the links are behind a login or a paywall (HBSP coursepacks,
paid newsletters, LinkedIn), and a naive fetcher stores the paywall stub as
though it were the article - a note that looks archived, reads plausibly, and
is wrong. Every response is classified, and anything short of real text is
recorded as such with the URL intact.

Links are harvested from the notes already on disk, not from the API, so this
runs standalone against whatever has been archived and needs no re-pull.
"""

import hashlib
import os
import pathlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from typing import Any, Iterable

from runner import config
from runner.config import VAULT

from . import extract as extract_mod
from . import state as state_mod
from .layout import course_of, locate
from .render import safe_filename, write_note

CACHE_DIR = config.STATE_DIR / "canvas-readings"

# A real browser UA. Not deception: several of these hosts serve a stripped
# page to an unknown agent, and a stripped page is exactly the silent-wrong
# content this module exists to avoid.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

MAX_BYTES = 8 << 20
TIMEOUT = 25
# `TIMEOUT` bounds each read, not the download: a server trickling a few KB a
# second never trips it. One did on 2026-09-23 and held a 1-minute links pass
# for over ten. Past this, the reading is abandoned as `error:slow` and retried
# on the next run.
FETCH_DEADLINE = 60
DEFAULT_DELAY = 1.0          # seconds between requests to the same host

# Below this, whatever came back is not an article.
MIN_ARTICLE_CHARS = 400

# A reading saved by hand from a signed-in browser. Not "ok": the text is real,
# but its provenance is a person with a browser, not the fetcher, and the two
# should never be conflated in a note that claims to be an archive.
MANUAL = "manual"

# Statuses whose note carries the actual text underneath.
TEXT_STATUSES = ("ok", MANUAL)

# What `--gated` lists, and therefore what a dropped file is allowed to satisfy.
# robots-disallowed belongs here: the fetcher declined it on principle, but a
# person with a browser can still save it (Stanford library proxy links, mostly).
# It was missing until 2026-10-05, which hid 102 readings from the worklist and
# made adopt refuse a hand-saved HBR article.
GATED_STATUSES = ("login-required", "paywalled", "thin", "robots-disallowed")
# A paywall marker only counts as a paywall when the text is also short: real
# articles about subscriptions exist, and full-length ones are not stubs.
PAYWALL_MAX_CHARS = 4000

# Never fetched. Not "probably gated" - these return a login wall to any
# unauthenticated client, so attempting costs a request and teaches nothing.
LOGIN_HOSTS = {"hbsp.harvard.edu", "linkedin.com", "www.linkedin.com",
               "drive.google.com", "docs.google.com", "accounts.google.com"}
LOGIN_SUFFIXES = ("hbsp.harvard.edu", "linkedin.com")

# Fetched by nobody: there is no text at the other end, only media. Recorded as
# a link so the reading list stays complete.
MEDIA_HOSTS = {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com",
               "podcasts.apple.com", "music.apple.com", "open.spotify.com",
               "www.loom.com", "loom.com", "soundcloud.com"}

# Canvas itself, and the things that are not web pages at all.
SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:", "#")

PAYWALL_MARKERS = (
    "subscribe to continue", "sign in to continue", "to continue reading",
    "become a paid subscriber", "this post is for paid subscribers",
    "create a free account", "already a subscriber", "members only",
    "start your free trial", "subscribers only", "register to read",
)

# Stripped before dedup: the same reading arrives with different campaign tags
# from different sessions and would otherwise be fetched three times.
TRACKING_PARAMS = re.compile(r"^(utm_|mc_|_hs|fbclid|gclid|igshid|ref|source|s|si)$")

# Promotional blocks that sit INSIDE the article element, so boilerplate
# removal keeps them: newsletter sign-ups, "share this", author bylines, course
# CTAs. Only ever matched against short lines at the very start or end of the
# text, and only until the first line that does not match - a real paragraph
# mentioning "subscribe" mid-article is never touched.
PROMO_MARKERS = (
    "subscribe", "subscriber", "subscribing", "sign up for", "signup",
    "join the collective", "join the community", "join robin", "join our",
    "join 2", "follow us on", "follow me on", "ping me on", "find me on",
    "consider sharing", "share this post", "share it with friends",
    "free edition", "welcome to this month", "hey, robin here",
    "you might also like", "further reading", "related reading", "read next",
    "originally published", "preview the other", "start your free",
    "was this forwarded", "leave a comment", "restack", "view comments",
    "all rights reserved", "privacy policy", "privacy notice", "cookie",
    "commitment to your privacy", "terms of service", "newsletter",
)
# Sign-offs and bylines sit between the promo copy and the article ("Sincerely,",
# "Robin 👋"). They match nothing, so the scan is allowed to step over a short
# line - but a cut is only committed when the region also contains real promo.
PROMO_BRIDGE_LINE = 40
# A body paragraph is usually long; a promo line is usually short. Combined
# with the end-anchored scan this is what keeps the trimmer from eating prose.
PROMO_MAX_LINE = 300
# Never remove more than this share of the article, whatever the patterns say.
PROMO_MAX_FRACTION = 0.15
PROMO_MAX_LINES = 12

MD_LINK = re.compile(r"\]\((https?://[^\s)]+)\)")
BARE_LINK = re.compile(r"<(https?://[^\s>]+)>")


# ---- urls -----------------------------------------------------------------

def normalise(url: str) -> str:
    """One canonical form per destination, for dedup and caching."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url
    query = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
             if not TRACKING_PARAMS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((
        p.scheme.lower(), p.netloc.lower(), path,
        urllib.parse.urlencode(query), ""))


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


def classify_host(url: str) -> str:
    """"fetch" | "login-required" | "media" | "skip", before any request."""
    if url.startswith(SKIP_SCHEMES):
        return "skip"
    host = host_of(url)
    if not host or host.endswith("canvas.stanford.edu"):
        return "skip"
    if "panopto" in host:
        return "media"
    if host in MEDIA_HOSTS:
        return "media"
    # Subdomains too: `services.hbsp.harvard.edu` serves the coursepack PDFs,
    # and a script fetching it is not refused but stalled - it held a links
    # pass for twenty minutes on 2026-09-23 before this matched suffixes.
    if host in LOGIN_HOSTS or any(host.endswith("." + h) for h in LOGIN_SUFFIXES):
        return "login-required"
    return "fetch"


def key_of(url: str) -> str:
    return hashlib.sha1(normalise(url).encode("utf-8")).hexdigest()


# ---- harvesting -----------------------------------------------------------

# An internal link to a reading note: `[[.../Readings/<name>|label]]`.
_READING_LINK = re.compile(r"\[\[([^\]|#\\]*?/Readings/[^\]|#\\]+?)(?:\.md)?(?=[\]|#\\])")


def harvest(root, course_filter: set[str] | None = None,
            data: dict | None = None) -> dict[str, dict]:
    """Every outbound URL in the archive, with the notes it came from.

    Reads what was written rather than re-querying Canvas, so this pass is
    independent of a pull and can be re-run after one.

    With `data`, internal links to reading notes count as sources too. The
    links pass replaces a reading's URL in the course notes with a link to its
    note, so from the second run on the URL is gone and the reading had no
    visible source: no "Linked from" line, and no way to find its session. 28
    readings sat in _Unfiled/ that way while a session page linked to them
    (2026-09-23). The note path is mapped back to its reading through state.
    """
    found: dict[str, dict] = {}
    by_note: dict[str, str] = {}
    for k, r in ((data or {}).get("readings") or {}).items():
        for n in (r.get("notes") or {}).values():
            by_note[n] = k
    for note in sorted(root.rglob("*.md")):
        rel = note.relative_to(root)
        parts = rel.parts
        if len(parts) < 2 or parts[0] == "_files":
            continue
        # Reading notes contain the URL they were written for, so harvesting
        # them makes every reading cite itself as a source.
        if "Readings" in parts:
            continue
        course = course_of(parts)
        if not course or len(parts) < 2:
            continue
        if course_filter and course not in course_filter:
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _READING_LINK.finditer(text):
            k = by_note.get(m.group(1))
            if not k:
                continue
            known = data["readings"][k]
            rec = found.setdefault(k, {"url": known.get("url", ""), "courses": set(),
                                       "sources": set(), "titles": []})
            rec["courses"].add(course)
            rec["sources"].add(str(rel))
        for m in list(MD_LINK.finditer(text)) + list(BARE_LINK.finditer(text)):
            raw = m.group(1).rstrip(".,;)")
            k = key_of(raw)
            rec = found.setdefault(k, {"url": normalise(raw), "courses": set(),
                                       "sources": set(), "titles": []})
            rec["courses"].add(course)
            rec["sources"].add(str(rel))
            title = _link_title(text, m.start())
            # First label seen wins. Sorting picked "A Field Research
            # Handbook..." over the "Grow by Choosing the Right Road"
            # the page actually says, purely on the alphabet.
            if title and title not in rec["titles"]:
                rec["titles"].append(title)
    return found


def _link_title(text: str, at: int) -> str:
    """The `[...]` label immediately before a markdown link, if there is one."""
    close = text.rfind("[", max(0, at - 300), at)
    if close < 0:
        return ""
    label = text[close + 1:at].strip()
    if not label or "\n" in label or len(label) >= 200:
        return ""
    # The label is markdown, so a bolded link gives "**AARRR Framework**" and
    # that ends up in the note's H1 and filename.
    label = re.sub(r"[*_`]+", "", label).strip()
    return label


# ---- fetching -------------------------------------------------------------

class Fetcher:
    def __init__(self, *, delay: float = DEFAULT_DELAY, respect_robots: bool = True,
                 verbose: bool = False):
        self.delay = delay
        self.respect_robots = respect_robots
        self.verbose = verbose
        self._last: dict[str, float] = {}
        self._robots: dict[str, Any] = {}
        # Hosts that would not even connect this run. Each attempt at one
        # costs the full TIMEOUT, and the robots.txt check pays it once more
        # first, so two readings on a dead blog cost ~100 seconds; a links
        # pass of 84 requests took 15 minutes on 2026-09-23. One failure to
        # connect writes the host off for the rest of the run - next run
        # tries again.
        self._dead: set[str] = set()
        self.requests = 0

    @staticmethod
    def _unreachable(e: Exception) -> bool:
        """Did this fail before any HTTP exchange - DNS, refusal, timeout?"""
        if isinstance(e, urllib.error.HTTPError):
            return False  # the server answered; it is alive
        reason = getattr(e, "reason", e)
        return isinstance(reason, (TimeoutError, ConnectionError, OSError))

    def _wait(self, host: str) -> None:
        gap = time.monotonic() - self._last.get(host, 0.0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        host = host_of(url)
        rp = self._robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f"https://{host}/robots.txt")
            # robotparser.read() must NOT be used here. It fetches with the
            # default Python User-Agent, which several of these hosts answer
            # with 403 - and read() turns a 403 on robots.txt into
            # `disallow_all = True`, silently. The first real run reported 24
            # of 39 readings as robots-disallowed, including pages whose
            # robots.txt plainly allows them. Fetch it ourselves, with the
            # same UA the actual request will use.
            try:
                self._wait(host)
                req = urllib.request.Request(f"https://{host}/robots.txt",
                                             headers={"User-Agent": UA})
                self.requests += 1
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = r.read(512_000).decode("utf-8", "replace")
                rp.parse(body.splitlines())
            except Exception as e:
                # No robots.txt, or unreachable. Absent means allowed - which
                # is the standard's own default, not a convenience.
                if self._unreachable(e):
                    self._dead.add(host)
                rp.parse([])
            self._robots[host] = rp
        try:
            return rp.can_fetch(UA, url)
        except Exception:
            return True

    def fetch(self, url: str) -> tuple[bytes, str, str, str]:
        """Returns (body, content_type, final_url, status).

        status is "ok", "login-required", "robots-disallowed" or "error:...".
        Never raises: a bad link is a fact to record, not a run-ending event.
        """
        host = host_of(url)
        if host in self._dead:
            return b"", "", url, "error:host-unreachable"
        if not self.allowed(url):
            return b"", "", url, "robots-disallowed"
        if host in self._dead:  # robots.txt found it dead just now
            return b"", "", url, "error:host-unreachable"
        self._wait(host)
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        try:
            self.requests += 1
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = _read_with_deadline(r)
                if body is None:
                    return b"", "", url, "error:slow"
                return body, r.headers.get("Content-Type", ""), r.geturl(), "ok"
        except urllib.error.HTTPError as e:
            if e.code in (401, 402, 403, 451):
                return b"", "", url, "login-required"
            return b"", "", url, f"error:HTTP {e.code}"
        except Exception as e:
            if self._unreachable(e):
                self._dead.add(host)
            return b"", "", url, f"error:{type(e).__name__}"


def tidy_article(text: str) -> tuple[str, int]:
    """Strip newsletter and marketing blocks from the ends of an article.

    trafilatura already removes page structure - nav, sidebars, footers,
    related-article lists - so what is left is the article element. What it
    cannot remove is promotional copy the author put *inside* that element:
    a newsletter's "Join the community", a closing "consider sharing this
    with friends", an opening "Hey, Robin here!".

    Deliberately timid. It only walks inward from each end, stops at the first
    line that does not look promotional, ignores long lines, and refuses to cut
    more than a sixth of the text. Losing a paragraph of a case reading is a
    worse outcome than keeping a sign-up box.
    """
    lines = [ln for ln in text.strip().splitlines()]
    if not lines:
        return text, 0
    original_len = len(text)
    budget = int(original_len * PROMO_MAX_FRACTION)

    def promo(line: str) -> bool:
        s = line.strip()
        if not s or len(s) > PROMO_MAX_LINE:
            return False
        low = s.lower()
        return any(m in low for m in PROMO_MARKERS)

    def scan(order: list[int]) -> list[int]:
        """Walk inward collecting a candidate region; commit only on promo."""
        region: list[int] = []
        chars = 0
        saw_promo = False
        for i in order:
            s = lines[i].strip()
            if len(region) >= PROMO_MAX_LINES or chars + len(s) > budget:
                break
            if not s:
                region.append(i); continue
            if promo(s):
                region.append(i); chars += len(s); saw_promo = True; continue
            if len(s) <= PROMO_BRIDGE_LINE:
                region.append(i); chars += len(s); continue
            break
        return region if saw_promo else []

    head = scan(list(range(len(lines))))
    tail = scan(list(range(len(lines) - 1, len(head) - 1, -1)))
    start = len(head)
    end = len(lines) - len(tail)
    if end <= start:
        return text.strip(), 0

    trimmed = "\n".join(lines[start:end]).strip()
    # If the patterns matched almost everything, they were wrong about what
    # this page is. Keep the original rather than publish a fragment.
    if len(trimmed) < original_len * 0.5:
        return text.strip(), 0
    return trimmed, original_len - len(trimmed)


# A publisher's purchase box. Unlike the soft markers above these never appear
# in an article's own prose, so they count against any page whose body is short
# enough to be a stub - and "short" is measured without the reference list,
# which is what let an abstract plus 238 references (Nature, 2026-09-22) pass
# as a 47 KB article.
PURCHASE_MARKERS = (
    "buy this article", "purchase this article", "rent this article",
    "purchase on springerlink", "subscribe to this journal", "access options",
    "get full access", "unlock full access", "access through your institution",
    "log in via an institution", "purchase pdf", "check access",
    "subscribe to read", "to read the full story", "you've reached your limit",
    "you have reached your limit", "free articles left", "subscribe now to read",
)
STUB_BODY_MAX = 6000

# Publishers whose pages hold the rest of the article back by script. A plain
# fetch gets the first paragraph or two and no marker at all - HBR's "Why your
# brain loves good storytelling" came back as one paragraph, 447 chars, "ok".
# From these hosts a short result is a teaser, not an article.
METERED_HOSTS = {
    "hbr.org", "www.statnews.com", "statnews.com", "endpoints.news",
    "www.wsj.com", "wsj.com", "www.nytimes.com", "nytimes.com", "www.ft.com",
    "ft.com", "www.economist.com", "www.bloomberg.com", "bloomberg.com",
    "www.washingtonpost.com", "www.theinformation.com", "www.nature.com",
    "www.adweek.com", "www.businessinsider.com", "www.theatlantic.com",
    "www.newyorker.com", "sloanreview.mit.edu", "www.fastcompany.com",
    "www.sciencedirect.com", "www.tandfonline.com", "journals.sagepub.com",
    "onlinelibrary.wiley.com", "link.springer.com", "www.jstor.org",
}
TEASER_MAX = 2500

_REFS_HEAD = re.compile(
    r"^#{1,4}\s*(references|bibliography|works cited|footnotes|notes)\s*$", re.I | re.M)


def _article_body(text: str) -> str:
    """The text before any reference list: what the length tests should see."""
    m = _REFS_HEAD.search(text)
    return text[:m.start()] if m else text


def _read_with_deadline(r) -> bytes | None:
    """The response body, up to MAX_BYTES, or None past FETCH_DEADLINE."""
    start, chunks, got = time.monotonic(), [], 0
    while got < MAX_BYTES:
        chunk = r.read(min(64 << 10, MAX_BYTES - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
        if time.monotonic() - start > FETCH_DEADLINE:
            return None
    return b"".join(chunks)


def classify_response(text: str, final_url: str, original: str) -> str:
    """Did an article actually come back? "ok" | "paywalled" | "thin" | ... """
    if _looks_like_login(final_url) and host_of(final_url) != host_of(original):
        return "login-required"
    body = _article_body(text).strip()
    n = len(body)
    if n < MIN_ARTICLE_CHARS:
        return "thin"
    low = text.lower()
    if n < STUB_BODY_MAX and any(mark in low for mark in PURCHASE_MARKERS):
        return "paywalled"
    if n < PAYWALL_MAX_CHARS and any(mark in low for mark in PAYWALL_MARKERS):
        return "paywalled"
    hosts = {host_of(final_url), host_of(original)}
    if n < TEASER_MAX and hosts & METERED_HOSTS:
        return "paywalled"
    # A body that stops mid-thought on an ellipsis was cut off by the page.
    if n < 3 * TEASER_MAX // 2 and body.endswith(("\u2026", "...")):
        return "paywalled"
    return "ok"


def _looks_like_login(url: str) -> bool:
    u = url.lower()
    return any(w in u for w in ("/login", "/signin", "/sign-in", "/auth/", "accounts."))


# ---- the pass -------------------------------------------------------------

def records(root, data: dict, course_filter: set[str] | None = None) -> dict[str, dict]:
    """Every reading, from the notes on disk plus everything the cache remembers.

    Harvesting alone is not enough. Internalising REPLACED the URLs in those
    notes with wikilinks, so a second harvest finds almost nothing and a deleted
    reading note could never be rebuilt. The cache remembers every URL ever seen
    and which course it belonged to, so it seeds the rest.

    Shared by `run` and `adopt` deliberately. This used to live inside `run`,
    and the one thing this package has proved repeatedly is that a second reader
    of the same data drifts from the first - the `fetched` to
    `canvas_fetch_status` rename broke two separate readers before anyone
    noticed.
    """
    found = harvest(root, course_filter, data)
    for k, known in (data.get("readings") or {}).items():
        for course in (known.get("notes") or {}):
            if course_filter and course not in course_filter:
                continue
            # A course that was renamed leaves its old name in the cache, and
            # writing to it recreates the folder that was just deleted.
            if not locate(root, course).is_dir():
                continue
            rec = found.setdefault(k, {"url": known.get("url", ""), "courses": set(),
                                       "sources": set(), "titles": [], "modules": {}})
            rec["courses"].add(course)
            # The recorded note path already says which module it belongs to.
            # Without this a cache-seeded reading loses its session and every
            # one of them lands in _Unfiled.
            parts = (known["notes"][course] or "").split("/")
            if len(parts) >= 3 and parts[-2] == "Readings":
                rec.setdefault("modules", {})[course] = parts[-3]
            if known.get("title") and known["title"] not in rec["titles"]:
                rec["titles"].append(known["title"])
    return found


def run(root, *, course_filter: set[str] | None = None, force: bool = False,
        limit: int | None = None, delay: float = DEFAULT_DELAY,
        plan_only: bool = False, prune: bool = False,
        verbose: bool = False) -> dict[str, Any]:
    """Fetch every outbound reading and write one note per course per link."""
    try:
        import trafilatura  # type: ignore
    except ImportError:
        if not plan_only:
            return {"error": "trafilatura is not installed — "
                             "~/automation/.venv/bin/pip install -r ~/automation/requirements.txt"}
        trafilatura = None  # type: ignore

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = state_mod.load()
    cache: dict[str, dict] = data.setdefault("readings", {})
    found = records(root, data, course_filter)

    buckets: dict[str, list[str]] = {}
    for k, rec in found.items():
        buckets.setdefault(classify_host(rec["url"]), []).append(k)

    if plan_only:
        hosts: dict[str, int] = {}
        for rec in found.values():
            hosts[host_of(rec["url"])] = hosts.get(host_of(rec["url"]), 0) + 1
        return {"plan": True, "unique": len(found), "buckets":
                {k: len(v) for k, v in buckets.items()}, "hosts": hosts}

    fetcher = Fetcher(delay=delay, verbose=verbose)
    tally: dict[str, int] = {}
    written_notes: set[str] = set()
    moved: list[tuple[str | None, str]] = []
    fetched_now = 0

    for k, rec in sorted(found.items(), key=lambda kv: kv[1]["url"]):
        kind = classify_host(rec["url"])
        if kind == "skip":
            continue

        cached = cache.get(k)
        # `fetched` was renamed to `canvas_fetch_status` and this reader was
        # missed, same as in `gated()`. Here it did not fail quietly: the get
        # returned None, None != "pending" is true, and the next line raised
        # KeyError - so the links pass crashed on any reading already in the
        # cache, and only ever worked against an empty one. Read the status
        # once and require it, rather than inferring presence from "not
        # pending".
        cached_status = (cached or {}).get("canvas_fetch_status")
        # `--force` must never refetch a hand-saved reading. Refetching would
        # get the login wall it always gets, overwrite the cache with nothing,
        # and destroy a PDF that cost a trip to a signed-in browser - the only
        # text in this archive that cannot be re-derived from anywhere.
        if cached_status == MANUAL:
            status, text = MANUAL, _read_cache(k)
        # `error:*` is a failed fetch, not a verdict: the link may work next
        # time, so it is retried every run. It used to be cached like any
        # other status and never tried again short of --force, though the
        # docs promised a re-run could succeed. Dead hosts now cost one
        # attempt per run (`Fetcher._dead`), which is what makes this cheap.
        elif (cached and not force and cached_status and cached_status != "pending"
              and not cached_status.startswith("error:")):
            status, text = cached_status, _read_cache(k)
            # Re-judge cached text on every run. It costs no request, and it is
            # how a better classifier reaches readings fetched before it
            # existed: 2026-09-23's paywall rules found teasers stored as "ok".
            if status == "ok" and text:
                status = classify_response(text, rec["url"], rec["url"])
        elif kind in ("media", "login-required"):
            status, text = kind, ""
        elif limit is not None and fetched_now >= limit:
            continue
        else:
            fetched_now += 1
            body, ctype, final, status = fetcher.fetch(rec["url"])
            text = ""
            if status == "ok":
                if "pdf" in ctype.lower() or rec["url"].lower().endswith(".pdf"):
                    text, status = _pdf_from_bytes(body, k)
                else:
                    html = body.decode("utf-8", "replace")
                    # favor_precision drops author bylines and trailing CTAs
                    # that the default mode keeps; measured at a ~3% cost on
                    # real article text and a no-op on most pages.
                    # markdown output, not plain text: it costs about 1% more
                    # characters and keeps the paragraph breaks, headings and
                    # emphasis the author wrote. Plain text ran every article
                    # together into one unreadable block.
                    text = trafilatura.extract(
                        html, include_comments=False, include_tables=True,
                        include_links=False, include_images=False,
                        favor_precision=True, deduplicate=True,
                        output_format="markdown") or ""
                    text, trimmed = tidy_article(text)
                    if verbose and trimmed:
                        print(f"    {'trimmed':16s} {trimmed:>7} chars of promo copy")
                    status = classify_response(text, final, rec["url"])
                    if not rec["titles"]:
                        meta = trafilatura.extract_metadata(html)
                        if meta and meta.title:
                            rec["titles"].append(meta.title)
            if text:
                _write_cache(k, text)
            if verbose:
                print(f"    {status:16s} {len(text):>7} {rec['url'][:90]}")

        # Update the entry, never rebuild it. Rebuilding kept four named
        # fields and silently dropped the rest - `manual_file` and `assigned`
        # among them - so every links run erased which hand-saved file answered
        # which reading, and the next adopt re-scanned files it had already
        # filed (found 2026-09-23 on the first `refresh`).
        entry = dict(cache.get(k) or {})
        entry.update({"url": rec["url"], "canvas_fetch_status": status,
                      "canvas_fetched_at": (entry.get("canvas_fetched_at")
                                            if status == MANUAL else state_mod.now()),
                      "chars": len(text),
                      "title": rec["titles"][0] if rec["titles"] else entry.get("title", ""),
                      "notes": entry.get("notes", {})})
        cache[k] = entry
        tally[status] = tally.get(status, 0) + 1
        before = dict(cache[k].get("notes") or {})
        notes = _write_reading_notes(root, rec, k, status, text, cache[k]["title"])
        moved.extend((before.get(c), n) for c, n in notes.items() if before.get(c) != n)
        cache[k]["notes"] = {**before, **notes}
        # notes.values() are vault-relative paths without the extension.
        written_notes.update(str(VAULT / f"{n}.md") for n in notes.values())

    # Apply the rewrite to notes already on disk, so it takes effect without
    # waiting for another pull. The pull produces the same output from the same
    # cache, so this converges rather than fighting it.
    tally["links_internalised"] = rewrite_sources(root, data, course_filter)
    tally["relinked"] = _relink(root, moved)
    if prune:
        gone = prune_readings(root, written_notes, course_filter, dry_run=False)
        tally["pruned"] = len(gone)

    state_mod.save(data)
    tally["gated"] = sum(tally.get(s, 0) for s in
                         ("login-required", "paywalled", "thin", "robots-disallowed"))
    return {"tally": tally, "unique": len(found), "requests": fetcher.requests}


_READING_KIND = re.compile(r"^canvas_kind:[ \t]*\"?reading\"?[ \t]*$", re.M)


def _is_reading_note(note) -> bool:
    try:
        with open(note, encoding="utf-8", errors="replace") as fh:
            head = fh.read(800)
    except OSError:
        return False
    return head.startswith("---") and bool(_READING_KIND.search(head.split("\n---", 1)[0]))


def prune_readings(root, keep: set[str], course_filter: set[str] | None = None,
                   *, dry_run: bool = False) -> list[str]:
    """Remove reading notes this run did not write.

    Readings are named after their link label, so a changed label leaves the
    old note behind. Only `Readings/` folders are touched; nothing else in the
    archive is this pass's to remove.
    """
    removed: list[str] = []
    touched = set()
    for k in keep:
        try:
            touched.add(course_of(pathlib.Path(k).relative_to(root).parts))
        except ValueError:
            pass
    for note in sorted(root.rglob("*.md")):
        parts = note.relative_to(root).parts
        if "Readings" not in parts or parts[0] == "_files":
            continue
        if course_filter and course_of(parts) not in course_filter:
            continue
        # Guard (2026-09-27, owner decision): never prune a course this run did not
        # write a single reading for. A dropped course is no longer listed on
        # Canvas, so an all-courses `links --prune` would otherwise sweep its
        # reading notes while producing nothing for it. Whitelist by course,
        # the same shape as archive.prune, so his archive of a dropped class
        # survives every run.
        if course_of(parts) not in touched:
            continue
        if str(note) in keep:
            continue
        # Only a note this pass wrote is its to remove, and every one it writes
        # says so in its frontmatter. Before this, any note in a Readings/
        # folder that the run had not just written was deleted - including a
        # note of the owner's own, which is the blacklist shape that once cost
        # 22 PDFs in the main prune.
        if not _is_reading_note(note):
            continue
        removed.append(str(note))
        if not dry_run:
            note.unlink(missing_ok=True)
    return removed


def rewrite_sources(root, data: dict, course_filter: set[str] | None = None) -> int:
    """Point every archived reading link in the course notes at its note."""
    changed = 0
    for note in sorted(root.rglob("*.md")):
        parts = note.relative_to(root).parts
        if len(parts) < 2 or parts[0] == "_files" or "Readings" in parts:
            continue
        if course_filter and course_of(parts) not in course_filter:
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        new = internalise(text, data, course_of(parts))
        if new != text:
            note.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def gated(data: dict, course_filter: set[str] | None = None) -> list[dict]:
    """The worklist for the browser pass: what a logged-in session must get.

    Takes the state rather than a vault root. It used to take `root`, ignore it,
    and load the state file itself - which reads fine and is untestable, because
    the answer depended on whichever machine's `canvas.json` happened to be
    there. A test asserting on it passed on the Mac, whose canvas.json is a stale
    stub, and failed on the box, which has the real 685.

    It counts from the reading cache and not from the notes on disk, which is
    deliberate and load-bearing: one URL can produce a note in several courses,
    and a pruned note leaves its cache entry behind. Counting the vault once gave
    22 where the honest answer was 33.
    """
    cache = (data or {}).get("readings", {})
    # The cache key is `canvas_fetch_status`. This read `fetched`, which the
    # fetch pass stopped writing when the property was renamed, so the worklist
    # came back empty for every run and reported "nothing gated" while 22
    # readings sat behind a login. An empty worklist is indistinguishable from
    # a clean one, which is why this went unnoticed.
    # `--course` used to be accepted and ignored here, so asking for one
    # course's worklist printed the whole archive's 695.
    return sorted(
        ({"url": r["url"], "status": r["canvas_fetch_status"],
          "title": r.get("title", ""),
          "courses": sorted((r.get("notes") or {}).keys())}
         for r in cache.values()
         if r.get("canvas_fetch_status") in GATED_STATUSES
         and (not course_filter or course_filter & set((r.get("notes") or {}).keys()))),
        key=lambda r: (r["courses"][:1], r["status"], r["url"]))


def internal_target(data: dict, url: str, course: str) -> str | None:
    """Vault path of the reading note for this URL, if one exists.

    Both the pull and the links pass consult this, so a link rewritten by one
    is produced identically by the other and a re-pull does not revert it.
    """
    rec = (data.get("readings") or {}).get(key_of(url))
    return _existing((rec or {}).get("notes", {}).get(course))


def _existing(note: str | None) -> str | None:
    """`note` if that vault note is on disk, else None.

    State can name a note that is gone - pruned after a title change, or lost
    to a case clash (`Syllabus.md` beside `syllabus.md`). Linking to it anyway
    wrote 9 dead links on 2026-09-23. Without a target the caller keeps the
    original URL, which still works, and the next links pass internalises it
    once the note exists again.
    """
    if not note:
        return None
    path = VAULT / (note if note.endswith(".md") else note + ".md")
    return note if path.is_file() else None


CANVAS_FILE_URL = re.compile(r"https?://[^/\s]*canvas[^/\s]*(?:/courses/(\d+))?/files/(\d+)")


def canvas_file_target(data: dict, url: str) -> str | None:
    """Vault path of the archived note for a Canvas file URL, if there is one.

    A page that links to a course file carries an ordinary Canvas URL, not an
    outbound reading, so `internal_target` never sees it. Left alone these die
    with the Canvas account - and they are signed URLs, so they can expire well
    before that. The course id is taken from the URL itself rather than the
    calling course, because that is what the link actually points at.
    """
    m = CANVAS_FILE_URL.match(url)
    if not m:
        return None
    cid, fid = m.group(1), m.group(2)
    courses = data.get("courses") or {}
    # A bare `/files/<id>` link names no course; Canvas file ids are global.
    pool = [courses.get(cid) or {}] if cid else courses.values()
    rec = next((r for c in pool if (r := (c.get("files") or {}).get(fid))), None)
    return _existing((rec or {}).get("note"))


def internalise(md: str, data: dict, course: str) -> str:
    """Point archived readings and course files at their notes, not the web.

    Clicking either in a page should stay in Obsidian; the note itself carries
    the original URL. Links with no archived note are left alone.
    """
    def swap(m):
        text, url = m.group(1), m.group(2)
        target = internal_target(data, url, course) or canvas_file_target(data, url)
        return f"[[{target}|{text}]]" if target else m.group(0)
    return re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)", swap, md)


# ---- notes and cache ------------------------------------------------------

def _cache_path(k: str):
    return CACHE_DIR / f"{k}.txt"


def _read_cache(k: str) -> str:
    p = _cache_path(k)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _write_cache(k: str, text: str) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _cache_path(k).write_text(text, encoding="utf-8")


def _pdf_from_bytes(body: bytes, k: str) -> tuple[str, str]:
    """An external link that turned out to be a PDF. Same extractor as Canvas files."""
    tmp = CACHE_DIR / f"{k}.pdf"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(body)
    text, status = extract_mod.extract(str(tmp))
    return text, status


def _source_course_module(src: str) -> tuple[str | None, str | None]:
    """(course, module) for a note path relative to the archive root.

    Any layout: `<Course>/<Module>/x.md`, `<Quarter>/<Course>/...`, or
    `<Quarter>/<Enrolled|Waitlisted>/<Course>/...`. This read `parts[0]` as the
    course and `parts[1]` as the module, which matched nothing once courses
    moved under quarter folders (2026-09-23): every reading note lost its
    "Linked from" line and fell back to `_Unfiled/` instead of its session.
    The module is only returned when the source sits inside one, not for the
    course's own top-level notes.
    """
    parts = tuple(src.split(os.sep))
    course = course_of(parts)
    if not course or course not in parts:
        return None, None
    i = parts.index(course)
    return course, (parts[i + 1] if len(parts) > i + 2 else None)


def _relink(root, swaps: list[tuple[str, str]]) -> int:
    """Point links at reading notes that just moved. Returns notes changed.

    A reading note moves when its session is found (or changes). The course
    notes link to it by path, so without this they kept the old path until the
    next pull regenerated them - and a pull runs before the links pass in a
    refresh, so it took two refreshes to converge. `.md` and every delimiter
    form are handled; only whole paths are replaced.
    """
    swaps = [(o, n) for o, n in swaps if o and n and o != n]
    if not swaps:
        return 0
    pats = [(re.compile(r"\[\[" + re.escape(o) + r"(\.md)?(?=[\]|#\\])"), n) for o, n in swaps]
    changed = 0
    for note in sorted(root.rglob("*.md")):
        parts = note.relative_to(root).parts
        if parts and parts[0] == "_files":
            continue
        try:
            text = note.read_text(encoding="utf-8")
        except OSError:
            continue
        new = text
        for pat, n in pats:
            if pat.pattern and n:
                new = pat.sub(lambda m, n=n: "[[" + n + (m.group(1) or ""), new)
        if new != text:
            note.write_text(new, encoding="utf-8")
            changed += 1
    return changed


def _write_reading_notes(root, rec: dict, k: str, status: str, text: str,
                         title: str) -> dict[str, str]:
    """One note per course that links here. The text is small; a copy per
    course keeps a course folder self-contained, which is the whole layout."""
    name = safe_filename(title or (rec["titles"][0] if rec["titles"] else None) or
                         (urllib.parse.urlsplit(rec["url"]).path.rsplit("/", 1)[-1] or host_of(rec["url"])),
                         maxlen=110)
    written: dict[str, str] = {}
    for course in sorted(rec["courses"]):
        # Put the reading in the session that links to it, so a module folder
        # holds the whole session: its page, its deck, and what it told you to
        # read. Falls back to _Unfiled when only the course anchor links it.
        mods = {m for c, m in (_source_course_module(src) for src in rec["sources"])
                if c == course and m}
        module = (sorted(mods)[0] if mods
                  else (rec.get("modules") or {}).get(course) or "_Unfiled")
        meta = {
            "type": "class-material",
            "canvas_source": "canvas",
            "canvas_kind": "reading",
            "canvas_course": f"[[{course}]]",
            "canvas_module": module,
            "canvas_external_url": rec["url"],
            "canvas_fetch_status": status,
            "canvas_fetched_at": state_mod.now(),
        }
        body = [f"# {title or name}", "", f"<{rec['url']}>", ""]
        linked = sorted(src for src in rec["sources"]
                        if _source_course_module(src)[0] == course)
        if linked:
            body.append("**Linked from:** " + ", ".join(
                f"[[{str(root.relative_to(VAULT))}/{s}".replace(".md", "") + f"|{os.path.basename(s)[:-3]}]]"
                for s in linked[:8]))
            body.append("")
        body.append(_status_line(status))
        if status in TEXT_STATUSES and text:
            body += ["", "## Extracted text", "", text]
        # The course folder exists by now (`run` skips any `locate` cannot
        # find), and only the pull knows its enrolment group, so find it
        # rather than rebuild it: a rebuilt path would miss Enrolled/.
        note = locate(root, course) / module / "Readings" / f"{name}.md"
        write_note(str(note), meta, "\n".join(body))
        written[course] = str(note.relative_to(VAULT)).removesuffix(".md")
    return written


def _status_line(status: str) -> str:
    return {
        "ok": "*Full text below, fetched from the source.*",
        "manual": "*Full text below. This one is behind a login, so it could not be "
                  "fetched; it was saved by hand from a signed-in browser and adopted "
                  "by `vt canvas adopt`.*",
        "media": "*Audio or video. There is no text to archive; the link is the whole record.*",
        "login-required": "> **Not archived — this needs your login.** The page returns a sign-in "
                          "wall to any unauthenticated client, so nothing was stored rather than "
                          "storing the wall. Listed by `vt canvas links --gated`.",
        "paywalled": "> **Not archived — paywalled.** A stub came back, not the article. It was "
                     "deliberately not stored as though it were the reading. Listed by "
                     "`vt canvas links --gated`.",
        "thin": "> **Nothing extractable.** The page loaded but yielded almost no text, which "
                "usually means it is an app shell or the content is behind a script. "
                "Listed by `vt canvas links --gated`.",
        "robots-disallowed": "> **Not fetched.** The site's `robots.txt` disallows it.",
    }.get(status, f"> **Not archived:** `{status}`.")


# ---- adopting hand-saved readings -----------------------------------------
#
# The gated worklist is 42 HBSP cases plus the rest of the 685: readings behind
# a login that no script can fetch. The owner opens each in a signed-in browser and
# saves the PDF. The job here is to work out which reading each saved file *is*.
#
# The previous design made that his problem: it keyed on the filename, used the
# filename as the note title, and filed everything under a `_manual` bucket. So
# every saved file had to be renamed by hand at the exact moment renaming is
# most tedious and least reversible - renaming an adopted file created a second
# note rather than fixing the first.
#
# None of that was necessary. State already holds the reading's real title (the
# instructor's own), its course, its module and the path of the note that is
# already sitting in the right session folder saying "login-required". The only
# unknown is which file answers which URL, and that is answerable from the PDF's
# own text.

# Words too common to identify anything, so they are excluded from a match.
_STOP = {"the", "and", "for", "with", "from", "that", "this", "how", "why", "what",
         "case", "note", "harvard", "business", "school", "publishing", "review",
         "part", "study", "series", "press", "university", "college", "abridged",
         "rev", "pdf", "eng", "copy", "final", "draft", "download"}

# A title token has to be this long to count. Three is low enough to keep words
# that carry a title ("pay", "risk", "cost") and is safe only because tokens are
# weighted by rarity below - without that, short common words dominate.
_MIN_TOKEN = 3

# How much of the PDF to look at. A case has its title on the first page; reading
# the whole document only adds words that dilute the score.
_MATCH_CHARS = 4000

# A match needs both: this share of the title's distinctive words present, and
# this much daylight over the runner-up. Two thresholds rather than one, because
# a course's HBSP cases are often on one theme and their titles overlap.
_MIN_SHARE = 0.55
_MIN_MARGIN = 0.20

# A title has to carry at least this much weight to be matchable at all. It is
# deliberately low: its job is to reject a title that filtering emptied, not to
# require long titles. Set to 1.5 once, which looked right against "Article from
# FT" and was wrong - it also rejected "Coursera", "Supercell" and "Interco",
# which are single words and perfectly identifying. Junk titles are handled by
# `_UNINFORMATIVE` above, which is the honest mechanism: the problem with
# "Article from FT" was never that it was short, it was that its words say
# nothing. Short and distinctive is not the same as short and useless.
_MIN_TITLE_MASS = 0.4

# Words common enough in written English that their presence in a document says
# nothing about which document it is. Rarity weighting cannot catch these: it
# measures rarity across the candidate titles, not across the language, so a
# word appearing in exactly one title scores as maximally distinctive even when
# it is "would". Matching Downloads against the whole gated list scored "2023
# Final Solutions.pdf" at 1.00 against a reading titled "What Would Have To Be
# True" on {would, true, have}. Every one of those is in almost any document.
_UNINFORMATIVE = {
    "would", "have", "true", "about", "there", "their", "these", "those", "than",
    "then", "when", "what", "which", "while", "will", "with", "your", "you",
    "they", "them", "this", "that", "here", "into", "over", "under", "more",
    "most", "some", "such", "only", "also", "just", "like", "make", "made",
    "take", "come", "give", "know", "think", "want", "need", "must", "should",
    "could", "been", "being", "does", "done", "each", "every", "other", "same",
    "well", "back", "down", "out", "up", "not", "but", "and", "for", "are",
    "was", "were", "can", "cant", "did", "may", "might", "one", "two", "new",
    "now", "how", "why", "who", "all", "any", "its", "our", "his", "her",
    "sign", "page", "link", "links", "click", "main", "home", "next",
    "article", "site", "external", "view", "read", "file", "document", "copy",
}


# A title saying "spreadsheet supplement" describes a workbook, not a case. HBSP
# ships several cases as a PDF plus a spreadsheet whose titles differ only by
# that phrase - "Ridgeway Power Holdings, Inc.: Cost of Capital" against
# "... Cost of Capital, Spreadsheet for Students" - so the words that separate
# them are exactly the words that never appear in either document's text.
# The file's own type settles it.
_SHEET_TITLE = re.compile(
    r"spreadsheet|supplement|exhibit|workbook|data\s*set|template", re.I)
_SHEET_EXT = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv"}

# How much a file/title kind disagreement costs. Enough to clear _MIN_MARGIN on
# its own, since without it these pairs tie at 1.00 and neither is adoptable.
_KIND_MISMATCH = 0.55


def _is_sheet_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in _SHEET_EXT


# Canvas appends this to the visible text of every external link, and the
# harvester takes the link text as the title. It rides into the title unspaced
# ("Bringing Craft Back into PlanningLinks to an external site.") where it
# contributes four tokens of pure noise that dilute the real ones, and in a few
# cases it is the entire title. Stripped before anything is scored or weighted.
_LINK_NOISE = re.compile(
    r"\s*Links?\s+to\s+an?\s+external\s+site\.?\s*", re.I)


def _clean_title(title: str) -> str:
    return _LINK_NOISE.sub(" ", title or "").strip().strip(".,;:-–— ")


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if len(w) >= _MIN_TOKEN and w not in _STOP and w not in _UNINFORMATIVE}


# A slug is only evidence if it is an opaque identifier. `hbsp.../tu/3a030325`
# is one; `.../stanford` and `.../verification` are ordinary words that happen
# to end a path, and matching a filename against those scored `Receipt
# Stanford.pdf` at 1.00 against a reading titled "Sign up", and `Verification of
# Membership Letter` against "The Stanford GSB LinkedIn". A slug must therefore
# look machine-generated: long enough, and mixing letters with digits.
_OPAQUE_SLUG = re.compile(r"^(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{6,}$")


def _slug_of(url: str) -> str:
    """The identifying tail of a URL, but only when it is an opaque id.

    Returns "" for a slug that is a readable word, because such a slug is
    evidence of nothing and scoring on it produces confident nonsense.
    """
    tail = urllib.parse.urlsplit(url or "").path.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail if _OPAQUE_SLUG.match(tail) else ""


# A profile page is a person, not a reading. Nobody saves one as a file, and
# every document about that person names them, so a profile scores as highly as
# the document itself. The Corvid case tied at 1.00 with "Ines Thornbury
# Vale", its protagonist's LinkedIn, and was refused. Such a link can still be
# listed as gated; it just never competes for a dropped file.
_PROFILE_URL = re.compile(
    r"^https?://([a-z]+\.)?linkedin\.com/(in|company|school)/"
    r"|^https?://(www\.)?(x|twitter)\.com/[^/?#]+/?(\?|#|$)", re.I)


def _is_profile(url: str) -> bool:
    return bool(_PROFILE_URL.match(url or ""))


def matchable(candidate: dict) -> bool:
    """Is this reading worth matching a loose file against at all?

    The gated list is every external link Canvas ever rendered, and most of it
    is not a reading. Titles in there include "Examples", "Chart", "website",
    "how to apply", "See Syllabus" and "Main Takeaways" - anchor text, harvested
    faithfully, describing nothing. Matching a folder of documents against those
    produces confident nonsense at a scale no threshold can fix: a first pass
    over ~/Downloads returned 39 "safe" attributions of which roughly four were
    right, including an enrolment certificate filed as "Sign up" and a
    verification letter as "The Stanford GSB LinkedIn".

    So the fix is upstream of scoring. A candidate qualifies if its title
    carries two or more informative words, **or** its URL ends in an opaque
    identifier - which is what a coursepack link looks like, and what lets
    genuinely one-word titles ("Coursera", "Supercell", "Interco") still play.
    """
    if _is_profile(candidate.get("url") or ""):
        return False
    return len(_tokens(_clean_title(candidate.get("title")))) >= 2 or bool(
        _slug_of(candidate.get("url") or ""))


def _weights(candidates: list[tuple[str, dict]]) -> dict[str, float]:
    """How much each title word is worth, as 1 / (titles containing it).

    A course's gated readings are all on that course's subject, so their titles
    share the subject's vocabulary. In a transit course "How to Pay for
    Urban Transit" reduces to {urban, transit, pay} and every other case on the list
    contains "urban" and "transit" too - matching on raw overlap would score them
    all identically and adopt nothing, or worse, adopt the wrong one.

    Weighting by rarity across the candidate set puts the discriminating word
    where it belongs: "kaiser" identifies one reading, "health" identifies none.
    """
    df: dict[str, int] = {}
    for _, c in candidates:
        for t in _tokens(_clean_title(c.get("title"))):
            df[t] = df.get(t, 0) + 1
    return {t: 1.0 / n for t, n in df.items()}


def _score(candidate: dict, filename: str, text: str,
           weights: dict[str, float]) -> tuple[float, str]:
    """How strongly this file looks like this reading. Returns (score, why).

    Three signals, in descending order of how much they can be trusted:

      1. The URL's slug appearing in the filename. Exact, and unambiguous when
         the site names its downloads after the product id.
      2. The title's distinctive words appearing in the document's own text,
         each weighted by how rare it is among this course's candidates. This is
         the signal that works no matter what the file is called, which is the
         entire point of the change.
      3. The same words appearing in the filename. Weakest, and only ever used
         to break a tie, because a filename is whatever a browser decided.
    """
    title_tokens = _tokens(_clean_title(candidate.get("title")))
    if not title_tokens:
        return 0.0, "no title to match on"

    slug = _slug_of(candidate.get("url") or "")
    if slug and slug in filename.lower():
        return 1.0, f"filename contains the URL slug {slug!r}"

    def mass(ts) -> float:
        return sum(weights.get(t, 1.0) for t in ts)

    total = mass(title_tokens)
    if not total:
        return 0.0, "no title to match on"
    if total < _MIN_TITLE_MASS:
        return 0.0, (f"title carries too little to identify anything "
                     f"({', '.join(sorted(title_tokens)) or 'nothing usable'})")
    in_text = title_tokens & _tokens(text[:_MATCH_CHARS])
    in_name = title_tokens & _tokens(filename)
    share = mass(in_text) / total
    if in_name:
        # Corroboration only: it can lift a match over the line but never carry
        # one on its own.
        share = min(1.0, share + 0.15 * mass(in_name) / total)
    hits = ", ".join(sorted(in_text, key=lambda t: -weights.get(t, 1.0))[:6]) or "nothing"
    why = f"{len(in_text)}/{len(title_tokens)} title words in the text ({hits})"

    wants_sheet = bool(_SHEET_TITLE.search(_clean_title(candidate.get("title"))))
    if wants_sheet != _is_sheet_file(filename):
        share *= _KIND_MISMATCH
        why += (f"; discounted, the title {'wants' if wants_sheet else 'does not want'} "
                f"a spreadsheet and this file is "
                f"{'one' if _is_sheet_file(filename) else 'not'}")
    return share, why


def _extract_text(path) -> tuple[str, str]:
    if not extract_mod.supported(str(path)):
        return "", "unsupported"
    # Always OCR here: a hand-saved file is one document he chose to save, and a
    # scan that cannot be read cannot be matched to anything.
    return extract_mod.extract(str(path), ocr=True)


def adopt_from(root, data: dict, source, *, dry_run: bool = False,
               verbose: bool = False) -> dict[str, Any]:
    """Match files in an arbitrary folder against the WHOLE gated worklist.

    `adopt` scopes candidates to the course whose `_manual/` a file was dropped
    in, which is what makes its matching reliable. This is for the case where
    that is not known yet: a browser saving 42 coursepack PDFs puts them all in
    ~/Downloads under names like `R1609D-PDF-ENG.pdf`, and which course each
    belongs to is exactly the question.

    Attribution has to come from the document, not from the order things were
    downloaded. Measured the hard way: driving the browser through six links and
    assuming the Nth file was the Nth link filed the CarMax case under a Toyota
    one, because downloads complete out of order and some do not arrive at all.
    The matcher refused it, which is the only reason it was caught.

    Matching across every course rather than within one is a weaker position, so
    the thresholds do the work: rarity is computed across the whole worklist, and
    a file that does not clear both bars is left where it is and reported.
    """
    from .archive import FILES_ROOT, MANUAL as MANUAL_DIR

    cache: dict[str, dict] = data.setdefault("readings", {})
    found = records(root, data)
    # One entry per reading, not per course. A reading linked from two courses
    # would otherwise appear twice with identical titles, tie with itself at the
    # top, and be refused for having no margin over the runner-up - which is
    # itself. The file goes to the first course; `_write_reading_notes` writes a
    # note in every course that links it regardless.
    candidates = []
    for k, rec in found.items():
        if cache.get(k, {}).get("canvas_fetch_status") not in GATED_STATUSES:
            continue
        courses = sorted(rec.get("courses", set()))
        if not courses:
            continue
        cand = {"url": cache[k].get("url", ""),
                "title": cache[k].get("title", ""), "course": courses[0]}
        if matchable(cand):
            candidates.append((k, cand))
    weights = _weights(candidates)
    placed, skipped = [], []
    for path in sorted(p for p in source.iterdir() if p.is_file()
                       and not p.name.startswith(".")):
        text, status = _extract_text(path)
        if status != "ok":
            skipped.append({"file": path.name, "reason": f"could not read it: {status}"})
            continue
        scored = sorted(((_score(c, path.name, text, weights), k, c)
                         for k, c in candidates), key=lambda t: -t[0][0])
        if not scored:
            skipped.append({"file": path.name, "reason": "no gated readings at all"})
            continue
        (best, why), k, cand = scored[0]
        runner = scored[1][0][0] if len(scored) > 1 else 0.0
        if best < _MIN_SHARE or (best - runner) < _MIN_MARGIN:
            skipped.append({"file": path.name,
                            "reason": (f"no clear match (best {best:.2f} "
                                       f"\u201c{cand['title']}\u201d, runner-up {runner:.2f}); "
                                       f"{why}")})
            continue
        dest = FILES_ROOT / cand["course"] / MANUAL_DIR / path.name
        placed.append({"file": path.name, "title": cand["title"],
                       "course": cand["course"], "score": best, "why": why,
                       "dest": str(dest)})
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
    return {"placed": placed, "skipped": skipped,
            "scanned": len(placed) + len(skipped)}


def adopt(root, data: dict, *, dry_run: bool = False, verbose: bool = False) -> dict[str, Any]:
    """Match every file dropped in a `_manual/` folder to the reading it answers.

    Identity is the reading's URL, never the filename, so the file can keep
    whatever name the browser gave it and renaming it later changes nothing.

    A match is never guessed. Anything that does not clear both thresholds is
    reported as unmatched or ambiguous with its scores, because filing a case
    under the wrong reading is worse than filing it nowhere - and an adoption
    that quietly picked the wrong one would look exactly like a clean run.
    """
    from .archive import FILES_ROOT, MANUAL as MANUAL_DIR

    cache: dict[str, dict] = data.setdefault("readings", {})
    found = records(root, data)
    already = {r.get("manual_file") for r in cache.values() if r.get("manual_file")}

    matched: list[dict] = []
    unmatched: list[dict] = []
    result = {"matched": matched, "unmatched": unmatched, "scanned": 0,
              "courses_without_dropzone": []}

    for cid, crec in sorted(data.get("courses", {}).items()):
        course = crec.get("folder")
        if not course:
            continue
        drop = FILES_ROOT / course / MANUAL_DIR
        if not drop.is_dir():
            continue

        # Only this course's outstanding gated readings are candidates. That is
        # what makes the matching reliable: a course has a handful of them, not
        # the whole archive's 685.
        candidates = [
            (k, {"url": cache[k].get("url", ""), "title": cache[k].get("title", "")})
            for k, rec in found.items()
            if course in rec.get("courses", set())
            and cache.get(k, {}).get("canvas_fetch_status") in GATED_STATUSES
            and not _is_profile(cache[k].get("url", ""))
        ]

        pending: list[tuple[Any, str]] = []
        for path in sorted(drop.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if str(path) in already:
                continue
            result["scanned"] += 1
            text, status = _extract_text(path)
            if status not in ("ok",):
                unmatched.append({"course": course, "file": path.name,
                                  "reason": f"could not read the file: {status}"})
                continue
            pending.append((path, text))

        # Repeat until a pass adopts nothing. A matched reading leaves the
        # candidate list, and that can be exactly what separates another file's
        # tie: "Deal Structure and Deal Terms" tied at 1.00 with "Risk and Reward
        # in Seed Lending", because a long lending note contains every word of
        # both titles, and was refused only because it sorted before the file
        # that was Risk and Reward. The outcome must not depend on filenames.
        # Every file in a pass is scored against the same candidates, so within
        # a pass order cannot matter either; claimed readings leave between
        # passes. Dry runs shrink the list too, or they report ties a real run
        # would never hit.
        refused: dict[str, dict] = {}
        progress = True
        while pending and progress:
            progress = False
            still: list[tuple[Any, str]] = []
            claimed: set[str] = set()
            weights = _weights(candidates)
            for path, text in pending:
                if not candidates:
                    # Keep an earlier pass's scores if there were any: "no clear
                    # match" says why, "nothing outstanding" only says when.
                    refused.setdefault(path.name, {
                        "course": course, "file": path.name,
                        "reason": "this course has no outstanding gated readings"})
                    still.append((path, text))
                    continue

                scored = sorted(((_score(c, path.name, text, weights), k, c)
                                 for k, c in candidates), key=lambda t: -t[0][0])
                (best, why), k, cand = scored[0]
                runner = scored[1][0][0] if len(scored) > 1 else 0.0
                # A reading another file claimed earlier in this pass is deferred
                # like a tie: retried once it has left the list, never filed twice.
                if k in claimed or best < _MIN_SHARE or (best - runner) < _MIN_MARGIN:
                    rival = (f" \u201c{scored[1][2]['title']}\u201d"
                             if len(scored) > 1 and best >= _MIN_SHARE
                             and best - runner < _MIN_MARGIN else "")
                    refused[path.name] = {
                        "course": course, "file": path.name,
                        "reason": (f"no clear match (best {best:.2f} \u201c{cand['title']}\u201d, "
                                   f"runner-up {runner:.2f}{rival}); {why}"),
                        "best_key": k}
                    still.append((path, text))
                    continue

                refused.pop(path.name, None)
                progress = True
                claimed.add(k)
                matched.append({"course": course, "file": path.name, "key": k,
                                "title": cand["title"], "url": cand["url"],
                                "score": best, "why": why, "chars": len(text)})
                if dry_run:
                    continue

                _record_adoption(root, cache, found, k, cand, path, text)
                already.add(str(path))
                if verbose:
                    print(f"    {course}: {path.name} -> {cand['title']}")
            pending = still
            candidates = [(ck, c) for ck, c in candidates if ck not in claimed]
        unmatched.extend(refused.values())

    return result


def _record_adoption(root, cache: dict, found: dict, k: str, cand: dict, path,
                     text: str, *, assigned: bool = False) -> None:
    """Store a hand-saved file's text as the reading's own. Shared by the scored
    path and `--as`, so the two can never record an adoption differently."""
    # The text joins the reading cache exactly where a fetched one would
    # live, so every downstream pass treats it identically from here on.
    _write_cache(k, text)
    rec = cache.setdefault(k, {"url": cand["url"], "title": cand["title"]})
    rec["canvas_fetch_status"] = MANUAL
    rec["canvas_fetched_at"] = state_mod.now()
    rec["chars"] = len(text)
    rec["manual_file"] = str(path)
    if assigned:
        # A person made this match, not the scorer. Kept so a later reader of
        # the state can tell the two apart.
        rec["assigned"] = True
    before = dict(rec.get("notes") or {})
    rec["notes"] = {**before, **_write_reading_notes(root, found[k], k, MANUAL, text,
                                                     cand["title"])}
    _relink(root, [(before.get(c), n) for c, n in rec["notes"].items()])


def _find_reading(cache: dict, url: str) -> tuple[str | None, str]:
    """The cache key for a reading URL, or (None, why).

    Exact first. Then, because a URL copied from a browser rarely carries the
    same query string Canvas rendered (`?usp=sharing` against `?usp=drive_link`),
    the same scheme, host and path - but only if exactly one reading has it.
    """
    k = key_of(url)
    if k in cache:
        return k, ""
    def bare(u: str) -> str:
        p = urllib.parse.urlsplit(normalise(u))
        return f"{p.scheme}://{p.netloc}{p.path}"
    want = bare(url)
    hits = [ck for ck, r in cache.items() if r.get("url") and bare(r["url"]) == want]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, (f"{len(hits)} readings share that address without its query string; "
                      "pass the URL exactly as the reading note's `canvas_external_url` has it")
    return None, "no reading in the archive has that URL"


def adopt_as(root, data: dict, url: str, file: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Assign one hand-saved file to one reading, by the person, not the scorer.

    For the readings no text can identify: Canvas took the title from the link's
    anchor text, so "use this link instead" is the only name Calloway's "Seven
    Quiet Pressures" has, and nothing in the article contains those words.
    `adopt` is right to refuse it. This is the explicit override, and it is
    strict about everything except the match itself: the file must sit in a
    course's `_manual/`, the reading must be gated and linked from that same
    course, and the text must actually come out.
    """
    from .archive import FILES_ROOT, MANUAL as MANUAL_DIR

    cache: dict[str, dict] = data.setdefault("readings", {})
    path = pathlib.Path(os.path.expanduser(file))
    if not path.is_absolute() and not path.exists():
        # A bare filename is the common case; look for it in every drop zone.
        hits = list(FILES_ROOT.glob(f"*/{MANUAL_DIR}/{path.name}"))
        if len(hits) > 1:
            return {"error": f"{path.name} is in {len(hits)} drop folders; pass the full path"}
        if hits:
            path = hits[0]
    path = path.resolve()
    if not path.is_file():
        return {"error": f"no such file: {file}"}
    if path.parent.name != MANUAL_DIR or path.parent.parent.parent != FILES_ROOT.resolve():
        return {"error": f"{path.name} is not in a course's {MANUAL_DIR}/ folder"}
    course = path.parent.parent.name

    # Resolved on both sides: scored adoption stores the path as listed, and a
    # symlinked vault would otherwise let one file answer two readings.
    if any(r.get("manual_file") and pathlib.Path(r["manual_file"]).resolve() == path
           for r in cache.values()):
        return {"error": f"{path.name} is already adopted; it cannot answer two readings"}

    k, why = _find_reading(cache, url)
    if k is None:
        return {"error": why}
    rec = cache[k]
    status = rec.get("canvas_fetch_status")
    if status not in GATED_STATUSES:
        return {"error": f"that reading is not waiting on a file (status: {status})"}
    found = records(root, data)
    if k not in found or course not in found[k].get("courses", set()):
        return {"error": f"that reading is not linked from {course}"}

    text, status = _extract_text(path)
    if status != "ok":
        return {"error": f"could not read {path.name}: {status}"}

    cand = {"url": rec.get("url", ""), "title": rec.get("title", "")}
    out = {"file": path.name, "course": course, "title": cand["title"],
           "url": cand["url"], "chars": len(text)}
    if not dry_run:
        _record_adoption(root, cache, found, k, cand, path, text, assigned=True)
    return out


def ensure_dropzones(root, data: dict, *, dry_run: bool = False) -> list[str]:
    """Create a `_manual/` folder for every course that still has gated readings.

    They were created by hand once and one was missed - OB-779, the course with
    the most HBSP cases. A precondition anybody has to remember is a
    precondition that gets forgotten, so the tool makes them itself.

    A course whose note folder is gone is skipped, the same guard `records`
    applies. The reading cache remembers course folder names that have since
    been renamed - MKTG 330 is in there under its old `<code> - <title>` name -
    and creating a drop folder for one would put an empty directory in the vault
    that nothing will ever read from.
    """
    from .archive import FILES_ROOT, MANUAL as MANUAL_DIR

    cache = data.get("readings", {})
    wanted: set[str] = set()
    for rec in cache.values():
        if rec.get("canvas_fetch_status") not in GATED_STATUSES:
            continue
        wanted.update((rec.get("notes") or {}).keys())
    made = []
    for course in sorted(wanted):
        if not locate(root, course).is_dir():
            continue
        drop = FILES_ROOT / course / MANUAL_DIR
        if not drop.is_dir():
            if not dry_run:
                drop.mkdir(parents=True, exist_ok=True)
            made.append(course)
    return made
