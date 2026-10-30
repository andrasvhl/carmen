"""Claude's markdown -> Telegram HTML, in chunks Telegram accepts.

HTML rather than MarkdownV2: its escaping rules (18 special characters,
context-dependent) are where bots break. HTML needs `& < >` escaped in text and
`"` in attributes, and supports b, i, s, code, pre, a, blockquote.

Order matters (research-3 item 10): chunk the MARKDOWN first, then render each
chunk. Telegram's 4096 limit counts the text after entities are parsed, in
UTF-16 code units, so a chunk measured in markdown is safe once rendered;
tables are the exception (padding grows them) and are re-checked.

A chunk never ends inside a code fence: the fence is closed at the cut and
reopened, with its language, at the top of the next chunk.
"""

import html
import re
from dataclasses import dataclass

CHUNK_LIMIT = 4000          # OpenClaw's Telegram value, safer than 4096
TELEGRAM_LIMIT = 4096
MAX_CHUNKS = 3              # past this an answer is a document, not messages
LEAD_LIMIT = 600

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+#.-]*)")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_HR = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_CODE = re.compile(r"(`+)(.+?)\1")
_SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")


def utf16_len(text: str) -> int:
    """Length as Telegram counts it. An emoji outside the BMP is two."""
    return len(text.encode("utf-16-le")) // 2


# --- chunking ---------------------------------------------------------------

def _fence_of(line: str) -> tuple[str, str] | None:
    m = _FENCE.match(line)
    return (m.group(1), m.group(2)) if m else None


def _closes(line: str, marker: str) -> bool:
    s = line.strip()
    return s.startswith(marker) and set(s) == {marker[0]}


def _split_long(line: str, limit: int) -> list[str]:
    """A single line longer than a chunk: whitespace first, then hard. Python
    strings index by code point, so a surrogate pair can never be split."""
    pieces = []
    while utf16_len(line) > limit:
        # Largest prefix that fits, in code points.
        lo, hi = 1, len(line)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if utf16_len(line[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        cut = line.rfind(" ", 0, lo)
        if cut <= lo // 2:
            cut = lo
        pieces.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    pieces.append(line)
    return pieces


def chunk_markdown(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split on paragraph breaks, then lines, then words; never inside a fence."""
    lines: list[str] = []
    for line in text.split("\n"):
        lines += _split_long(line, limit - 24)

    out: list[str] = []
    cur: list[str] = []
    cur_len = 0
    fence: tuple[str, str] | None = None     # (marker, opening line) while inside one
    blank_at = -1                            # last blank line in cur outside a fence

    def length(parts: list[str]) -> int:
        return utf16_len("\n".join(parts))

    for line in lines:
        if fence is None:
            opened = _fence_of(line)
            after = (opened[0], line) if opened else None
        else:
            after = None if _closes(line, fence[0]) else fence
        add = utf16_len(line) + (1 if cur else 0)
        room = utf16_len(after[0]) + 1 if after else 0
        if cur and cur_len + add + room > limit:
            if fence is None and blank_at > 0:
                out.append("\n".join(cur[:blank_at]))
                cur = cur[blank_at + 1:]
                if cur and length(cur) + 1 + utf16_len(line) + room > limit:
                    out.append("\n".join(cur))
                    cur = []
            elif fence is not None:
                out.append("\n".join(cur + [fence[0]]))
                cur = [fence[1]]
            else:
                out.append("\n".join(cur))
                cur = []
            cur_len = length(cur)
            blank_at = -1
            add = utf16_len(line) + (1 if cur else 0)
        cur.append(line)
        cur_len += add
        fence = after
        if fence is None and not line.strip():
            blank_at = len(cur) - 1
    if cur:
        out.append("\n".join(cur))
    return [c.strip("\n") for c in out if c.strip()]


# --- markdown -> HTML -------------------------------------------------------

def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _inline(text: str) -> str:
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = text.replace("\x00", "")
    text = _CODE.sub(lambda m: keep(f"<code>{_esc(m.group(2))}</code>"), text)

    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        if url.lower().startswith(_SAFE_SCHEMES):
            return keep(f'<a href="{html.escape(url, quote=True)}">{_esc(label)}</a>')
        return keep(_esc(f"{label} ({url})"))

    text = _LINK.sub(link, text)
    text = _esc(text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<i>\1</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _table(rows: list[str]) -> str:
    """Telegram has no tables. Aligned monospace is the readable substitute."""
    grid = [_cells(r) for r in rows if not _TABLE_SEP.match(r)]
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    cols = [max(len(_plain_inline(r[i])) for r in grid) for i in range(width)]
    lines = ["  ".join(_plain_inline(c).ljust(cols[i]) for i, c in enumerate(r)).rstrip()
             for r in grid]
    return f"<pre>{_esc(chr(10).join(lines))}</pre>"


def _is_table_start(lines: list[str], i: int) -> bool:
    return ("|" in lines[i] and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]) is not None
            and "-" in lines[i + 1])


def to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _fence_of(line)
        if fence:
            marker, lang = fence
            body = []
            i += 1
            while i < len(lines) and not _closes(lines[i], marker):
                body.append(lines[i])
                i += 1
            i += 1                                   # the closing fence, if any
            code = _esc("\n".join(body))
            lang = re.sub(r"[^\w+#.-]", "", lang)
            out.append(f'<pre><code class="language-{lang}">{code}</code></pre>' if lang
                       else f"<pre>{code}</pre>")
            continue
        if _is_table_start(lines, i):
            rows = []
            while i < len(lines) and "|" in lines[i]:
                rows.append(lines[i])
                i += 1
            out.append(_table(rows))
            continue
        quote = _QUOTE.match(line)
        if quote:
            body = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                body.append(_inline(_QUOTE.match(lines[i]).group(1)))
                i += 1
            out.append("<blockquote>" + "\n".join(body) + "</blockquote>")
            continue
        heading = _HEADING.match(line)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>" if heading.group(1) else "")
        elif _HR.match(line):
            out.append("──────────")
        elif _BULLET.match(line):
            m = _BULLET.match(line)
            out.append(f"{m.group(1)}• {_inline(m.group(2))}")
        elif _ORDERED.match(line):
            m = _ORDERED.match(line)
            out.append(f"{m.group(1)}{m.group(2)}. {_inline(m.group(3))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out).strip()


# --- the plain-text fallback -------------------------------------------------

def _plain_inline(text: str) -> str:
    text = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text)
    text = _CODE.sub(lambda m: m.group(2), text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    return text


def to_plain(md: str) -> str:
    """What is sent when Telegram rejects the HTML: the same words, no markup,
    and nothing that needs escaping because there is no parse_mode."""
    out = []
    in_fence = None
    for line in md.split("\n"):
        if in_fence is None:
            fence = _fence_of(line)
            if fence:
                in_fence = fence[0]
                continue
        elif _closes(line, in_fence):
            in_fence = None
            continue
        if in_fence is not None:
            out.append(line)
            continue
        heading = _HEADING.match(line)
        if heading:
            out.append(heading.group(1).upper() if len(heading.group(1)) < 60 else heading.group(1))
            continue
        bullet = _BULLET.match(line)
        if bullet:
            out.append(f"{bullet.group(1)}• {_plain_inline(bullet.group(2))}")
            continue
        if _TABLE_SEP.match(line) and "-" in line:
            continue
        out.append(_plain_inline(line))
    return "\n".join(out).strip()


def visible(html_text: str) -> str:
    """The text Telegram will count: tags out, entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


# --- the delivery plan ------------------------------------------------------

@dataclass
class Part:
    html: str
    plain: str


@dataclass
class Plan:
    parts: list[Part]
    document: bytes | None = None


def _render(chunk: str, limit: int) -> list[Part]:
    rendered = to_html(chunk)
    plain = to_plain(chunk)
    if (utf16_len(visible(rendered)) <= TELEGRAM_LIMIT and utf16_len(plain) <= TELEGRAM_LIMIT) \
            or limit < 500:
        return [Part(rendered, plain)]
    parts = []
    for smaller in chunk_markdown(chunk, limit // 2):
        parts += _render(smaller, limit // 2)
    return parts


def plan(md: str, *, limit: int = CHUNK_LIMIT, max_chunks: int = MAX_CHUNKS) -> Plan:
    """Messages, or a lead line plus the whole answer as a .md file."""
    md = (md or "").strip() or "(the turn produced no text)"
    chunks = chunk_markdown(md, limit)
    if len(chunks) > max_chunks:
        lead = next((p for p in md.split("\n\n") if p.strip() and not _fence_of(p)), "")
        lead = lead if utf16_len(lead) <= LEAD_LIMIT else lead[:LEAD_LIMIT].rstrip() + "…"
        note = "\n\n_Full answer attached._"
        return Plan([Part(to_html(lead + note), to_plain(lead + note))],
                    document=md.encode("utf-8"))
    parts: list[Part] = []
    for chunk in chunks:
        parts += _render(chunk, limit)
    return Plan(parts)
