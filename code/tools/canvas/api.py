"""Canvas LMS REST client: token auth, real pagination, backoff, safe downloads.

Stdlib only, deliberately. The pull half has to keep working on a box with a
bare python3, so nothing here imports a third-party package.

Three things the browser-driven predecessor got wrong and this does not:

  1. **Pagination is the `Link:` header, not a guess.** The old skill said
     "paginate with ?page=2 if needed". A course with 140 files silently lost
     40 of them.
  2. **403 and 404 are normal.** Concluded courses have tabs turned off, and a
     whole-archive run that dies on the first disabled Quizzes tab archives
     nothing. Those raise `Unavailable`, which callers record and step over.
  3. **Authorization is stripped across a host change.** Canvas file URLs
     redirect to inst-fs/S3. urllib re-sends every header by default, which
     would hand the API token to a CDN.
"""

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

from . import cache as cache_mod

USER_AGENT = "vault-canvas-archive/1 (+personal archive)"

# Give up after this many attempts on a retryable status. Five attempts with
# the backoff below spans ~31s, which clears Canvas's per-minute throttle.
MAX_ATTEMPTS = 5
RETRYABLE = {429, 500, 502, 503, 504}


class CanvasError(Exception):
    """The request failed in a way the caller cannot paper over."""


class Unavailable(CanvasError):
    """This course, or this tab of it, is not readable. Expected, not fatal.

    Concluded courses routinely 403 on quizzes, discussions or files because
    the instructor disabled the tab. Record it and move on.
    """

    def __init__(self, path: str, status: int):
        self.path = path
        self.status = status
        super().__init__(f"{path} -> HTTP {status}")


class _StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Drop Authorization when a redirect leaves the Canvas host.

    Canvas hands out file URLs that redirect to inst-fs or S3. urllib copies
    the original headers onto the redirected request, so without this the API
    token is sent to whatever host Canvas points at.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


def _parse_link_header(value: str) -> dict[str, str]:
    """`<url>; rel="next", <url>; rel="last"` -> {"next": url, "last": url}."""
    links = {}
    for part in value.split(","):
        m = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^"\s;]+)"?', part)
        if m:
            links[m.group(2)] = m.group(1)
    return links


class Client:
    """One Canvas host, one token, one connection policy."""

    def __init__(self, host: str, token: str, *, verbose: bool = False,
                 cache: "cache_mod.Cache | None" = None):
        self.host = host.replace("https://", "").replace("http://", "").strip("/")
        self.base = f"https://{self.host}/api/v1"
        self.token = token
        self.verbose = verbose
        # Recording is the default for an ordinary pull, so the cache is a
        # by-product of work already being done. Replay mode makes no requests
        # at all, which is also why it needs no token.
        self.cache = cache
        self._opener = urllib.request.build_opener(
            _StripAuthOnRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )
        self.requests = 0

    # ---- transport --------------------------------------------------------

    def _open(self, url: str, *, auth: bool = True, timeout: int = 60):
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "application/json")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")

        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                self.requests += 1
                return self._opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403, 404):
                    raise Unavailable(url, e.code) from None
                if e.code not in RETRYABLE:
                    body = e.read()[:400].decode("utf-8", "replace")
                    raise CanvasError(f"{url} -> HTTP {e.code}: {body}") from None
                last = e
                delay = float(e.headers.get("Retry-After") or 0) or 2 ** attempt
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                delay = 2 ** attempt
            if attempt < MAX_ATTEMPTS - 1:
                if self.verbose:
                    print(f"  retry in {delay:.0f}s ({last})")
                time.sleep(delay)
        raise CanvasError(f"{url} failed after {MAX_ATTEMPTS} attempts: {last}")

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        if path.startswith("http"):
            return path
        url = f"{self.base}/{path.lstrip('/')}"
        if params:
            url += "?" + _encode(params)
        return url

    # ---- reads ------------------------------------------------------------

    def _fetch(self, url: str) -> tuple[str, str]:
        """(body, Link header) for one URL, through the cache if there is one.

        Every JSON read in the package goes through here, which is what makes
        `pull --from-cache` a complete replay rather than a partial one: there
        is no second path to the network.

        A `403` or `404` is recorded as faithfully as a body, because a
        concluded course really does have tabs turned off and a replay has to
        reproduce that. A `401` is not - see `cache.CACHEABLE_STATUSES`. A
        `CanvasError` is not either: it means a non-retryable status or five
        exhausted attempts, both of which a later run might get past, and
        caching a transient failure would make it permanent.
        """
        if self.cache is not None and self.cache.replay:
            entry = self.cache.load(url)
            if entry is None:
                raise cache_mod.Miss(url)
            if entry["outcome"] == "unavailable":
                raise Unavailable(url, entry.get("status", 404))
            return entry.get("body", ""), entry.get("link", "")

        try:
            with self._open(url) as r:
                body = r.read().decode("utf-8")
                link = r.headers.get("Link", "")
        except Unavailable as e:
            if self.cache is not None:
                self.cache.store_unavailable(url, e.status)
            raise
        if self.cache is not None:
            self.cache.store_ok(url, body, link)
        return body, link

    def get(self, path: str, **params: Any) -> Any:
        """One object. Raises Unavailable on 401/403/404."""
        body, _ = self._fetch(self._url(path, params))
        return json.loads(body)

    def paginate(self, path: str, **params: Any) -> Iterator[dict]:
        """Every page, following `Link: rel="next"` until it is absent."""
        params.setdefault("per_page", 100)
        url = self._url(path, params)
        seen = set()
        while url:
            if url in seen:  # a malformed next link pointing at itself
                return
            seen.add(url)
            body, link = self._fetch(url)
            payload = json.loads(body)
            if isinstance(payload, dict):
                # A few endpoints wrap the list, e.g. discussion_topics/:id/view
                payload = next((v for v in payload.values() if isinstance(v, list)), [])
            yield from payload
            url = _parse_link_header(link).get("next", "")

    def try_paginate(self, path: str, **params: Any) -> tuple[list[dict], str]:
        """paginate() but turning Unavailable into a status string.

        Returns (items, status) where status is "ok" or "unavailable:403".
        This is the shape every section fetch wants: a disabled tab is a fact
        to record on the course, not an exception to unwind the run with.
        """
        try:
            return list(self.paginate(path, **params)), "ok"
        except Unavailable as e:
            return [], f"unavailable:{e.status}"
        except CanvasError as e:
            return [], f"error:{e}"

    # ---- downloads --------------------------------------------------------

    def download(self, url: str, dest: str) -> tuple[int, str]:
        """Stream `url` to `dest`. Returns (bytes written, sha256 hex).

        Written to a temp file and renamed, so an interrupted download never
        leaves a half file that the next run mistakes for a complete one.
        """
        if self.cache is not None and self.cache.replay:
            # Not a cached thing: the binary is already in the vault under
            # `_files/`, so a replay of an archived course never reaches here.
            # Arriving means Canvas is offering a file this machine has not
            # got, and a replay can neither supply it nor pretend it did.
            raise cache_mod.Offline(
                "not in the vault, and --from-cache cannot download it")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        digest = hashlib.sha256()
        total = 0
        # Canvas file URLs carry their own verifier; the bearer token is still
        # sent to the Canvas host and stripped at the first cross-host hop.
        with self._open(url, timeout=300) as r, open(tmp, "wb") as fh:
            while chunk := r.read(1 << 16):
                fh.write(chunk)
                digest.update(chunk)
                total += len(chunk)
        os.replace(tmp, dest)
        return total, digest.hexdigest()


def _encode(params: dict[str, Any]) -> str:
    """urlencode, but a list value becomes repeated `key[]=` pairs.

    Canvas uses `state[]=completed&state[]=available`, which urlencode's
    doseq cannot express because the brackets belong to the key.
    """
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, (list, tuple)):
            k = key if key.endswith("[]") else f"{key}[]"
            pairs.extend((k, str(v)) for v in value)
        elif isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        else:
            pairs.append((key, str(value)))
    return urllib.parse.urlencode(pairs)
