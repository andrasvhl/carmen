"""The Telegram bridge: Telegram long poll in, Claude Agent SDK turns out.

How it works and the decisions behind it live in the vault, at
`_system/telegram-bridge.md`; the original proposal, build plan and the five
openclaw-research notes are archived in `_system/telegram-bridge/Archive/proposals/`.
Code lives here, never there.

A package rather than one file like `jobs/wa_consumer.py`, because the parts are
independent enough to test alone:

    telegram.py   raw Bot API over httpx, the one 429 handler, the poller
    inbox.py      SQLite inbound queue, dedupe, the offset
    sessions.py   SQLite: topic -> session, Claude session id, timestamps
    agent.py      SDK wrapper: health check, warm clients, watchdog, interrupt
    approvals.py  can_use_tool -> inline buttons -> a tap or a 2-minute deny
    render.py     markdown -> Telegram HTML, fence-aware chunks, plain fallback
    media.py      downloads, voice transcription, saving into Attachments/
    commands.py   /new /stop /status /sessions and the stop words
    proactive.py  taps on job-sent buttons: Create task, Let Claude fix it, the fix log
    __main__.py   the daemon: lock, env, owner filter, one worker per lane

Run it under systemd (`units/tg-bridge.service`), never by hand on the Mac:

    ssh vault-host 'cd ~/automation && .venv/bin/python -m jobs.tg_bridge'
"""

import os

from runner import config

# Read once, like wa_consumer: a daemon re-reading the credential file every
# turn would pick up a half-written edit. Restarting is how config changes.
_ENV: dict[str, str] | None = None


def env(name: str, default: str = "") -> str:
    """Process environment first, ~/.automation.env second."""
    global _ENV
    value = os.environ.get(name)
    if value:
        return value
    if _ENV is None:
        _ENV = config.load_env()
    return _ENV.get(name, default)

