"""Which Claude session answers which conversation. SQLite, one transaction per write.

A lane is a private chat, or one topic in it: (chat_id, thread_id or 0). Each
lane has named sessions (the /sessions fallback, decision 6) and a pointer to
the current one. With topics working, every lane simply has one, "main".

The Claude session id is minted here (uuid4), never parsed out of the CLI's
output, so it is known and persisted before a process starts (research-1 item
4). `started` says whether a transcript can exist yet: it decides `session_id=`
versus `resume=`, and is set from the transcript file after each turn, not
from whether the turn "succeeded".

Three timestamps, per research-1 item 2: session_started_at (this Claude id was
minted), last_interaction_at (a real user message, nothing else), updated_at
(any write). There is no automatic reset: Claude Code compacts on its own.

OpenClaw moved off JSON files after a long run of corruption bugs. This is the
same file as the inbox, in WAL mode.
"""

import sqlite3
import uuid as uuidlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .inbox import connect

DEFAULT_NAME = "main"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id             INTEGER NOT NULL,
    thread_id           INTEGER NOT NULL DEFAULT 0,
    name                TEXT NOT NULL,
    claude_session_id   TEXT NOT NULL,
    started             INTEGER NOT NULL DEFAULT 0,
    warm                INTEGER NOT NULL DEFAULT 0,
    aborted_last_run    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    session_started_at  TEXT NOT NULL,
    last_interaction_at TEXT,
    updated_at          TEXT NOT NULL,
    UNIQUE(chat_id, thread_id, name)
);
CREATE TABLE IF NOT EXISTS lanes (
    chat_id      INTEGER NOT NULL,
    thread_id    INTEGER NOT NULL,
    current_name TEXT NOT NULL,
    PRIMARY KEY (chat_id, thread_id)
);
CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    session    INTEGER NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mint() -> str:
    return str(uuidlib.uuid4())


@dataclass
class Session:
    id: int
    chat_id: int
    thread_id: int
    name: str
    claude_session_id: str
    started: bool
    warm: bool
    aborted_last_run: bool
    created_at: str
    session_started_at: str
    last_interaction_at: str | None
    updated_at: str


class SessionStore:
    def __init__(self, path: Path):
        self.conn = connect(path)
        self.conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ---- reads -----------------------------------------------------------

    def get(self, session_id: int) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session(row) if row else None

    def current(self, chat_id: int, thread_id: int = 0) -> Session:
        """The lane's current session, created on first use."""
        with self._tx() as c:
            lane = c.execute("SELECT current_name FROM lanes WHERE chat_id = ? AND thread_id = ?",
                             (chat_id, thread_id)).fetchone()
            name = lane["current_name"] if lane else DEFAULT_NAME
            if not lane:
                c.execute("INSERT INTO lanes(chat_id, thread_id, current_name) VALUES (?, ?, ?)",
                          (chat_id, thread_id, name))
            return self._ensure(c, chat_id, thread_id, name)

    def list(self, chat_id: int, thread_id: int = 0) -> tuple[str, list[Session]]:
        lane = self.conn.execute(
            "SELECT current_name FROM lanes WHERE chat_id = ? AND thread_id = ?",
            (chat_id, thread_id)).fetchone()
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE chat_id = ? AND thread_id = ? ORDER BY name",
            (chat_id, thread_id)).fetchall()
        return (lane["current_name"] if lane else DEFAULT_NAME), [self._session(r) for r in rows]

    def session_for_message(self, chat_id: int, message_id: int) -> Session | None:
        row = self.conn.execute(
            "SELECT s.* FROM messages m JOIN sessions s ON s.id = m.session "
            "WHERE m.chat_id = ? AND m.message_id = ?", (chat_id, message_id)).fetchone()
        return self._session(row) if row else None

    # ---- writes ----------------------------------------------------------

    def switch(self, chat_id: int, thread_id: int, name: str) -> tuple[Session, bool]:
        """Make `name` current in the lane, creating it if new. (session, created)."""
        with self._tx() as c:
            existed = c.execute(
                "SELECT 1 FROM sessions WHERE chat_id = ? AND thread_id = ? AND name = ?",
                (chat_id, thread_id, name)).fetchone() is not None
            c.execute("INSERT INTO lanes(chat_id, thread_id, current_name) VALUES (?, ?, ?) "
                      "ON CONFLICT(chat_id, thread_id) DO UPDATE SET current_name = excluded.current_name",
                      (chat_id, thread_id, name))
            return self._ensure(c, chat_id, thread_id, name), not existed

    def switch_to(self, session: Session) -> None:
        """Quote-reply routing: the replied-to message's session becomes current."""
        self.switch(session.chat_id, session.thread_id, session.name)

    def reset(self, session_id: int) -> Session:
        """`/new`, or a failed health check: a fresh Claude id under the same name."""
        now = _now()
        with self._tx() as c:
            c.execute("UPDATE sessions SET claude_session_id = ?, started = 0, warm = 0, "
                      "session_started_at = ?, updated_at = ? WHERE id = ?",
                      (mint(), now, now, session_id))
        return self.get(session_id)

    def set_started(self, session_id: int, started: bool) -> None:
        self._update(session_id, started=int(started))

    def set_warm(self, session_id: int, warm: bool) -> None:
        self._update(session_id, warm=int(warm))

    def set_aborted(self, session_id: int, aborted: bool) -> None:
        self._update(session_id, aborted_last_run=int(aborted))

    def touch_interaction(self, session_id: int) -> None:
        self._update(session_id, last_interaction_at=_now())

    def remember_message(self, chat_id: int, message_id: int | None, session_id: int) -> None:
        if not message_id:
            return
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO messages(chat_id, message_id, session, at) "
                      "VALUES (?, ?, ?, ?)", (chat_id, message_id, session_id, _now()))

    def clear_warm_flags(self) -> None:
        """At startup no client is warm, whatever the last process believed."""
        with self._tx() as c:
            c.execute("UPDATE sessions SET warm = 0")

    # ---- internals -------------------------------------------------------

    _COLUMNS = {"started", "warm", "aborted_last_run", "last_interaction_at"}

    def _update(self, session_id: int, **fields) -> None:
        assert set(fields) <= self._COLUMNS
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as c:
            c.execute(f"UPDATE sessions SET {sets}, updated_at = ? WHERE id = ?",
                      (*fields.values(), _now(), session_id))

    def _ensure(self, c: sqlite3.Connection, chat_id: int, thread_id: int, name: str) -> Session:
        row = c.execute("SELECT * FROM sessions WHERE chat_id = ? AND thread_id = ? AND name = ?",
                        (chat_id, thread_id, name)).fetchone()
        if row:
            return self._session(row)
        now = _now()
        c.execute("INSERT INTO sessions(chat_id, thread_id, name, claude_session_id, created_at, "
                  "session_started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (chat_id, thread_id, name, mint(), now, now, now))
        row = c.execute("SELECT * FROM sessions WHERE chat_id = ? AND thread_id = ? AND name = ?",
                        (chat_id, thread_id, name)).fetchone()
        return self._session(row)

    @staticmethod
    def _session(r: sqlite3.Row) -> Session:
        return Session(r["id"], r["chat_id"], r["thread_id"], r["name"], r["claude_session_id"],
                       bool(r["started"]), bool(r["warm"]), bool(r["aborted_last_run"]),
                       r["created_at"], r["session_started_at"], r["last_interaction_at"],
                       r["updated_at"])
