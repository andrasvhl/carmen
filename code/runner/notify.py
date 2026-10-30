"""The ways a job reaches the owner: email, a task note in the vault, push, and Telegram.

Urgent goes to Pushover, which breaks through Focus. What can wait hours goes
to Telegram (send_telegram), silent in quiet hours.

Email interrupts. The note carries the work. Infrastructure problems send
both; the weekly all-green sends mail only. Push (Pushover) is for things
that lose value within minutes, e.g. a registration that is about to open:
by the time mail is read, the seats are gone.
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from . import config

# Anything a test process actually delivers says so. Tests stub these
# functions, but a missed stub reaches the real channel: on 2026-10-04
# tests/wa_tripwire stubbed send_mail only, and once send_urgent became
# push-first its simulated trip sent a real high-priority "WhatsApp BAN
# TRIPWIRE" push that read exactly like the real thing (the owner: "these tests
# should be marked as tests").
TEST_LABEL = "[TEST] "


def in_test() -> bool:
    """True when the running program is one of the repo's tests.

    `python3 -m tests.x` (the repo convention) is recognised by the main
    module's name, `python3 tests/x.py` by its path; AUTOMATION_TEST=1 forces it.
    """
    if os.environ.get("AUTOMATION_TEST") == "1":
        return True
    main = sys.modules.get("__main__")
    name = getattr(getattr(main, "__spec__", None), "name", "") or ""
    if name == "tests" or name.startswith("tests."):
        return True
    path = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    return f"{os.sep}tests{os.sep}" in path


def _label(text: str) -> str:
    return text if not in_test() or text.startswith(TEST_LABEL) else TEST_LABEL + text


class MailUnavailable(Exception):
    """Raised when mail is not configured. Callers degrade to notes only."""


def send_mail(subject: str, body: str) -> str:
    env = config.load_env()
    user = env.get("GMAIL_USER")
    # Google shows app passwords as four groups of four. Pasted with the
    # spaces they are 19 characters and Gmail rejects them.
    password = (env.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = env.get("MAIL_TO", user)

    if not user or not password:
        raise MailUnavailable(f"{config.ENV_FILE} missing GMAIL_USER or GMAIL_APP_PASSWORD")

    msg = EmailMessage()
    msg["Subject"] = _label(subject)
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return to


class PushUnavailable(Exception):
    """Raised when push is not configured or Pushover refuses. Callers fall back to mail."""


PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send_push(title: str, message: str, *, url: str | None = None,
              url_title: str | None = None, priority: int = 0,
              device: str | None = None, retry: int | None = None,
              expire: int | None = None) -> None:
    """Send a Pushover notification to the owner's phone.

    priority: -1 quiet, 0 normal, 1 high (bypasses quiet hours). 2 (emergency,
    repeats until acknowledged) needs explicit retry and expire (seconds) from
    the caller; without them it is refused.
    """
    if priority not in (-1, 0, 1, 2):
        raise ValueError("priority must be -1, 0, 1 or 2")
    # Emergency (2) repeats until acknowledged, and on iOS plays through mute and
    # Focus. Only allowed with explicit, short limits from the caller, so nothing
    # can page him for hours by accident. Cap raised from 600 s to 3600 s on
    # 2026-10-06 for routine_push's 21:30 slot (retry 300 s, expire 1 h).
    if priority == 2 and not (retry and expire and 30 <= retry and expire <= 3600):
        raise ValueError("priority 2 needs retry >= 30 and expire <= 3600 seconds")
    env = config.load_env()
    user, token = env.get("PUSHOVER_USER"), env.get("PUSHOVER_TOKEN")
    if not user or not token:
        raise PushUnavailable(f"{config.ENV_FILE} missing PUSHOVER_USER or PUSHOVER_TOKEN")

    fields = {"token": token, "user": user, "title": _label(title)[:250],
              "message": message[:1024], "priority": str(priority)}
    # One named Pushover device only (e.g. the dumb phone at night). None means
    # every device on the account, which is the default for everything else.
    if device:
        fields["device"] = device[:25]
    if priority == 2:
        fields["retry"], fields["expire"] = str(retry), str(expire)
    if url:
        fields["url"] = url[:512]
        if url_title:
            fields["url_title"] = url_title[:100]
    req = urllib.request.Request(PUSHOVER_URL, data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            reply = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise PushUnavailable(f"Pushover HTTP {exc.code}: {detail}")
    except (urllib.error.URLError, OSError) as exc:
        raise PushUnavailable(f"Pushover unreachable: {type(exc).__name__}: {exc}")
    # Pushover signals success with status 1; anything else is a refusal,
    # never a silent success.
    if reply.get("status") != 1:
        raise PushUnavailable(f"Pushover refused: {reply.get('errors') or reply}")


class TelegramUnavailable(Exception):
    """Raised when Telegram is not configured or the Bot API refuses. Callers fall back to mail."""


TELEGRAM_API = "https://api.telegram.org"


def send_telegram(text: str, *, thread_id: int | None = None,
                  buttons: list[list[tuple[str, str]]] | None = None,
                  silent: bool | None = None) -> int:
    """Message the owner's private chat with the vault bot. Returns the message id.

    For what can wait hours (user-perspective decision 4): Pushover keeps what
    must break through Focus. Plain text, no parse_mode, so a job's `<` or `&`
    can never make Telegram reject the whole message. `thread_id` posts into a
    topic. The bot token never appears in an exception: the URL carries it.

    `buttons` are rows of (label, callback_data). The bridge daemon receives
    the tap, because it is the one polling this bot; callback_data must be one
    of the prefixes it handles (tg_bridge/proactive.py), under 64 bytes.

    `silent=None` means "decide by the clock": inside quiet hours (read from
    working-conditions, runner/quiet_hours.py) the message is delivered with
    no sound or banner. Nothing is held back, so nothing is lost overnight.
    """
    env = config.load_env()
    token, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_OWNER_ID")
    if not token or not chat:
        raise TelegramUnavailable(f"{config.ENV_FILE} missing TELEGRAM_BOT_TOKEN or TELEGRAM_OWNER_ID")

    if silent is None:
        from . import quiet_hours
        silent = quiet_hours.is_quiet()
    # 4096 is counted in UTF-16 code units; 4000 code points stays clear of it
    # for anything short of a wall of emoji.
    fields = {"chat_id": chat, "text": _label(text)[:4000], "disable_web_page_preview": "true"}
    if thread_id:
        fields["message_thread_id"] = str(thread_id)
    if silent:
        fields["disable_notification"] = "true"
    if buttons:
        for row in buttons:
            for _, data in row:
                if len(data.encode("utf-8")) > 64:
                    raise ValueError(f"callback_data over 64 bytes: {data!r}")
        fields["reply_markup"] = json.dumps({"inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row]
            for row in buttons]})
    req = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/sendMessage",
                                 data=urllib.parse.urlencode(fields).encode())
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            reply = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("description", "")
        except (ValueError, AttributeError):
            detail = ""
        raise TelegramUnavailable(f"Telegram HTTP {exc.code}: {detail[:300]}")
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramUnavailable(
            f"Telegram unreachable: {type(exc).__name__}: {str(exc).replace(token, '<token>')}")
    if not reply.get("ok"):
        raise TelegramUnavailable(f"Telegram refused: {reply.get('description') or reply}")
    return int((reply.get("result") or {}).get("message_id") or 0)


def send_urgent(subject: str, body: str, *, push_message: str, priority: int = 0,
                url: str | None = None, url_title: str | None = None) -> str:
    """Push first, then mail as the record. Returns the mail recipient.

    For failures the owner must act on now. He reads push and does not read mail
    promptly, so mail alone buried the Claude Code expiry warning and the
    WhatsApp ban tripwire in an inbox nobody opened.

    A failed push is never silent: the mail subject says so, because a mail
    standing in for a push has lost the urgency the push was meant to carry.
    Raises MailUnavailable like send_mail, which callers already handle; if
    the push landed, that only costs the record.
    """
    try:
        send_push(subject.removeprefix("vault-host: "), push_message,
                  priority=priority, url=url, url_title=url_title)
    except PushUnavailable as exc:
        subject = f"[PUSH FAILED] {subject}"
        body = f"The push for this alert failed ({exc}), so this mail is the only alert.\n\n{body}"
    return send_mail(subject, body)


def push_or_mail(subject: str, body: str, *, push_message: str, priority: int = 0,
                 url: str | None = None, url_title: str | None = None) -> str:
    """Push only; mail only when the push fails. Returns "push" or the mail recipient.

    Since the maintenance brief (2026-10-03) a delivered push carries no mail
    record: the owner does not read those mails, and the record is the vault's
    alert log. The failure path is send_urgent's, unchanged: the mail subject
    says [PUSH FAILED], because that mail is now the only alert. Raises
    MailUnavailable when both channels are down.
    """
    try:
        send_push(subject.removeprefix("vault-host: "), push_message,
                  priority=priority, url=url, url_title=url_title)
        return "push"
    except PushUnavailable as exc:
        return send_mail(f"[PUSH FAILED] {subject}",
                         f"The push for this alert failed ({exc}), so this mail is the only "
                         f"alert.\n\n{body}")


def write_note(title: str, body: str, *, due: str | None = None,
               urgent: bool = True, project: str = "Productivity Stack Build") -> Path | None:
    """Create a task note in Inbox/. Returns None if one already exists.

    Two rules are enforced here rather than trusted to each job:

    1. Create only, never edit. An existing file is left completely alone.
    2. The caller must put something varying in the title (a date, a cycle)
       so the next occurrence gets its own note instead of being suppressed
       forever by the first one.

    Together these keep every job clear of the two-writer conflict gap in
    scheduled-execution.md: nothing here ever writes to a file the owner might
    have open on his phone.
    """
    config.INBOX.mkdir(parents=True, exist_ok=True)
    path = config.INBOX / f"{_safe_filename(title)}.md"
    if path.exists():
        return None

    now = datetime.now().astimezone()
    front = [
        "---",
        "type: action",
        f"captured: {now.strftime('%Y-%m-%dT%H:%M')}",
        "status: open",
        f"priority: {'4-high' if urgent else '3-normal'}",
        f"urgent: {'true' if urgent else 'false'}",
        f"quadrant: {'urgent-important' if urgent else 'important-not-urgent'}",
        "startable: cold",
    ]
    if due:
        front.append(f"due: {due}")
    if urgent:
        # SYSTEM.md: urgent means all three of urgent, 4-high and scheduled today.
        front.append(f"scheduled: {now.date()}")
    front += ["projects:", f'  - "[[{project}]]"', "---"]

    path.write_text("\n".join(front) + "\n" + body.strip() + "\n", encoding="utf-8")
    return path


def _safe_filename(title: str) -> str:
    """Obsidian rejects these in filenames; everything else is fair game."""
    for ch in '\\/:*?"<>|':
        title = title.replace(ch, "-")
    return title.strip()[:180]
