"""The inbound queue. Every update is on disk before Telegram is told it was received.

The commit point is the whole design (research-3 item 4):

  1. Write the raw update to SQLite, commit.
  2. THEN advance the stored offset, which is what the next getUpdates sends,
     and which is how Telegram learns it may forget the update.

A crash between the two re-fetches the batch; dedupe by update_id and by
(chat_id, message_id) makes that harmless. The reverse order would lose a
message on every crash in that window.

Row states:

    pending   on disk, not yet handed to the agent. Processed after a restart.
    adopted   handed to the agent. NEVER replayed after a restart: a half-run
              tool may already have acted. The owner gets a notice instead.
    done      finished, successfully or with a reported failure.
    dead      dropped on purpose (not the owner, discarded by /stop,
              interrupted by a restart). Kept for dedupe until pruned.

A voice transcript lives in its row until the turn completes, then is erased
(build-plan decision 2: never in the vault, not kept afterwards either).
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

PENDING, ADOPTED, DONE, DEAD = "pending", "adopted", "done", "dead"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offsets (
    key         TEXT PRIMARY KEY,      -- "<bot id>:<token fingerprint>", never the token
    next_offset INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS updates (
    update_id   INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,         -- message | callback_query | other
    chat_id     INTEGER,
    thread_id   INTEGER,
    message_id  INTEGER,
    raw         TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'pending',
    transcript  TEXT,
    note        TEXT,
    received_at TEXT NOT NULL,
    adopted_at  TEXT,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS updates_chat_message
    ON updates(chat_id, message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS updates_state ON updates(state);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path) -> sqlite3.Connection:
    """WAL plus a busy timeout: two connections (inbox, sessions) share one file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@dataclass
class Row:
    update_id: int
    kind: str
    chat_id: int | None
    thread_id: int
    message_id: int | None
    raw: dict
    state: str
    transcript: str | None = None
    note: str | None = None

    @property
    def message(self) -> dict:
        return self.raw.get("message") or {}


def topic_of(msg: dict) -> int:
    thread = msg.get("message_thread_id")
    if not thread:
        return 0
    if (msg.get("chat") or {}).get("type") == "private" or msg.get("is_topic_message"):
        return int(thread)
    return 0


def describe(update: dict) -> tuple[str, int | None, int, int | None]:
    """(kind, chat_id, thread_id, message_id) for an update.

    In groups the thread only counts when Telegram says the message is in a
    topic: message_thread_id also appears on plain reply chains there (OpenClaw
    bug #2731), and keying sessions on that splits one conversation. A private
    chat has no reply threads, so there any thread id is a topic (a guess until
    the Phase 0 topics spike runs against a real bot; see docs-tg/NOTES.md).
    """
    if "message" in update:
        msg = update["message"]
        return ("message", (msg.get("chat") or {}).get("id"), topic_of(msg),
                msg.get("message_id"))
    if "callback_query" in update:
        msg = (update["callback_query"].get("message") or {})
        thread = topic_of(msg)
        # message_id stays NULL: the button's message is the bot's own, and
        # two taps on one keyboard are two distinct updates.
        return "callback_query", (msg.get("chat") or {}).get("id"), thread, None
    return "other", None, 0, None


class Inbox:
    def __init__(self, path: Path):
        self.conn = connect(path)
        self.conn.executescript(_SCHEMA)

    # ---- offset ----------------------------------------------------------

    def next_offset(self, key: str) -> int | None:
        row = self.conn.execute("SELECT next_offset FROM offsets WHERE key = ?",
                                (key,)).fetchone()
        return row["next_offset"] if row else None

    def _advance_offset(self, key: str, next_offset: int) -> None:
        self.conn.execute(
            "INSERT INTO offsets(key, next_offset, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET next_offset = MAX(next_offset, excluded.next_offset), "
            "updated_at = excluded.updated_at",
            (key, next_offset, _now()))

    # ---- intake ----------------------------------------------------------

    def store(self, updates: list[dict], offset_key: str) -> list[Row]:
        """Persist a getUpdates batch, then advance the offset. Returns new rows only.

        Two separate commits, in this order, on purpose: see the module docstring.
        """
        fresh: list[Row] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for update in updates:
                update_id = int(update["update_id"])
                kind, chat_id, thread_id, message_id = describe(update)
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO updates(update_id, kind, chat_id, thread_id, "
                    "message_id, raw, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (update_id, kind, chat_id, thread_id, message_id,
                     json.dumps(update, ensure_ascii=False), _now()))
                if cur.rowcount:
                    fresh.append(Row(update_id, kind, chat_id, thread_id, message_id,
                                     update, PENDING))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        if updates:
            self._advance_offset(offset_key, max(int(u["update_id"]) for u in updates) + 1)
        return fresh

    # ---- lifecycle -------------------------------------------------------

    def adopt(self, update_ids: list[int]) -> None:
        """Mark rows as handed to the agent. Written before the agent starts."""
        self._set(update_ids, ADOPTED, stamp="adopted_at")

    def finish(self, update_ids: list[int], state: str = DONE, note: str | None = None) -> None:
        """Done or dead. The transcript is erased here, by decision 2."""
        if not update_ids:
            return
        marks = ",".join("?" * len(update_ids))
        self.conn.execute(
            f"UPDATE updates SET state = ?, note = COALESCE(?, note), transcript = NULL, "
            f"finished_at = ? WHERE update_id IN ({marks})",
            (state, note, _now(), *update_ids))

    def _set(self, update_ids: list[int], state: str, *, stamp: str) -> None:
        if not update_ids:
            return
        marks = ",".join("?" * len(update_ids))
        self.conn.execute(f"UPDATE updates SET state = ?, {stamp} = ? "
                          f"WHERE update_id IN ({marks})", (state, _now(), *update_ids))

    def set_transcript(self, update_id: int, text: str) -> None:
        self.conn.execute("UPDATE updates SET transcript = ? WHERE update_id = ?",
                          (text, update_id))

    def get(self, update_id: int) -> Row | None:
        row = self.conn.execute("SELECT * FROM updates WHERE update_id = ?",
                                (update_id,)).fetchone()
        return self._row(row) if row else None

    def recover(self) -> tuple[list[Row], list[Row]]:
        """After a restart: (pending rows to process, adopted rows to report).

        Adopted rows are only returned. The caller sends the "interrupted by a
        restart" notice and then finishes them as dead, so a crash between the
        two repeats the notice rather than losing it.
        """
        pending = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM updates WHERE state = ? ORDER BY update_id", (PENDING,))]
        adopted = [self._row(r) for r in self.conn.execute(
            "SELECT * FROM updates WHERE state = ? ORDER BY update_id", (ADOPTED,))]
        return pending, adopted

    def prune(self, days: int = 14) -> int:
        """Old finished rows go. Dedupe only needs rows above the offset, which
        are never this old."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self.conn.execute("DELETE FROM updates WHERE state IN (?, ?) AND received_at < ?",
                                (DONE, DEAD, cutoff))
        return cur.rowcount

    @staticmethod
    def _row(r: sqlite3.Row) -> Row:
        return Row(r["update_id"], r["kind"], r["chat_id"], r["thread_id"] or 0,
                   r["message_id"], json.loads(r["raw"]), r["state"], r["transcript"], r["note"])
