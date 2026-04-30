"""
SQLite-backed persistent dialog log.  db path comes from config.DB_PATH.
The module auto-creates the parent directory on first access.

Schema
------
sessions  — one row per user session (user_id, started_at, last_active, turn_count)
messages  — one row per message  (session_id FK, role, content, ts)

All timestamps are Unix epoch integers (UTC).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, List, Dict, Any, Optional

import config


def _ensure_db_dir() -> None:
    """Create the directory that will hold the SQLite file, if it doesn't exist."""
    db_dir = os.path.dirname(os.path.abspath(config.DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

_lock = threading.Lock()

# ── Schema ─────────────────────────────────────────────────────────────────
_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    started_at  INTEGER NOT NULL,
    last_active INTEGER NOT NULL,
    turn_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role         TEXT    NOT NULL CHECK(role IN ('user','assistant','alice')),
    content      TEXT    NOT NULL,
    ts           INTEGER NOT NULL,           -- when message was created/received
    delivered    INTEGER NOT NULL DEFAULT 1, -- 0=not yet sent to Alice, 1=sent
    delivered_at INTEGER                     -- unix ts when sent to Alice (NULL if not yet)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_last   ON sessions(last_active DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _migrate(con: sqlite3.Connection) -> None:
    """Apply any pending schema migrations in order."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
    ).fetchone()
    if not row:
        return
    sql = row["sql"]

    # Migration 1: add 'alice' role
    if "'alice'" not in sql:
        con.executescript("""
            ALTER TABLE messages RENAME TO messages_old;
            CREATE TABLE messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                role       TEXT    NOT NULL CHECK(role IN ('user','assistant','alice')),
                content    TEXT    NOT NULL,
                ts         INTEGER NOT NULL
            );
            INSERT INTO messages SELECT * FROM messages_old;
            DROP TABLE messages_old;
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
        """)
        # re-read after migration
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        sql = row["sql"] if row else sql

    # Migration 2: add 'delivered' column
    if "delivered" not in sql:
        con.execute("ALTER TABLE messages ADD COLUMN delivered INTEGER NOT NULL DEFAULT 1")
        sql += " delivered"  # update local copy so migration 3 check works

    # Migration 3: add 'delivered_at' column
    if "delivered_at" not in sql:
        con.execute("ALTER TABLE messages ADD COLUMN delivered_at INTEGER")


def init_db() -> None:
    """Create the DB file + tables (and run migrations). Call once at startup."""
    _ensure_db_dir()
    with _lock, _conn() as con:
        con.executescript(_DDL)
        _migrate(con)


# ── Write ──────────────────────────────────────────────────────────────────

def log_user_message(user_id: str, text: str) -> None:
    """Log a user utterance that doesn't result in a Claude API call."""
    now = int(time.time())
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT id FROM sessions WHERE user_id=? ORDER BY last_active DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            cur = con.execute(
                "INSERT INTO sessions(user_id, started_at, last_active, turn_count) VALUES(?,?,?,0)",
                (user_id, now, now),
            )
            session_id = cur.lastrowid
        else:
            session_id = row["id"]

        con.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES(?,?,?,?)",
            (session_id, "user", text, now),
        )
        con.execute(
            "UPDATE sessions SET last_active=? WHERE id=?",
            (now, session_id),
        )


def log_alice_message(user_id: str, text: str) -> None:
    """
    Log an intermediate Alice system message (e.g. 'Клод думает…', greetings,
    command acknowledgements) that don't involve a Claude API call.
    """
    now = int(time.time())
    with _lock, _conn() as con:
        row = con.execute(
            "SELECT id FROM sessions WHERE user_id=? ORDER BY last_active DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if not row:
            cur = con.execute(
                "INSERT INTO sessions(user_id, started_at, last_active, turn_count) VALUES(?,?,?,0)",
                (user_id, now, now),
            )
            session_id = cur.lastrowid
        else:
            session_id = row["id"]

        con.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES(?,?,?,?)",
            (session_id, "alice", text, now),
        )
        con.execute(
            "UPDATE sessions SET last_active=? WHERE id=?",
            (now, session_id),
        )


def _get_or_create_session(con: sqlite3.Connection, user_id: str, now: int) -> int:
    """Return the most-recent session_id for *user_id*, creating one if needed."""
    row = con.execute(
        "SELECT id FROM sessions WHERE user_id=? ORDER BY last_active DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO sessions(user_id, started_at, last_active, turn_count) VALUES(?,?,?,0)",
        (user_id, now, now),
    )
    return cur.lastrowid


def log_turn(user_id: str, user_text: str, assistant_text: str) -> None:
    """
    Append a completed user↔assistant turn to the persistent log.
    DEPRECATED for normal Claude turns — use log_user_message() + log_assistant_message()
    instead so that the user message is written before the background task starts.
    """
    now = int(time.time())
    with _lock, _conn() as con:
        session_id = _get_or_create_session(con, user_id, now)
        con.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES(?,?,?,?)",
            (session_id, "user", user_text, now),
        )
        con.execute(
            "INSERT INTO messages(session_id, role, content, ts) VALUES(?,?,?,?)",
            (session_id, "assistant", assistant_text, now),
        )
        con.execute(
            "UPDATE sessions SET last_active=?, turn_count=turn_count+1 WHERE id=?",
            (now, session_id),
        )


def log_assistant_message(user_id: str, text: str) -> int:
    """
    Log the Claude reply immediately when received (delivered=0).
    Returns the message id so it can be marked as delivered later.
    """
    now = int(time.time())
    with _lock, _conn() as con:
        session_id = _get_or_create_session(con, user_id, now)
        cur = con.execute(
            "INSERT INTO messages(session_id, role, content, ts, delivered) VALUES(?,?,?,?,0)",
            (session_id, "assistant", text, now),
        )
        con.execute(
            "UPDATE sessions SET last_active=?, turn_count=turn_count+1 WHERE id=?",
            (now, session_id),
        )
        return cur.lastrowid


def mark_delivered(message_id: int, delivered_at: Optional[int] = None) -> None:
    """
    Mark a previously logged assistant message as delivered to Alice.

    Parameters
    ----------
    message_id:
        Row id of the assistant message to mark.
    delivered_at:
        Unix timestamp of delivery. If None, current time is used.
        Pass ``user_ts + 1`` when you need the assistant message to sort
        strictly after the triggering user utterance.
    """
    ts = delivered_at if delivered_at is not None else int(time.time())
    with _lock, _conn() as con:
        con.execute(
            "UPDATE messages SET delivered=1, delivered_at=? WHERE id=?",
            (ts, message_id),
        )


def new_session(user_id: str) -> None:
    """Force-start a new session for *user_id* (called after history reset)."""
    now = int(time.time())
    with _lock, _conn() as con:
        con.execute(
            "INSERT INTO sessions(user_id, started_at, last_active, turn_count) VALUES(?,?,?,0)",
            (user_id, now, now),
        )


# ── Read ───────────────────────────────────────────────────────────────────

def list_sessions(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """Return most-recent sessions (for dashboard list view)."""
    with _conn() as con:
        rows = con.execute(
            """
            SELECT id, user_id, started_at, last_active, turn_count
            FROM sessions
            ORDER BY last_active DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_sessions() -> int:
    with _conn() as con:
        return con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def get_session(session_id: int) -> Optional[Dict[str, Any]]:
    with _conn() as con:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def get_messages(session_id: int) -> List[Dict[str, Any]]:
    """
    Return all messages for a session ordered by effective time.
    For assistant messages the effective time is delivered_at (when sent to Alice);
    for all others it is ts (when created). Undelivered assistant messages sort
    by ts so they appear in generation order if never delivered.
    """
    with _conn() as con:
        rows = con.execute(
            """
            SELECT role, content, ts, delivered, delivered_at,
                   COALESCE(delivered_at, ts) AS sort_ts
            FROM messages
            WHERE session_id=?
            ORDER BY sort_ts, id
            """,
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: int) -> None:
    """Delete a session and all its messages (CASCADE)."""
    with _lock, _conn() as con:
        con.execute("DELETE FROM sessions WHERE id=?", (session_id,))
