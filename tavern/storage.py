"""Per-bot SQLite storage: chat messages, chat→character bindings, small KV."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS bindings (
    chat_id    INTEGER PRIMARY KEY,
    card_file  TEXT NOT NULL,
    name       TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BotStorage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(_SCHEMA)
            c.commit()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---- messages ---------------------------------------------------------
    def append(self, thread_id: str, role: str, content: str) -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO messages(thread_id, role, content, ts) VALUES(?,?,?,?)",
                      (thread_id, role, content, _now()))
            c.commit()

    def history(self, thread_id: str, limit: int) -> list[dict]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT role, content FROM messages WHERE thread_id=? "
                             "ORDER BY id DESC LIMIT ?", (thread_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def count(self, thread_id: str) -> int:
        with closing(self._conn()) as c:
            return c.execute("SELECT COUNT(*) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()[0]

    def reset(self, thread_id: str) -> int:
        with closing(self._conn()) as c:
            cur = c.execute("DELETE FROM messages WHERE thread_id=?", (thread_id,))
            c.commit()
            return cur.rowcount

    # ---- bindings -----------------------------------------------------------
    def get_binding(self, chat_id: int) -> dict | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM bindings WHERE chat_id=?", (chat_id,)).fetchone()
            return dict(r) if r else None

    def set_binding(self, chat_id: int, card_file: str, name: str) -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO bindings(chat_id, card_file, name, updated_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(chat_id) DO UPDATE SET card_file=excluded.card_file, "
                      "name=excluded.name, updated_at=excluded.updated_at",
                      (chat_id, card_file, name, _now()))
            c.commit()

    # ---- kv -----------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return r["v"] if r else default

    def set(self, key: str, value: str) -> None:
        with closing(self._conn()) as c:
            c.execute("INSERT INTO kv(k, v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (key, value))
            c.commit()
