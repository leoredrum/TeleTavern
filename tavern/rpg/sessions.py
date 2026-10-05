"""RPG game sessions + per-turn log, ported from connector/dungeon-master-bot/bot.py.

One active session per Telegram chat; /newgame archives the previous one.
Every turn is stored in SQLite and appended to sessions/<id>/raw_log.md, which
the export commands turn into a script or novel notes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS game_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE, chat_id INTEGER, title TEXT, player_name TEXT,
    character_name TEXT, status TEXT, summary TEXT, log_path TEXT, export_path TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS game_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, turn_number INTEGER, speaker TEXT, raw_text TEXT,
    cleaned_text TEXT, metadata_json TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_session ON game_turns(session_id, turn_number);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _cn_int(n: int) -> str:
    digits = "零一二三四五六七八九"
    if n < 10:
        return digits[n]
    if n < 20:
        return "十" + (digits[n % 10] if n % 10 else "")
    if n < 100:
        return digits[n // 10] + "十" + (digits[n % 10] if n % 10 else "")
    return str(n)


class RPGSessions:
    def __init__(self, db_path: Path, sessions_dir: Path, character_name: str):
        self.db_path = Path(db_path)
        self.sessions_dir = Path(sessions_dir)
        self.character_name = character_name
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.executescript(_SCHEMA)
            db.commit()

    # ---- paths -----------------------------------------------------------------
    def session_dir(self, session_id: str) -> Path:
        return self.sessions_dir / session_id

    def raw_log_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "raw_log.md"

    # ---- sessions -----------------------------------------------------------------
    def archive_active(self, chat_id: int) -> int:
        with closing(sqlite3.connect(self.db_path)) as db:
            cur = db.execute("UPDATE game_sessions SET status='archived', updated_at=? "
                             "WHERE chat_id=? AND status='active'", (_now(), chat_id))
            db.commit()
            return cur.rowcount

    def create(self, chat_id: int, title: str = "新冒险", player_name: str = "玩家") -> dict:
        self.archive_active(chat_id)
        session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        now = _now()
        sdir = self.session_dir(session_id)
        (sdir / "exports").mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO game_sessions(session_id, chat_id, title, player_name, "
                       "character_name, status, summary, log_path, export_path, created_at, updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (session_id, chat_id, title, player_name, self.character_name, "active", "",
                        str(self.raw_log_path(session_id)), str(sdir / "exports"), now, now))
            db.commit()
        meta = {"session_id": session_id, "title": title, "telegram_chat_id": chat_id,
                "player_name": player_name, "character_name": self.character_name,
                "status": "active", "created_at": now, "updated_at": now}
        (sdir / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return meta

    def active(self, chat_id: int) -> dict | None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM game_sessions WHERE chat_id=? AND status='active' "
                           "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
            return dict(r) if r else None

    def get_or_create(self, chat_id: int) -> dict:
        return self.active(chat_id) or self.create(chat_id)

    def set_status(self, session_id: str, status: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE game_sessions SET status=?, updated_at=? WHERE session_id=?",
                       (status, _now(), session_id))
            db.commit()
        meta_path = self.session_dir(session_id) / "metadata.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update(status=status, updated_at=_now())
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def touch(self, session_id: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("UPDATE game_sessions SET updated_at=? WHERE session_id=?", (_now(), session_id))
            db.commit()

    def list_recent(self, chat_id: int, limit: int = 10) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM game_turns t WHERE t.session_id=s.session_id "
                "AND t.speaker='player') AS turn_count FROM game_sessions s WHERE chat_id=? "
                "ORDER BY id DESC LIMIT ?", (chat_id, limit)).fetchall()
            return [dict(r) for r in rows]

    # ---- turns --------------------------------------------------------------------
    def next_turn(self, session_id: str) -> int:
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute("SELECT COALESCE(MAX(turn_number),0)+1 FROM game_turns WHERE session_id=?",
                              (session_id,)).fetchone()[0]

    def record(self, session: dict, speaker: str, turn: int, text: str, metadata: dict | None = None) -> None:
        cleaned = (text or "").strip()
        with closing(sqlite3.connect(self.db_path)) as db:
            db.execute("INSERT INTO game_turns(session_id, turn_number, speaker, raw_text, cleaned_text, "
                       "metadata_json, created_at) VALUES(?,?,?,?,?,?,?)",
                       (session["session_id"], turn, speaker, text or "", cleaned,
                        json.dumps(metadata or {}, ensure_ascii=False), _now()))
            db.commit()
        self._append_turn_md(session, speaker, turn, cleaned)

    def turns(self, session_id: str) -> list[dict]:
        with closing(sqlite3.connect(self.db_path)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT * FROM game_turns WHERE session_id=? ORDER BY turn_number, id",
                              (session_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---- markdown log / exports -------------------------------------------------------
    def _md_header(self, session: dict) -> str:
        return (f"# {session.get('title') or '新冒险'}\n\n"
                f"- Session ID: `{session.get('session_id')}`\n"
                f"- 角色: {session.get('character_name') or self.character_name}\n"
                f"- 创建: {session.get('created_at')}\n\n---\n")

    def _append_turn_md(self, session: dict, speaker: str, turn: int, text: str) -> None:
        path = self.raw_log_path(session["session_id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(self._md_header(session), encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            if speaker == "player":
                f.write(f"\n## 第 {turn} 回合\n\n**玩家**\n\n{text}\n")
            elif speaker == "gm":
                f.write(f"\n**{self.character_name}**\n\n{text}\n\n---\n")
            else:
                f.write(f"\n## 第 {turn} 回合\n\n**系统**\n\n{text}\n")

    def append_state_md(self, session: dict, turn: int, before: str, after: str,
                        changes: list, conflicts: list, rule_changes=None, director_changes=None) -> None:
        path = self.raw_log_path(session["session_id"])
        if not path.exists():
            return

        def fmt_changes(items) -> str:
            return "；".join(f"{c.get('entity', '?')}：{c.get('change', '?')}" for c in (items or [])) or "（无）"

        def fmt_conflicts(items) -> str:
            return "；".join(f"{c.get('category', '?')}：{c.get('entity', '?')}" for c in (items or [])) or "（无）"

        block = "\n".join([
            f"\n> **【第 {turn} 回合 · 世界状态】**",
            f"> - Before：{before or '（空）'}",
            f"> - 叙事变更：{fmt_changes(changes)}",
            f"> - 规则变更：{fmt_changes(rule_changes)}",
            f"> - 导演变更：{fmt_changes(director_changes)}",
            f"> - After：{after or '（空）'}",
            f"> - 冲突：{fmt_conflicts(conflicts)}",
            "",
        ])
        with path.open("a", encoding="utf-8") as f:
            f.write(block)

    def _by_turn(self, session_id: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for t in self.turns(session_id):
            out.setdefault(t["turn_number"], {})[t["speaker"]] = t["cleaned_text"] or t["raw_text"] or ""
        return out

    def build_script(self, session: dict) -> str:
        by_turn = self._by_turn(session["session_id"])
        out = [f"# 剧本：{session.get('title') or '未命名冒险'}\n",
               "> 由 raw_log 自动转换的基础剧本格式（未润色）。\n"]
        for n in sorted(by_turn):
            g = by_turn[n]
            out.append(f"\n## 第{_cn_int(n)}幕\n")
            if "player" in g:
                out.append(f"\n**玩家**：\n{g['player']}\n")
            if "gm" in g:
                out.append(f"\n**{self.character_name}**：\n{g['gm']}\n")
            elif "system" in g:
                out.append(f"\n**旁白**：\n{g['system']}\n")
            out.append("\n---\n")
        return "\n".join(out)

    def build_notes(self, session: dict) -> str:
        by_turn = self._by_turn(session["session_id"])
        choices = [by_turn[k]["player"] for k in sorted(by_turn) if "player" in by_turn[k]]
        first_gm = next((by_turn[k]["gm"] for k in sorted(by_turn) if "gm" in by_turn[k]), "")
        lines = [f"# 小说素材：{session.get('title') or '未命名冒险'}\n",
                 f"- Session ID：`{session.get('session_id')}`\n",
                 "\n## 主要人物\n", f"- 主角：{session.get('player_name') or '玩家'}（玩家）",
                 f"- 叙事者：{self.character_name}\n",
                 "\n## 开场\n", first_gm[:1200] or "（无）", "\n\n## 玩家关键选择\n"]
        lines += [f"{i}. {c}" for i, c in enumerate(choices, 1)] or ["（无）"]
        return "\n".join(lines)

    def write_export(self, session: dict, name: str, content: str) -> Path:
        path = self.session_dir(session["session_id"]) / name
        path.write_text(content, encoding="utf-8")
        return path
