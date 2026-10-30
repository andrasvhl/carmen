#!/usr/bin/env python3
"""Run the whole Canvas archive against a stub Canvas, into a scratch vault.

There is no live-Canvas test and there should not be: it needs a token, a
network and a real enrolment. This exercises everything downstream of the HTTP
layer, which is where all four of the first-run defects actually were.

    ~/automation/.venv/bin/python -m tests.canvas_stub_run   # makes its own scratch vault

Asserts the two properties that matter: a first run writes the expected tree,
and a second run writes nothing.
"""
try: import tests._venv  # noqa: F401  re-runs under the venv
except ModuleNotFoundError: import _venv  # noqa: F401  run by path, not -m
import os
import pathlib
import tempfile

# Always a fresh scratch vault, set before anything imports runner.config.
# This used to rely on the caller exporting VAULT_PATH; run without it, it
# wrote a fake "Example Course" course into the real
# vault (found 2026-09-23, more than once), and later runs then reported
# "unchanged 8" against that leftover instead of testing anything.
os.environ["VAULT_PATH"] = tempfile.mkdtemp(prefix="canvas-stub-")

COURSE ={"id": 900104, "name": "Example Course",
          "course_code": "SP26-MKTG-330-01", "term": {"name": "Spring 2026"},
          "teachers": [{"display_name": "Pat Example"}, {"display_name": "Sam Sample"}]}

class Stub:
    host = "canvas.stanford.edu"
    requests = 0
    def get(self, path, **kw):
        if path == "courses/900104":
            return dict(COURSE, syllabus_body="<h2>Syllabus</h2><p>Grading: 40% participation.</p>")
        if path == "courses/900104/pages/overview":
            return {"title": "Overview", "updated_at": "2026-04-01T00:00:00Z",
                    "body": '<p>Read <a href="https://urldefense.com/v3/__https://hbr.org/x__;!!a$">this</a>.</p>'
                            '<table><tr><th>Week</th><th>Topic</th></tr><tr><td>1</td><td>Intro</td></tr></table>'
                            '<iframe src="https://stanford.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=9"></iframe>'}
        if path == "courses/900104/discussion_topics/9/view":
            return {"participants": [{"id": 5, "display_name": "Classmate A"}],
                    "view": [{"user_id": 5, "created_at": "2026-04-05", "message": "<p>My take.</p>"}]}
        raise AssertionError("unexpected get: " + path)
    def paginate(self, path, **kw): return iter(self.try_paginate(path, **kw)[0])
    def try_paginate(self, path, **kw):
        if path.endswith("/modules"):
            return [{"name": "Session 1: Intro", "items": [
                {"type": "Page", "title": "Overview", "page_url": "overview", "indent": 0},
                {"type": "File", "title": "Deck", "content_id": 111, "indent": 1},
                {"type": "ExternalUrl", "title": "Robin", "external_url":
                 "https://urldefense.com/v3/__https://example-newsletter.com/p/x__;!!a$"}]}], "ok"
        if path.endswith("/pages"): return [{"url": "overview", "title": "Overview"}], "ok"
        if path.endswith("/assignments"):
            return [{"id": 7, "name": "Kestrel Memo", "due_at": "2026-04-20", "points_possible": 20,
                     "description": "<p>Write <strong>two</strong> pages.</p>",
                     "rubric": [{"description": "Clarity", "points": 10, "long_description": "Is it clear?"}],
                     "submission": {"submitted_at": "2026-04-19", "score": 18,
                                    "body": "<p>My memo.</p>", "attachments": []},
                     "html_url": "https://canvas.stanford.edu/courses/900104/assignments/7"}], "ok"
        if path.endswith("/discussion_topics") and kw.get("only_announcements"):
            return [{"id": 8, "title": "Room change: C105", "posted_at": "2026-04-02T10:00:00Z",
                     "message": "<p>We move to C105.</p>", "author": {"display_name": "Pat"}}], "ok"
        if path.endswith("/discussion_topics"):
            return [{"id": 9, "title": "Week 1 thread", "posted_at": "2026-04-03T10:00:00Z",
                     "message": "<p>Discuss.</p>", "author": {"display_name": "Pat"}}], "ok"
        if path.endswith("/quizzes"): return [], "unavailable:403"
        if path.endswith("/files"):
            return [{"id": 111, "display_name": "Session1.pdf", "size": 14,
                     "updated_at": "2026-04-01T00:00:00Z", "folder_id": 2,
                     "url": "https://canvas.stanford.edu/files/111/download?verifier=x"}], "ok"
        if path.endswith("/folders"): return [{"id": 2, "full_name": "course files/Decks"}], "ok"
        raise AssertionError("unexpected paginate: " + path)
    def download(self, url, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(b"%PDF-1.4 fake\n")
        return 14, "deadbeef"

from tools.canvas import archive
data = {"version": 1, "courses": {}}
p = archive.Puller(Stub(), data); p.pull(COURSE)
print(f"run 1: wrote {p.written}, unchanged {p.unchanged}, downloaded {p.downloaded}, problems {p.problems}")
assert p.written == 8, f"expected 8 notes, got {p.written}"
assert p.downloaded == 1 and not p.problems

p2 = archive.Puller(Stub(), data); p2.pull(COURSE)
print(f"run 2: wrote {p2.written}, unchanged {p2.unchanged}, downloaded {p2.downloaded}")
assert p2.written == 0, "a second run must write nothing - incrementality is broken"
assert p2.downloaded == 0
print("extract:", archive.run_extract(data))
root = pathlib.Path(os.environ["VAULT_PATH"])
from tools.canvas.layout import course_dir
C = course_dir(root / "Areas/MBA/Course Archive", "Example Course", None)

# Module folders drop the word "Session" and the date: "Session 1: Intro"
# becomes "1 - Intro", so a sidebar shows what the session is about.
M = C / "1 - Intro"
assert M.is_dir(), f"module folder not renamed: {sorted(p.name for p in C.iterdir())}"

# The module's first page is its folder note (X/X.md), so the session's own
# write-up shows without expanding the folder.
page = (M / "1 - Intro.md").read_text()
assert "https://hbr.org/x" in page and "urldefense" not in page, "urldefense not decoded"
assert "panopto" in page.lower(), "the Panopto iframe was dropped"
assert (M / "Materials/Session1.pdf.md").exists(), "file note not under its module"

# Items Canvas files under no module go to _Unfiled, not into a session.
assert (C / "_Unfiled/Assignments/Kestrel Memo.md").exists(), "module-less assignment misplaced"
assert (C / "_Unfiled/Announcements").is_dir(), "announcements should be unfiled"

mods = (C / "Modules.md").read_text()
# `.md` is kept because the name still has an extension. Stripping it sent
# Obsidian looking for a PDF in the notes tree, and clicking the link created an
# empty `Session1.pdf 1.md` beside the real note. This assertion still expected
# the old, broken form after that fix landed, and nobody ran it.
assert "1 - Intro/Materials/Session1.pdf.md|Deck" in mods, "file link in the spine is wrong"
anchor = (C / "Example Course.md").read_text()
assert "venue: GSB" in anchor, "GSB course not recognised"
print("\n--- tree ---")
for f in sorted(root.rglob("*")):
    if f.is_file(): print("  ", f.relative_to(root))
print("\nall assertions passed")


# --- article tidying, offline ------------------------------------------------
# These guard the trimmer in both directions: it must remove newsletter and
# footer copy, and it must never touch an article that has none.
from tools.canvas.links import tidy_article

PROSE = ("\n".join(
    f"This is paragraph {i} of a real article about field research, long "
    f"enough that no line-length heuristic could mistake it for a promotional "
    f"box, and it discusses funnels, retention and channel selection at length."
    for i in range(20)))

out, cut = tidy_article(PROSE)
assert cut == 0 and out == PROSE.strip(), "clean prose must not be trimmed"

noisy = ("Hey, Robin here! Welcome to this month's free edition of my newsletter.\n"
         + PROSE +
         "\nIf you're finding this newsletter valuable, consider sharing it with friends."
         "\nSincerely,\nRobin")
out, cut = tidy_article(noisy)
assert cut > 0, "newsletter wrapper was not trimmed"
assert "Robin" not in out and "free edition" not in out, "sign-off survived"
assert PROSE.splitlines()[0] in out, "the trimmer ate real prose"

# A page that is nothing but promo must come back whole rather than as a
# fragment: the patterns were wrong about what the page is, not the page.
promo_only = "Subscribe now.\nJoin our newsletter.\nFollow us on X.\nPrivacy policy."
out, cut = tidy_article(promo_only)
assert cut == 0 and out == promo_only, "an all-promo page must not be gutted"

# A body paragraph that merely mentions subscriptions is not a promo block.
mid = PROSE + "\nOur subscribe button converted at 4%, which surprised us.\n" + PROSE
out, cut = tidy_article(mid)
assert "converted at 4%" in out, "a mid-article mention was treated as boilerplate"

print("article-tidying assertions passed")


# --- void elements must not swallow the document --------------------------
# Canvas injects a stylesheet <link> at the top of every page body. `link` is
# void, so `</link>` never arrives; treating it as a skip region discarded
# every page in the first real pull down to its title.
from tools.canvas.render import html_to_markdown as _h2m

_page = ('<link rel="stylesheet" href="https://x.test/gsb.css"><meta charset="utf-8">'
         '<h2>Required Preparation</h2>'
         '<p>Read <a href="https://example-review.com/grow">Grow</a>.</p>'
         '<ul><li><a href="https://ahrefs.com/blog/aarrr">AARRR</a></li></ul>')
_md, _ = _h2m(_page)
assert "Required Preparation" in _md, "a leading <link> swallowed the page"
assert "example-review.com" in _md and "ahrefs.com" in _md, "external links lost"

# Real skip containers still work: script/style content stays out.
_md2, _ = _h2m("<p>Keep this.</p><script>var secret=1;</script><style>p{}</style><p>And this.</p>")
assert "Keep this." in _md2 and "And this." in _md2
assert "secret" not in _md2 and "p{}" not in _md2

print("html-converter assertions passed")


# --- prune removes orphans, and only orphans --------------------------------
from tools.canvas import archive as _arch

_base = C
_files = root / "Areas/MBA/Course Archive/_files/Example Course"
(_base / "1 - Intro/Pages").mkdir(parents=True, exist_ok=True)
(_base / "1 - Intro/Pages/Deleted From Canvas.md").write_text("orphan", encoding="utf-8")
(_base / "Session 1 - Intro/Pages").mkdir(parents=True, exist_ok=True)
(_base / "Session 1 - Intro/Pages/Old Layout.md").write_text("orphan", encoding="utf-8")
(_base / "1 - Intro/Readings").mkdir(parents=True, exist_ok=True)
(_base / "1 - Intro/Readings/Some Reading.md").write_text("owned by the links pass", encoding="utf-8")
(_files / "_manual").mkdir(parents=True, exist_ok=True)
(_files / "_manual/Saved By Hand.pdf").write_bytes(b"%PDF-1.4 hand-saved\n")

# Every file the pull wrote must be registered, or prune deletes it. Two
# separate misses today (_sync-log.md, and the locked-file error paths) were
# each a live file removed as an orphan.
_live = {str(f) for f in _base.rglob("*.md")
         if "Readings" not in f.relative_to(_base).parts
         and "Session 1 - Intro" not in f.relative_to(_base).parts
         and "Deleted From Canvas" not in f.name}
_missing = _live - p.seen
assert not _missing, f"pull wrote files it did not register: {sorted(_missing)}"

# An orphan is a path the tool RECORDED and no longer writes - a Canvas item
# that was deleted, or a note left behind by a layout change. Writing a file to
# disk with no state entry is not an orphan, it is somebody else's file, and
# since the whitelist inversion prune cannot touch one. So `known` has to carry
# these two, the way a real run's snapshot would.
_known = set(p.known) | {str(_base / "1 - Intro/Pages/Deleted From Canvas.md"),
                         str(_base / "Session 1 - Intro/Pages/Old Layout.md")}

# Files the tool never wrote, dropped in the middle of its own tree. Under the
# old blacklist every one of these was deleted unless its folder name happened
# to be on a hardcoded list; the point of the inversion is that none of them is
# reachable, whatever they are called and wherever they sit.
(_base / "1 - Intro/Mine").mkdir(parents=True, exist_ok=True)
(_base / "1 - Intro/Mine/my cheat sheet.pdf").write_bytes(b"irreplaceable")
(_base / "1 - Intro/loose note.md").write_text("mine", encoding="utf-8")
(_base / "_mine").mkdir(parents=True, exist_ok=True)
(_base / "_mine/group work.xlsx").write_bytes(b"also mine")

_gone = _arch.prune(_base, _files, p.seen, _known)
assert any("Deleted From Canvas" in g for g in _gone), "orphan not pruned"
assert any("Old Layout" in g for g in _gone), "stale layout not pruned"
assert (_base / "1 - Intro/Readings/Some Reading.md").exists(), "pruned a reading"
assert (_files / "_manual/Saved By Hand.pdf").exists(), "pruned a hand-saved file"
assert (_base / "1 - Intro/1 - Intro.md").exists(), "pruned a live note"
assert (_files / "1 - Intro/Session1.pdf").exists(), "pruned a live binary"
assert not (_base / "Session 1 - Intro").exists(), "empty stale folder left behind"

# The guarantee the inversion exists for.
assert (_base / "1 - Intro/Mine/my cheat sheet.pdf").exists(), "pruned his own file"
assert (_base / "1 - Intro/loose note.md").exists(), "pruned a loose note of his"
assert (_base / "_mine/group work.xlsx").exists(), "pruned his course-level folder"
assert (_base / "1 - Intro/Mine").is_dir(), "removed a folder of his"

# And it must not reach outside the course, whatever state claims.
_outside = root / "Areas/MBA/elsewhere.md"
_outside.parent.mkdir(parents=True, exist_ok=True)
_outside.write_text("not this course", encoding="utf-8")
_arch.prune(_base, _files, set(), {str(_outside)})
assert _outside.exists(), "prune escaped the course it was given"

# The readings prune follows the same rule: only its own notes. A note of his
# in a Readings/ folder used to be deleted by any `links --prune`.
from tools.canvas import links as _links
_R = C / "1 - Intro" / "Readings"
_R.mkdir(parents=True, exist_ok=True)
(_R / "Stale Label.md").write_text("---\ncanvas_kind: reading\n---\n\n# Stale\n", encoding="utf-8")
(_R / "My thoughts on the reading.md").write_text("Mine, no frontmatter.\n", encoding="utf-8")
(_R / "My tagged note.md").write_text("---\ntype: note\n---\n\nMine too.\n", encoding="utf-8")
# Since 2026-09-27 the prune only reaches courses the run wrote a reading for,
# so a dropped course keeps its notes. An empty keep set touches no course.
assert not _links.prune_readings(root / "Areas/MBA/Course Archive", set(), dry_run=True), \
    "readings prune reached a course the run did not touch"
_rg = _links.prune_readings(root / "Areas/MBA/Course Archive", {str(_R / "Some Reading.md")})
assert any("Stale Label" in g for g in _rg), "stale reading note not pruned"
assert (_R / "My thoughts on the reading.md").exists(), "readings prune deleted his note"
assert (_R / "My tagged note.md").exists(), "readings prune deleted his frontmatter note"

print("prune assertions passed")


# --- a pull must not throw away extracted text for an unchanged file --------
# Every pull used to reset every file note to "not extracted yet", and extract
# wrote the same text back: two rewrites per note per refresh.
import re as _re
_fnote = M / "Materials/Session1.pdf.md"
_frec = data["courses"]["900104"]["files"]["111"]
_frec["extracted"] = "ok"
_txt = _fnote.read_text(encoding="utf-8")
_txt = _re.sub(r"canvas_extract_status: \S+", "canvas_extract_status: ok", _txt)
_txt = _txt.rstrip() + "\n\n## Extracted text\n\nSlide one says hello.\n"
_fnote.write_text(_txt, encoding="utf-8")

p3 = archive.Puller(Stub(), data); p3.pull(COURSE)
_after = _fnote.read_text(encoding="utf-8")
assert "Slide one says hello." in _after, "pull discarded extracted text of an unchanged file"
assert "canvas_extract_status: ok" in _after, "pull reset an extracted note to pending"

# A changed file (new updated_at) must still get a fresh placeholder.
_orig = Stub.try_paginate
def _changed(self, path, **kw):
    out = _orig(self, path, **kw)
    if path.endswith("/files"):
        return [dict(out[0][0], updated_at="2026-05-01T00:00:00Z")], "ok"
    return out
Stub.try_paginate = _changed
p4 = archive.Puller(Stub(), data); p4.pull(COURSE)
Stub.try_paginate = _orig
assert "canvas_extract_status: pending" in _fnote.read_text(encoding="utf-8"), \
    "a changed file kept its stale extracted text"
print("extract-preservation assertions passed")
