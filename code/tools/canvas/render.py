"""Canvas HTML -> vault markdown, verbatim.

No model in the loop. The predecessor skill wrote model-authored session
summaries, which read well and are the wrong substrate for answering a
specific question about a specific session: you cannot tell what was dropped.

Everything here is stdlib. `markdownify` is used when the venv has it because
it handles nested tables better, and the built-in converter is the fallback so
a pull never depends on it.
"""

import hashlib
import html as html_mod
import os
import re
from html.parser import HTMLParser
from typing import Any

# Obsidian Sync refuses these outright; jobs/inventory.py checks for them and
# a note carrying one exists on exactly one machine, silently.
ILLEGAL_IN_FILENAME = set(':*?"<>|/\\')

URLDEFENSE = re.compile(r"https://urldefense\.com/v\d/__(.+?)__;")


def decode_urldefense(url: str) -> str:
    """Unwrap a Proofpoint redirect back to the real destination.

    Stanford rewrites outbound links in Canvas content. Carried over from the
    predecessor skill, which is the one thing it learned that is worth keeping.
    """
    m = URLDEFENSE.search(url or "")
    if not m:
        return url
    # Deliberately NOT unquoted: the v3 payload is the literal URL, so
    # unquoting turns a legitimate %20 into a space and breaks the link.
    return m.group(1)


def safe_filename(name: str, maxlen: int = 120) -> str:
    """A Canvas title turned into something Obsidian Sync will carry.

    Colons are the common case - "Session 1: Intro" - and they are illegal.
    """
    name = html_mod.unescape(name or "").strip()
    name = name.replace(":", " -").replace("/", "-").replace("\\", "-")
    # `#` is legal in a filename and unusable in a wikilink: Obsidian reads it
    # as a heading anchor, so `[[.../Exercise #1 - Core Values]]` resolves to
    # the note "Exercise " with heading "1 - Core Values" and finds nothing.
    # 44 notes across 5 courses were unreachable this way (2026-09-01).
    name = name.replace("#", "")
    # `[ ] | ^` are link syntax too. `[INDIVIDUAL] DoorDash Quiz` could not be
    # linked (Obsidian reads `]` as the end of the link), and a malformed Canvas
    # label left a reading named `Planning and Review Workbook]]`. 36
    # notes in 5 courses were unreachable (2026-09-23). Brackets become
    # parentheses so "[IN CLASS]" still reads as a tag.
    name = name.replace("[", "(").replace("]", ")").replace("|", "-").replace("^", "")
    name = name.replace("()", "")
    # A bracket that never opened (or never closed) was noise, not a tag.
    while name.count(")") > name.count("(") and ")" in name:
        i = name.rfind(")"); name = name[:i] + name[i + 1:]
    while name.count("(") > name.count(")") and "(" in name:
        i = name.find("("); name = name[:i] + name[i + 1:]
    name = "".join(" " if c in ILLEGAL_IN_FILENAME else c for c in name)
    name = re.sub(r"\s+", " ", name).strip(" .-")
    if len(name) > maxlen:
        name = name[:maxlen].rstrip(" .-")
    return name or "Untitled"


# ---- HTML to markdown -----------------------------------------------------

_BLOCK = {"p", "div", "section", "article", "header", "footer",
          "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
# Containers whose CONTENT is dropped. All three have closing tags.
_SKIP = {"script", "style", "head"}

# Void elements: no closing tag, ever. They must never open a skip region -
# `link` and `meta` were in _SKIP, and since `</link>` never arrives the skip
# depth stayed at 1 for the rest of the document. Canvas injects a stylesheet
# <link> at the top of every page body, so every page in the first real pull
# came out as a title and nothing else.
_VOID = {"link", "meta", "base", "col", "embed", "hr", "input", "param",
         "source", "track", "wbr", "area"}


class _Converter(HTMLParser):
    """Small, predictable HTML -> markdown.

    Deliberately not a general converter. Canvas page bodies are authored in
    Canvas's own RTE, so the tag vocabulary is narrow: headings, paragraphs,
    lists, links, images, iframes, tables and the occasional pre block.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.embeds: list[tuple[str, str]] = []   # (kind, url)
        self.links: list[str] = []
        self._skip = 0
        self._pre = 0
        self._list: list[str] = []                # "ul" / "ol" nesting
        self._ol_index: list[int] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        # Tables are buffered cell-by-cell: a markdown row cannot be streamed,
        # because the separator line depends on the column count of a row that
        # has not been read yet.
        self._cell: list[str] | None = None
        self._row: list[str] = []
        self._row_index = 0

    # -- helpers
    def _emit(self, text: str) -> None:
        if self._href is not None:
            self._link_text.append(text)
        elif self._cell is not None:
            self._cell.append(text)
        else:
            self.out.append(text)

    def _newline(self, count: int = 1) -> None:
        if self._href is not None:
            return
        while self.out and self.out[-1] == "\n":
            self.out.pop()
        self.out.extend(["\n"] * (count + 1) if self.out else [])

    # -- parser
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _VOID and tag not in ("img", "br"):
            return
        if tag in _SKIP:
            self._skip += 1
            return
        if self._skip:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._newline()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in _BLOCK:
            self._newline()
            if tag == "blockquote":
                self._emit("> ")
            if tag == "pre":
                self._pre += 1
                self._emit("```\n")
        elif tag == "br":
            self._emit("  \n")
        elif tag in ("ul", "ol"):
            self._newline()
            self._list.append(tag)
            self._ol_index.append(0)
        elif tag == "li":
            self._newline(0)
            indent = "  " * max(0, len(self._list) - 1)
            if self._list and self._list[-1] == "ol":
                self._ol_index[-1] += 1
                self._emit(f"{indent}{self._ol_index[-1]}. ")
            else:
                self._emit(f"{indent}- ")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "a":
            self._href = decode_urldefense(a.get("href", ""))
            self._link_text = []
        elif tag == "img":
            src = decode_urldefense(a.get("src", ""))
            alt = a.get("alt", "image")
            if src:
                self._emit(f"![{alt}]({src})")
                self.embeds.append(("image", src))
        elif tag == "iframe":
            # Panopto lecture recordings arrive this way. The Canvas API cannot
            # hand over the media, so the URL is all there is - record it
            # visibly rather than dropping it.
            src = decode_urldefense(a.get("src", ""))
            if src:
                self._newline()
                self._emit(f"> **Embedded media (not downloadable via the Canvas API):** <{src}>")
                self._newline()
                self.embeds.append(("iframe", src))
        elif tag == "table":
            self._newline()
            self._row_index = 0
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag in _SKIP:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in _BLOCK:
            if tag == "pre":
                self._pre = max(0, self._pre - 1)
                self._emit("\n```")
            self._newline()
        elif tag in ("ul", "ol"):
            if self._list:
                self._list.pop()
                self._ol_index.pop()
            self._newline()
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag == "a":
            href, self._href = self._href, None
            text = "".join(self._link_text).strip()
            self._link_text = []
            if href:
                self.links.append(href)
                self._emit(f"[{text or href}]({href})")
            elif text:
                self._emit(text)
        elif tag in ("td", "th"):
            cell = re.sub(r"\s+", " ", "".join(self._cell or [])).strip()
            self._cell = None
            self._row.append(cell.replace("|", "\\|"))
        elif tag == "tr":
            if self._row:
                self.out.append("| " + " | ".join(self._row) + " |\n")
                if self._row_index == 0:
                    self.out.append("|" + "---|" * len(self._row) + "\n")
                self._row_index += 1
            self._row = []
        elif tag == "table":
            self._newline()

    def handle_data(self, data):
        if self._skip:
            return
        if self._pre:
            self._emit(data)
            return
        text = re.sub(r"[ \t\r\n]+", " ", data)
        if text.strip() or (self.out and not self.out[-1].endswith(" ")):
            self._emit(text)


def html_to_markdown(html: str) -> tuple[str, list[tuple[str, str]]]:
    """Returns (markdown, embeds). Empty input gives ("", [])."""
    if not html or not html.strip():
        return "", []
    # Deliberately not markdownify. It was tried and dropped 2026-09-01: it
    # renders faster and prettier, but it leaves urldefense links wrapped and
    # discards <iframe> entirely, so every Panopto recording vanished from the
    # archive with nothing to show it had. One converter that does what this
    # archive needs beats a better one that quietly loses content.
    c = _Converter()
    c.feed(html)
    c.close()
    md = _tidy("".join(c.out))

    # Tripwire for the class of bug that made every page a title and nothing
    # else: compare against a crude tag strip. A converter that loses most of
    # the document is not a formatting problem, it is data loss, and it looks
    # like a short page rather than an error.
    naive = _tidy(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)))
    if len(naive) > 400 and len(md) < len(naive) * 0.35:
        return naive, c.embeds
    return md, c.embeds


def _tidy(md: str) -> str:
    md = re.sub(r"[ \t]+\n", "\n", md)

    # Markdown will not close an emphasis run with whitespace inside the
    # closing marker, so Canvas's `<strong>Read: </strong>` became a literal
    # `**Read: **` with the asterisks visible and the bold bleeding onward.
    # Push the whitespace outside the markers.
    md = re.sub(r"\*\*([ \t]+)([^*\n]+?)\*\*", r"\1**\2**", md)
    md = re.sub(r"\*\*([^*\n]+?)([ \t]+)\*\*", r"**\1**\2", md)
    md = re.sub(r"(?<!\*)\*([ \t]+)([^*\n]+?)\*(?!\*)", r"\1*\2*", md)
    md = re.sub(r"(?<!\*)\*([^*\n]+?)([ \t]+)\*(?!\*)", r"*\1*\2", md)
    md = md.replace("****", "")

    md = re.sub(r"[ \t]{2,}", " ", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ---- notes ----------------------------------------------------------------

def yaml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return '""'
    s = str(v)
    if s == "":
        return '""'
    # Quote anything YAML would reinterpret: wikilinks, colons, leading marks.
    if re.search(r'[:#\[\]{}&*!|>%@`"\']', s) or s[0] in "-?," or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    # An empty value carries no information and multiplies across thousands of
    # notes. Omit the key instead; absent and blank mean the same thing here.
    meta = {k: v for k, v in meta.items() if v not in ("", None, [])}
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines.extend(f"  - {yaml_value(i)}" for i in v)
        else:
            lines.append(f"{k}: {yaml_value(v)}")
    lines.append("---")
    return "\n".join(lines)


# C0 control characters carry no meaning in a note, and a single NUL makes the
# whole file "binary": grep skips it, so it silently drops out of any search
# over the vault. Two OIT-108 PDFs carried NULs straight out of a table cell in
# their text layer (2026-09-01). Form feed is common in PDF text and is
# redundant here, since pages are already delimited by "### Page N".
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def scrub(text: str) -> str:
    """Drop control characters a markdown note cannot carry. Keeps \\n and \\t."""
    return CONTROL_CHARS.sub("", text)


def note_text(meta: dict[str, Any], body: str) -> str:
    # Scrubbed here rather than in `write_note`, because `content_hash` also
    # goes through this. Sanitising in only one of them would make the stored
    # hash disagree with the bytes on disk, and every such note would look
    # changed on every run.
    return scrub(f"{frontmatter(meta)}\n\n{body.strip()}\n")


def content_hash(meta: dict[str, Any], body: str) -> str:
    """Identity of a note, ignoring `last_run`.

    `last_run` changes on every run by definition, so hashing it would make
    every note look modified and defeat the whole point of incrementality.
    """
    stable = {k: v for k, v in meta.items() if k != "last_run"}
    return hashlib.sha256(note_text(stable, body).encode("utf-8")).hexdigest()


def write_note(path: str, meta: dict[str, Any], body: str) -> bool:
    """Write the note. Returns True if it actually changed on disk."""
    text = note_text(meta, body)
    if os.path.exists(path):
        try:
            if _strip_last_run(open(path, encoding="utf-8").read()) == _strip_last_run(text):
                return False
        except OSError:
            pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)
    return True


def _strip_last_run(text: str) -> str:
    return re.sub(r"^last_run:.*\n", "", text, flags=re.M)
