"""Binary -> text, so the archive can actually answer a question.

A PDF sitting in the vault is storage. Its text sitting next to it is a
corpus. This is also what closes the sync gap: Obsidian Sync carries
`image, audio, pdf, video` and nothing else, so a .pptx or .xlsx exists on one
machine forever - but its extracted text is markdown, and markdown syncs
everywhere.

Every parser is imported lazily and reported as missing rather than crashing,
because a run that archives 900 PDFs should not die on the one .xlsx whose
library is absent.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile

from runner import config

# Raw pypdf output, before any post-processing, keyed on the file's sha256.
#
# Parsing a thousand PDFs takes minutes; re-deriving text from a cached parse
# takes seconds. Every fix to `_rejoin_words`, `_unwrap_lines` or `scrub` used
# to cost a full re-parse of the whole archive - three times on 2026-09-01
# alone - which made "does this read better?" an expensive question and so a
# question that did not get asked.
#
# sha256 keys it, so a stale entry is structurally impossible: different bytes
# mean a different key. That is why this cache needs no age guard, unlike the
# Canvas API cache, where the upstream content genuinely changes underneath a
# fixed URL.
PDF_CACHE_DIR = config.STATE_DIR / "canvas-pdf-text"

# Extensions we can turn into text, mapped to the pip package that does it.
PARSERS = {
    ".pdf": "pypdf",
    ".pptx": "python-pptx",
    ".docx": "python-docx",
    ".xlsx": "openpyxl",
    ".xlsm": "openpyxl",
}

# Read straight through, no parser needed.
PLAIN = {".txt", ".md", ".csv", ".tsv", ".r", ".py", ".sql", ".json", ".do",
         ".m", ".js", ".ts", ".html", ".xml", ".yaml", ".yml", ".rmd"}

# A WebVTT caption file is the searchable half of a recording, and the recording
# itself is not archivable. Stored raw it is unreadable - two thirds of the bytes
# are cue numbers and timestamps - so it gets folded back into prose.
# The whole cue line, not just its head: matching up to `-->` leaves the end
# timestamp behind, which then reads as a speaker turn called "00".
VTT_CUE = re.compile(r"^\s*\d+\s*$|^\s*[\d:.]+\s*-->.*$", re.M)
VTT_SPEAKER = re.compile(r"^([^:\n]{1,40}):\s*")

# Below this many characters per page, a PDF is almost certainly a scan. GSB
# cases are frequently scanned, and an empty note that claims to hold the text
# is worse than one that says it does not.
MIN_CHARS_PER_PAGE = 40

# OCR output for scans, keyed on sha256 like the parse cache and for the same
# reason: a 150-page scan takes minutes on the box, and the bytes decide the
# answer. Opt-in (`extract --ocr`) because it is slow, not because it is unsafe.
OCR_CACHE_DIR = config.STATE_DIR / "canvas-ocr-text"

# 300 dpi is what tesseract is tuned for; lower loses small print in exhibits.
OCR_DPI = 300
# One page should never take this long. A page that does is skipped and marked,
# rather than stalling an unattended run on a pathological image.
OCR_PAGE_TIMEOUT = 180

# Says, in the note itself, that the text was machine-read. Status stays "ok"
# because every consumer treats "ok" as "the text is here", which it is - but a
# reader quoting a figure should know it came through OCR.
OCR_BANNER = ("*Text recovered by OCR from a scan. Expect recognition errors, "
              "especially in tables and figures.*")


class Missing(Exception):
    """The parser for this file type is not installed."""


def supported(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in PARSERS or ext in PLAIN or ext == ".vtt"


def extract(path: str, *, reparse: bool = False, ocr: bool = False) -> tuple[str, str]:
    """Returns (text, status).

    status is "ok" when real text came out, "no-text-layer" for a scan,
    "unsupported" for a type with no parser, or "missing:<package>".
    With `ocr`, a PDF that comes back "no-text-layer" is OCR'd instead, and
    stays "no-text-layer" only if OCR finds nothing either.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".vtt":
        text = _vtt(path)
        return text, "ok" if text.strip() else "no-text-layer"
    if ext in PLAIN:
        return _plain(path), "ok"
    if ext == ".pdf":
        text, status = _pdf(path, reparse=reparse)
        if status == "no-text-layer" and ocr:
            return _ocr_pdf(path, reparse=reparse)
        return text, status
    if ext == ".pptx":
        return _guard(_pptx, path, "python-pptx")
    if ext == ".docx":
        return _guard(_docx, path, "python-docx")
    if ext in (".xlsx", ".xlsm"):
        return _guard(_xlsx, path, "openpyxl")
    return "", "unsupported"


def _guard(fn, path: str, package: str) -> tuple[str, str]:
    try:
        text = fn(path)
    except Missing:
        return "", f"missing:{package}"
    except Exception as e:  # a corrupt file is a fact, not a crash
        return "", f"error:{type(e).__name__}"
    return text, "ok" if text.strip() else "no-text-layer"


def _plain(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        body = fh.read()
    lang = os.path.splitext(path)[1].lstrip(".").lower()
    return f"```{lang}\n{body.rstrip()}\n```"


def _vtt(path: str) -> str:
    """A WebVTT transcript as readable prose.

    Drops the cue numbers and timestamps, then merges consecutive lines from the
    same speaker into a paragraph. A 119 KB transcript of a 90-minute class is
    95 KB of actual speech once the scaffolding is gone, and unreadable before.
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    raw = raw.replace("WEBVTT", "", 1)
    lines = [ln.strip() for ln in VTT_CUE.sub("", raw).split("\n")]
    out: list[str] = []
    speaker = None
    for ln in lines:
        if not ln:
            continue
        m = VTT_SPEAKER.match(ln)
        who, said = (m.group(1).strip(), ln[m.end():].strip()) if m else (speaker, ln)
        if not said:
            continue
        if who != speaker or not out:
            out.append(f"**{who}:** {said}" if who else said)
            speaker = who
        else:
            out[-1] += " " + said
    return "\n\n".join(out)


def _rejoin_words(text: str) -> str:
    """Undo one-word-per-line PDF extraction.

    Some PDFs position every word as its own text operation. pypdf then emits
    a newline after each, and the space between words arrives as a line of its
    own, so a page comes out as:

        OIT-108\\n \\nTakeaway\\n \\n-\\n \\nSession\\n \\n1

    The text is all there and almost unreadable, and it is common: 46 notes
    and 24,801 word breaks across the first three courses.

    Splitting on newlines gives a token stream in which a lone whitespace
    token is a word gap and a run of them is a line break, which is enough to
    put the page back together.

    Guarded on the pathology actually being present, because a normally
    extracted page must come through untouched - verified against two known
    good extractions, 44 pages, zero changed.
    """
    lines = text.split("\n")
    blanks = sum(1 for ln in lines if ln != "" and ln.strip() == "")
    if len(lines) < 8 or blanks < len(lines) * 0.30:
        return text
    out: list[str] = []
    gap = 0
    for ln in lines:
        if ln.strip() == "":
            gap += 1
            continue
        if out:
            out.append(" " if gap <= 1 else ("\n" if gap == 2 else "\n\n"))
        out.append(ln.strip())
        gap = 0
    return "".join(out)


# A line that starts a block of its own and must never be folded into the one
# above it: bullets, numbered items, table rows, headings.
_BLOCK_START = re.compile(r"^([•●•\-\*]|\d+[.)]\s|\||#{1,6}\s)")


def _is_hard_wrapped(all_text: str) -> bool:
    """Is this document wrapped at a narrow column mid-sentence?

    Two signals, and **both** are needed - neither works alone, measured on
    four real documents (2026-09-01):

        Marketplaces (bad)   median line 26  lowercase-start 30%
        MKTG deck  (fine)    median line 27  lowercase-start  0%
        Lifecycle  (fine)    median line 21  lowercase-start 18%
        Kestrel case (fine)    median line 77  lowercase-start 41%

    Short lines alone would reflow both decks, which are short-lined because
    slides are. Lowercase starts alone would reflow the Kestrel case, which is
    ordinary prose wrapped wide enough to read. Only their conjunction picks
    out text broken mid-sentence at a narrow column.
    """
    import statistics
    lines = [l.strip() for l in all_text.split("\n") if l.strip()]
    if len(lines) < 40:
        return False
    median_len = statistics.median(len(l) for l in lines)
    lowercase_start = sum(1 for l in lines if l[:1].islower()) / len(lines)
    return median_len < 45 and lowercase_start >= 0.25


def _unwrap_lines(text: str) -> str:
    """Fold hard-wrapped lines back into paragraphs.

    Only ever called on a document `_is_hard_wrapped` accepted. Blank lines,
    bullets, numbered items, table rows and headings all still break a
    paragraph, so list structure survives.
    """
    out: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if out and out[-1]:
                out.append("")
            continue
        if out and out[-1] and not _BLOCK_START.match(line):
            prev = out[-1]
            # A trailing hyphen is a wrap break when the next piece is
            # lowercase ("approxi-" + "mately"), and a real compound when it
            # is capitalised ("Non-" + "Transactional"). Dropping it blindly
            # produced "NonTransactional".
            if prev.endswith("-") and not prev.endswith("--"):
                out[-1] = prev[:-1] + line if line[:1].islower() else prev + line
            else:
                out[-1] = prev + " " + line
        else:
            out.append(line)
    return "\n".join(out)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_pages(path: str, *, reparse: bool = False) -> list[str] | str:
    """Raw per-page text from pypdf, cached. Returns a status string on failure.

    The cache holds pypdf's output *before* post-processing, so changing how
    text is cleaned up never means parsing again. Hashing a file costs
    milliseconds against seconds to parse it, so the key is always computed.
    """
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return "missing:pypdf"

    key = None
    if not reparse:
        try:
            key = PDF_CACHE_DIR / f"{_sha256(path)}.json"
            if key.exists():
                cached = json.loads(key.read_text(encoding="utf-8"))
                if isinstance(cached, list):
                    return cached
        except (OSError, ValueError):
            key = None  # a broken cache must never fail the extraction

    try:
        reader = PdfReader(path)
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:
        return f"error:{type(e).__name__}"

    if key is not None:
        try:
            PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = key.with_suffix(".tmp")
            tmp.write_text(json.dumps(pages), encoding="utf-8")
            os.replace(tmp, key)  # atomic, so a killed run leaves no half file
        except OSError:
            pass  # caching is an optimisation, never a requirement
    return pages


def _pdf(path: str, *, reparse: bool = False) -> tuple[str, str]:
    pages_or_status = _pdf_pages(path, reparse=reparse)
    if isinstance(pages_or_status, str):
        return "", pages_or_status
    raw = [_rejoin_words(t) for t in pages_or_status]
    if not raw:
        return "", "no-text-layer"
    # Decided once for the whole document, never per page. A PDF has one
    # layout, and a per-page guard fired on 9 of 38 pages of a deck that was
    # already perfectly readable - the page-level signal is too noisy to act
    # on, the document-level one separates cleanly.
    if _is_hard_wrapped("\n".join(raw)):
        raw = [_unwrap_lines(t) for t in raw]
    pages = [f"### Page {i}\n\n{t}" if t else f"### Page {i}\n\n*(no text)*"
             for i, t in enumerate(raw, 1)]
    body = "\n\n".join(pages)
    chars = len(re.sub(r"\s+", "", body))
    if chars / max(1, len(pages)) < MIN_CHARS_PER_PAGE:
        return "", "no-text-layer"
    return body, "ok"


def _ocr_pdf(path: str, *, reparse: bool = False) -> tuple[str, str]:
    """OCR a scanned PDF page by page with pdftoppm + tesseract.

    Both are system binaries, not pip packages, so a machine without them
    reports `missing:tesseract` and moves on, like any other absent parser.
    """
    for tool, pkg in (("pdftoppm", "poppler-utils"), ("tesseract", "tesseract")):
        if not shutil.which(tool):
            return "", f"missing:{pkg}"

    key = None
    pages: list[str] | None = None
    try:
        key = OCR_CACHE_DIR / f"{_sha256(path)}.json"
        if not reparse and key.exists():
            cached = json.loads(key.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                pages = cached
    except (OSError, ValueError):
        key = None  # a broken cache must never fail the extraction

    if pages is None:
        try:
            from pypdf import PdfReader  # type: ignore
            count = len(PdfReader(path).pages)
        except ImportError:
            return "", "missing:pypdf"
        except Exception as e:
            return "", f"error:{type(e).__name__}"

        with tempfile.TemporaryDirectory(prefix="canvas-ocr-") as tmp:
            # Render once for the whole document. Per-page pdftoppm calls
            # re-parsed an 8.8 MB scan eighteen times, which was half the time.
            # PNGs go to disk, not memory, so a 150-page scan still fits.
            try:
                subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-gray", "-png",
                                path, os.path.join(tmp, "p")],
                               check=True, capture_output=True,
                               timeout=OCR_PAGE_TIMEOUT * max(1, count))
            except (subprocess.SubprocessError, OSError) as e:
                return "", f"error:{type(e).__name__}"
            images = sorted(os.listdir(tmp), key=lambda n: int(re.sub(r"\D", "", n) or 0))

            # tesseract's own threading fought itself on the box's two cores
            # (sys time ~ user time). One thread per process, one process per
            # core, is roughly twice as fast.
            env = dict(os.environ, OMP_THREAD_LIMIT="1")

            def read(name: str) -> str:
                try:
                    out = subprocess.run(["tesseract", os.path.join(tmp, name), "stdout",
                                          "-l", "eng", "--psm", "1"],
                                         check=True, capture_output=True, text=True,
                                         timeout=OCR_PAGE_TIMEOUT, env=env)
                    return out.stdout.strip()
                except (subprocess.SubprocessError, OSError):
                    return ""  # one bad page is not a failed document

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=max(1, min(2, os.cpu_count() or 1))) as pool:
                pages = list(pool.map(read, images))

        if key is not None:
            try:
                OCR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                tmpkey = key.with_suffix(".tmp")
                tmpkey.write_text(json.dumps(pages), encoding="utf-8")
                os.replace(tmpkey, key)
            except OSError:
                pass

    # Always unwrapped, unlike a text layer: tesseract marks every paragraph
    # with a blank line and hard-breaks the lines inside it, which is exactly
    # the shape `_unwrap_lines` repairs, hyphenated breaks included.
    raw = [_unwrap_lines(_rejoin_words(t)) for t in pages]
    body = "\n\n".join(f"### Page {i}\n\n{t}" if t else f"### Page {i}\n\n*(no text)*"
                        for i, t in enumerate(raw, 1))
    chars = len(re.sub(r"\s+", "", "".join(raw)))
    if chars / max(1, len(raw)) < MIN_CHARS_PER_PAGE:
        return "", "no-text-layer"
    return f"{OCR_BANNER}\n\n{body}", "ok"


def _pptx(path: str) -> str:
    try:
        from pptx import Presentation  # type: ignore
    except ImportError:
        raise Missing
    out = []
    for n, slide in enumerate(Presentation(path).slides, 1):
        parts = [sh.text_frame.text.strip()
                 for sh in slide.shapes
                 if getattr(sh, "has_text_frame", False) and sh.text_frame.text.strip()]
        # Speaker notes are where the instructor's actual argument often is,
        # and the predecessor export dropped them entirely.
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        block = [f"### Slide {n}"]
        if parts:
            block.append("\n\n".join(parts))
        if notes:
            block.append(f"**Speaker notes**\n\n{notes}")
        out.append("\n\n".join(block))
    return "\n\n".join(out)


def _docx(path: str) -> str:
    try:
        import docx  # type: ignore
    except ImportError:
        raise Missing
    d = docx.Document(path)
    out = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                out.append("| " + " | ".join(cells) + " |")
    return "\n\n".join(out)


def _xlsx(path: str) -> str:
    import warnings
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError:
        raise Missing
    out = []
    # openpyxl warns about unknown extensions on most real-world workbooks, and
    # it fires lazily during row iteration, not at load. It is noise on every
    # file and says nothing about whether extraction worked.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                if row and any(c is not None and str(c).strip() for c in row):
                    rows.append("| " + " | ".join("" if c is None else str(c) for c in row) + " |")
            if rows:
                out.append(f"### Sheet: {ws.title}\n\n" + "\n".join(rows[:2000]))
        wb.close()
    return "\n\n".join(out)
