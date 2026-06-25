"""SQLite persistence for per-user conversation history.

Schema: one row per message. We rebuild the model's context window by selecting
the most recent N messages in order. The system prompt is stored separately so
we don't have to rebuild it on every turn.

Each user gets their own thread; group chats share a thread per chat id.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    thread_id    TEXT NOT NULL,           -- user_id for private chats, "{chat_id}:{thread_id}" for groups
    role         TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
    content      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    msg_id       INTEGER PRIMARY KEY AUTOINCREMENT
);
CREATE INDEX IF NOT EXISTS idx_thread_msg ON sessions(thread_id, msg_id);
"""


class SessionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def append(self, thread_id: str, role: str, content: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions(thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (thread_id, role, content, time.time()),
            )

    def history(self, thread_id: str, limit: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM sessions WHERE thread_id = ? "
                "ORDER BY msg_id DESC LIMIT ?",
                (thread_id, limit),
            ).fetchall()
        # Reverse to chronological order
        rows.reverse()
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def reset(self, thread_id: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE thread_id = ?", (thread_id,))
            return cur.rowcount

    def replace_last_assistant(self, thread_id: str, new_content: str) -> int:
        """Replace the most recent assistant message for this thread.

        Returns 1 if a row was updated, 0 if there was no assistant turn to replace.
        Used by Story Engine rewrite.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE sessions SET content = ? "
                "WHERE msg_id = (SELECT msg_id FROM sessions WHERE thread_id = ? AND role = ? "
                "ORDER BY msg_id DESC LIMIT 1)",
                (new_content, thread_id, ROLE_ASSISTANT),
            )
            return cur.rowcount

    def count(self, thread_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE thread_id = ?", (thread_id,)
            ).fetchone()
            return row["n"]

    def all_threads(self) -> list[tuple[str, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT thread_id, COUNT(*) AS n FROM sessions GROUP BY thread_id ORDER BY MAX(msg_id) DESC"
            ).fetchall()
            return [(r["thread_id"], r["n"]) for r in rows]