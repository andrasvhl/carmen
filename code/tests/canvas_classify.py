#!/usr/bin/env python3
"""Is what came back the article, or a teaser pretending to be one?

    ~/automation/.venv/bin/python -m tests.canvas_classify

Each case is the shape of a real false positive found 2026-09-23, plus the
articles that must NOT be caught by the same rules.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from tools.canvas.links import classify_response as C

fails = []
def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond: fails.append(name)

PARA = ("Spacing and retrieval practice improve long-term retention across ages and "
        "domains, and the effect holds in classrooms as well as laboratories. ")

# Nature: abstract, a purchase box, then hundreds of references. 47 KB, "ok".
nature = ("## Abstract\n\n" + PARA * 6 + "\n\n## Access options\n\nBuy this article\n\n"
          "- Purchase on SpringerLink\n\n## References\n\n"
          + "\n".join(f"{i}. Author, A. Title of paper {i}. Journal (2020)." for i in range(240)))
u = "https://www.nature.com/articles/s44159-022-00089-1"
check("abstract + purchase box + references is paywalled", C(nature, u, u) == "paywalled", C(nature, u, u))

# HBR: one paragraph, no marker at all - the rest is held back by script.
hbr = ("It is late and still. The station is empty. A courier hurries along the edge "
       "of the platform as the last train pulls in. " * 4)
u = "https://hbr.org/2014/10/example-metered-article"
check("a short result from a metered publisher is paywalled", C(hbr, u, u) == "paywalled")

# A news teaser that stops on an ellipsis, on a host not in the metered list.
teaser = ("Since the plan to cut bus fares began, an open question has been how far "
          "fares could fall. " * 5) + "how far she could bring the fare down …"
u = "https://example-news.com/story"
check("a short text ending in an ellipsis is paywalled", C(teaser, u, u) == "paywalled")

# Adweek: "UNLOCK FULL ACCESS" box under a short intro.
adweek = PARA * 5 + "\n\n## UNLOCK FULL ACCESS\n\nSubscribe and get full access to the news."
u = "https://www.adweek.com/media/x"
check("an unlock-full-access box is paywalled", C(adweek, u, u) == "paywalled")

# --- must stay ok -----------------------------------------------------------
long_article = PARA * 80
u = "https://hbr.org/2020/01/a-full-article"
check("a full-length article on a metered host stays ok", C(long_article, u, u) == "ok")

with_refs = PARA * 60 + "\n\n## References\n\n" + "1. Someone. Something. (2019).\n" * 50
u = "https://www.nature.com/articles/full-open-access"
check("a full article with references stays ok", C(with_refs, u, u) == "ok")

short_page = ("The Title IX Office supports students affected by sexual harassment and "
              "explains reporting options and resources available on campus. " * 4)
u = "https://share.stanford.edu/title-ix"
check("a short utility page on an ordinary host stays ok", C(short_page, u, u) == "ok")

mentions = PARA * 60 + " Readers who subscribe to this journal may already know the result."
u = "https://some-blog.org/long-post"
check("a long article that mentions a marker in passing stays ok", C(mentions, u, u) == "ok")

# --- a host that will not connect is tried once per run, not once per link ---
import urllib.error
from tools.canvas import links as L
_real, _calls = L.urllib.request.urlopen, []
def _timeout(req, timeout=None):
    _calls.append(req.full_url); raise urllib.error.URLError(TimeoutError("timed out"))
L.urllib.request.urlopen = _timeout
f = L.Fetcher(delay=0)
st = [f.fetch(f"https://dead.example/{i}")[3] for i in range(5)]
check("a dead host costs one attempt, not one per link", len(_calls) == 1, f"{len(_calls)} calls")
check("its links are recorded as unreachable", set(st) == {"error:host-unreachable"}, str(st))
def _404(req, timeout=None):
    raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
L.urllib.request.urlopen = _404
f2 = L.Fetcher(delay=0, respect_robots=False)
f2.fetch("https://alive.example/x")
check("a host that answers with an HTTP error is not written off", not f2._dead)
L.urllib.request.urlopen = _real

print()
print(f"{len(fails)} failure(s)" + (": " + ", ".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
