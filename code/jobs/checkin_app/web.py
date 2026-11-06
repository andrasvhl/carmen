"""The server's plumbing for the routes in __init__.py: the two ways in (Telegram initData, the
password and a 30-day cookie, a lockout after five wrong tries), vault files as signed links
(obsidian:// links are dead in the app), what a chat may attach, and one note section as a small
page.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl


MAX_AUTH_AGE = 86400       # Telegram reissues initData every time the app opens

IMAGE_MAX_BYTES = 12 * 1024 * 1024  # the page shrinks big photos first; a screenshot is far below

IMAGES_MAX = 4                      # per message

IMAGE_DIR = Path("Attachments") / "Carmen"

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

# PDFs too (Nov 3, the owner: "Yes, add PDFs"). Claude reads them with Read; 12 MB is about 16 MB
# of base64, inside the Agent SDK's 32 MB stream buffer (tg_bridge/agent.py MAX_BUFFER_BYTES).
ATTACH_TYPES = {**IMAGE_TYPES, "application/pdf": "pdf"}

VOICE_MAX_BYTES = 20 * 1024 * 1024   # Groq's upload cap is 25 MB; the page stops recording at 5 min


def verify_init_data(init_data: str, token: str, owner: str, now: float | None = None) -> bool:
    """Telegram's Mini App check: HMAC of the sorted fields, keyed by the bot token."""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", "")
    if not got or not token or not owner:
        return False
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        return False
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > MAX_AUTH_AGE:
            return False
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return False
    return str(user.get("id")) == str(owner)


COOKIE = "checkin"

COOKIE_AGE = 30 * 86400

LOCK_AFTER, LOCK_FOR = 5, 600


def make_cookie(secret: str, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + COOKIE_AGE))
    return f"{exp}.{hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()}"


def verify_cookie(value: str, secret: str, now: float | None = None) -> bool:
    exp, _, sig = (value or "").partition(".")
    if not secret or not exp.isdigit() or not sig:
        return False
    want = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig) and int(exp) > (now or time.time())


# Vault files in the app (Nov 2): obsidian:// links are dead in the chat sheet and in Telegram's
# webview, so every one is rewritten on the way out into a signed https link to /file. Signed,
# not cookie-authed, because Telegram opens links in the phone's browser, where neither initData
# nor the cookie exists. The signature binds the path and an expiry; the stored text keeps the
# obsidian:// link, so it is re-signed fresh on every read.
FILE_LINK_AGE = 7 * 86400

OBSIDIAN_LINK = re.compile(r"obsidian://open\?vault=<VAULT_NAME>&(?:amp;)?file=([^)\s\"'<>\]]+)")


def _file_sig(secret: str, rel: str, exp: str) -> str:
    return hmac.new(secret.encode(), f"file|{rel}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def sign_file(secret: str, rel: str, now: float | None = None) -> str:
    from urllib.parse import quote
    # The expiry is rounded up to a whole day (Nov 3: images in the chat flashed, because each
    # poll signed a new URL and the browser loaded the picture again): the same link all day.
    exp = str((int((now or time.time()) + FILE_LINK_AGE) // 86400 + 1) * 86400)
    return f"file?p={quote(rel, safe='')}&e={exp}&s={_file_sig(secret, rel, exp)}"


def verify_file(secret: str, rel: str, exp: str, sig: str, now: float | None = None) -> bool:
    if not secret or not rel or not exp.isdigit() or not sig:
        return False
    return hmac.compare_digest(_file_sig(secret, rel, exp), sig) and int(exp) > (now or time.time())


def resolve_vault_file(rel: str, vault: Path) -> Path | None:
    """The vault file a link names, or None. Obsidian links may omit .md; nothing outside the
    vault, nothing hidden (.git, .obsidian, .trash)."""
    root = vault.resolve()
    if any(part.startswith(".") for part in Path(rel).parts):
        return None
    for cand in (root / rel, root / f"{rel}.md"):
        try:
            p = cand.resolve()
        except OSError:
            continue
        if p.is_file() and p.is_relative_to(root):
            return p
    return None


def attach_stem(name: str) -> str:
    """A PDF keeps its own name in the vault (Nov 3), so Claude and Obsidian see what it is: the
    stem only, no path, no characters a vault path or a link would trip on, at most 80."""
    stem = Path(str(name).replace("\\", "/")).name
    stem = stem[:-4] if stem.lower().endswith(".pdf") else stem
    stem = re.sub(r"[^\w .,()&+-]+", " ", stem).strip(" .")
    return re.sub(r"\s+", " ", stem)[:80].strip() or "document"


def chat_images(body: dict, vault: Path) -> list[str]:
    """The attached images a chat message names (Nov 2): only files the upload route wrote,
    under Attachments/Carmen/, still in the vault; at most IMAGES_MAX."""
    out = []
    for rel in body.get("images") if isinstance(body.get("images"), list) else []:
        rel = str(rel)
        if rel.startswith(IMAGE_DIR.as_posix() + "/") and ".." not in Path(rel).parts \
                and resolve_vault_file(rel, vault) and rel not in out:
            out.append(rel)
    return out[:IMAGES_MAX]


SIGNED_LINK = re.compile(r"file\?p=([^&\s)\"'<>]+)&e=\d+&s=[0-9a-f]+")


def unlink_vault_files(obj):
    """The reverse, for anything coming back to be written into a note: the note keeps obsidian://."""
    if isinstance(obj, str):
        return SIGNED_LINK.sub(lambda m: "obsidian://open?vault=<VAULT_NAME>&file=" + m.group(1), obj)
    if isinstance(obj, list):
        return [unlink_vault_files(x) for x in obj]
    if isinstance(obj, dict):
        return {k: unlink_vault_files(v) for k, v in obj.items()}
    return obj


def link_vault_files(obj, secret: str, now: float | None = None):
    """Every obsidian:// link in a JSON-able value, rewritten to a signed /file link."""
    from urllib.parse import unquote
    if isinstance(obj, str):
        if "obsidian://" not in obj:
            return obj
        return OBSIDIAN_LINK.sub(lambda m: sign_file(secret, unquote(m.group(1)), now), obj)
    if isinstance(obj, list):
        return [link_vault_files(x, secret, now) for x in obj]
    if isinstance(obj, dict):
        return {k: link_vault_files(v, secret, now) for k, v in obj.items()}
    return obj


class Lockout:
    """In-memory: a restart clears it, which is fine for one owner."""
    def __init__(self):
        self.fails: list[float] = []

    def locked(self, now: float | None = None) -> bool:
        now = now or time.time()
        self.fails = [t for t in self.fails if now - t < LOCK_FOR]
        return len(self.fails) >= LOCK_AFTER

    def fail(self, now: float | None = None) -> None:
        self.fails.append(now or time.time())


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Carmen</title>
<link rel="manifest" href="manifest.webmanifest"><link rel="apple-touch-icon" href="icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Carmen">
<style>body{font:15px -apple-system,system-ui,sans-serif;background:#f2f2f7;display:flex;
justify-content:center;padding:60px 16px}form{background:#fff;border-radius:12px;padding:20px;
width:100%;max-width:320px}input,button{width:100%;font:inherit;padding:10px;border-radius:8px;
border:1px solid #ccc;margin-top:10px;box-sizing:border-box}button{background:#2481cc;color:#fff;
border:0}p{color:#c33;font-size:13px}@media (prefers-color-scheme:dark){body{background:#000}
form{background:#1c1c1e;color:#fff}input{background:#2c2c2e;color:#fff;border-color:#3a3a3c}}</style>
</head><body><form method="post"><b>Carmen</b>{msg}
<input type="password" name="password" autocomplete="current-password" placeholder="Password" autofocus>
<button>Sign in</button></form></body></html>"""


def section_page(p: Path, section: str) -> str:
    """One section of a note as a small readable page (a step's workspace link, Nov 4)."""
    import html as H
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(rf"^(#+)\s+{re.escape(section)}(?:\s.*)?$", text, re.M)   # "(Claude, Nov 4)" after it is fine
    if not m:
        part, title = text, p.stem
    else:
        level = len(m.group(1))
        rest = text[m.end():]
        stop = re.search(rf"^#{{1,{level}}}\s", rest, re.M)
        part, title = rest[:stop.start()] if stop else rest, section
    rows = []
    for ln in part.strip().splitlines():
        s = H.escape(ln)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", s)
        s = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', s)
        if re.match(r"^\s*- \[[xX]\] ", ln):
            rows.append("<li class=d>" + re.sub(r"^\s*- \[[xX]\]\s*", "", s) + "</li>")
        elif re.match(r"^\s*- \[ \] ", ln):
            rows.append("<li class=o>" + re.sub(r"^\s*- \[ \]\s*", "", s) + "</li>")
        elif re.match(r"^\s*[-*] ", ln):
            rows.append("<li>" + re.sub(r"^\s*[-*]\s*", "", s) + "</li>")
        elif re.match(r"^#+\s", ln):
            rows.append("<h3>" + re.sub(r"^#+\s*", "", s) + "</h3>")
        elif s.strip():
            rows.append(f"<p>{s}</p>")
    body = "".join(rows)
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{H.escape(title)}</title><style>body{{font:16px/1.45 -apple-system,system-ui,sans-serif;"
            f"max-width:680px;margin:0 auto;padding:18px;color:#1c1c1e;background:#f6f5f2}}h1{{font-size:20px}}"
            f"h3{{font-size:16px;margin:16px 0 4px}}li{{margin:4px 0}}li.d{{color:#888;text-decoration:line-through}}"
            f"a{{color:#2f55d4}}@media(prefers-color-scheme:dark){{body{{background:#121214;color:#eee}}}}</style>"
            f"<h1>{H.escape(title)}</h1><div class=src>{H.escape(p.stem)}</div><ul>{body}</ul>")
