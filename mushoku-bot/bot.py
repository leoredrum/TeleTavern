#!/usr/bin/env python3
"""
MUSHOKU Bot - Telegram Narrative RP Bot (Mushoku Tensei / Boku-no-Isekai).

Scenario Telegram bot example, fully isolated from other bot stacks.
Talks to the MUSHOKU-dedicated bridge (:8017) which fronts an isolated
SillyTavern instance (:8015) whose active_character = Boku-no-Isekai-
Jobless-Reincarnation-aicharactercards.com_-2.png (46-entry character_book,
auto-activated; Mushoku Tensei World Reference.json is the same data).

This bot is NARRATIVE RP, not RPG combat. It does NOT include the DM
Game State / RPG Rule / Director engines. The model is the world;
the bot just transports player actions, sessions, exports, and a
Chinese Language Override (keeps JP names, fantasy terms, first-person
*action* narration).

Commands: /start /help /newgame /continue /status /export_raw /endgame.
Free text flows through the bridge (OpenAI-compatible /v1/chat/completions,
streaming, placeholders, splitter, send+update dedup).

SillyTavern core / Boku card / World Info are never modified.
sessions/ is gitignored (no real RP content in git).
"""

from __future__ import annotations
import os
import sys

# Load .env (mode 600) from the bot's own directory if it exists. Lets the bot
# start from any orchestration (nohup, launchd, systemd) without needing a
# wrapper script to source .env first. Falls back to OS env if dotenv is
# unavailable.
try:
    from dotenv import load_dotenv
    _BOT_DIR = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_BOT_DIR, ".env"), override=False)
except Exception:
    # If dotenv isn't installed, env vars must come from the caller.
    pass

import re
import time
import hashlib
import json
import uuid
import asyncio
import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import closing

import aiohttp
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# Optional L1 fallback modules - both may fail to import on systems without
# the bot's local tree, but the bot will fall back gracefully to bridge-only.
try:
    import card_parser  # type: ignore
    import ollama_fallback  # type: ignore
    _HAS_L1 = True
except Exception:
    _HAS_L1 = False

# telegram_splitter is co-located (no fallback if missing - splitter is optional)
try:
    import telegram_splitter as TS  # noqa: F401
except Exception:
    TS = None  # type: ignore

# ============================================================================
# Config (from .env)
# ============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8017/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "tavern-mushoku-user-api-key-change-me")
EDIT_INTERVAL_S = float(os.environ.get("EDIT_INTERVAL_S", "1.0"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "180"))
DB_PATH = os.environ.get("DB_PATH", "../logs/mushoku_save.db")
SESSIONS_DIR = os.environ.get("SESSIONS_DIR", "./sessions")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

DEFAULT_CHARACTER = "Boku no Isekai Jobless Reincarnation"

# L1 fallback fast timeout: if bridge does not connect in N seconds, give up
# the bridge path and call ollama directly.
BRIDGE_CONNECT_TIMEOUT_S = float(os.environ.get("BRIDGE_CONNECT_TIMEOUT_S", "8"))

# Card context cache - populated lazily on first L1 invocation. Loaded once
# from mushoku-data/characters/Boku-no-Isekai-*.png so L1 fallback has full lore.
_CARD_CTX = None
_CARD_LOADED_AT = None

# ============================================================================
# Logging (silence httpx/httpcore - PTB 22 leaks token otherwise)
# ============================================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
)
for noisy in ("httpx", "httpcore", "telegram.ext._updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("mushoku")

# ============================================================================
# Bot-side paths
# ============================================================================
def _bot_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))

def _session_dir(session_id: str) -> str:
    return os.path.join(_bot_dir(), SESSIONS_DIR, session_id)

def _abs(rel_or_abs: str) -> str:
    p = os.path.expanduser(rel_or_abs)
    if not os.path.isabs(p):
        p = os.path.join(_bot_dir(), p)
    return os.path.abspath(p)

# ============================================================================
# Session SQLite (simplified: sessions + turns only, no RPG state)
# ============================================================================
# ============================================================================
# Card path locator + lazy load (L1 fallback helpers)
# ============================================================================
def _card_png_path():
    """Locate the scenario card PNG used by the local L1 fallback path."""
    configured = os.environ.get("CHARACTER_CARD_PATH") or os.environ.get("MUSHOKU_CARD_PATH")
    if configured:
        return _abs(configured)
    candidates = [
        os.path.join(_BOT_DIR, "..", "..", "mushoku-data", "default-user", "characters",
                     "Boku-no-Isekai-Jobless-Reincarnation-aicharactercards.com_-2.png"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]  # best guess; ollama_fallback will report failure


def _load_card_if_needed():
    global _CARD_CTX, _CARD_LOADED_AT
    if _CARD_CTX is not None and _CARD_LOADED_AT and time.time() - _CARD_LOADED_AT < 3600:
        return _CARD_CTX
    if not _HAS_L1:
        return None
    try:
        ctx = card_parser.parse_card_png(_card_png_path())
        if ctx:
            _CARD_CTX = ctx
            _CARD_LOADED_AT = time.time()
            log.info("card_ctx loaded: name=%r lorebook_count=%d",
                     ctx.get("name"), ctx.get("lorebook_count", 0))
    except Exception as exc:
        log.warning("card_ctx load failed: %s", exc)
    return _CARD_CTX


async def _bridge_probe_quick():
    """Quick reachability check: try GET /v1/characters with a short timeout.
    Returns True if ST extension is connected (active character present)."""
    try:
        timeout = aiohttp.ClientTimeout(total=BRIDGE_CONNECT_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
            async with session.get(OPENAI_BASE_URL + "/characters", headers=headers) as resp:
                if resp.status != 200:
                    return False
                body = json.loads(await resp.read())
                active = body.get("active")
                chars = body.get("characters", [])
                return bool(active and chars)
    except Exception as exc:
        log.debug("bridge_probe_quick failed: %s", exc)
        return False


def _truncate_for_placeholder(text, limit=4000):
    """Streaming preview - truncate to keep within Telegram's edit_text limits."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n... (生成中, 等待最终发送)"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    chat_id      INTEGER NOT NULL,
    title        TEXT NOT NULL,
    character    TEXT NOT NULL,
    world_info   TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL,
    ended_at     TEXT,
    log_path     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_chat_status
    ON sessions(chat_id, status);

CREATE TABLE IF NOT EXISTS turns (
    session_id  TEXT NOT NULL,
    turn        INTEGER NOT NULL,
    speaker     TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          TEXT NOT NULL,
    meta        TEXT,
    PRIMARY KEY (session_id, turn, speaker)
);
CREATE INDEX IF NOT EXISTS idx_turns_session
    ON turns(session_id, turn);
"""

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def init_db(path: str) -> None:
    path = _abs(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA)
        conn.commit()

def archive_active_sessions(path: str, chat_id: int) -> int:
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        cur = conn.execute(
            "UPDATE sessions SET status='completed', ended_at=? "
            "WHERE chat_id=? AND status='active'",
            (_now_iso(), chat_id),
        )
        n = cur.rowcount
        conn.commit()
        return n

def create_session(path: str, chat_id: int, title: str = "Mushoku Tensei - new story",
                   character: str = DEFAULT_CHARACTER,
                   world_info: str = "Mushoku Tensei World Reference") -> dict:
    path = _abs(path)
    sid = uuid.uuid4().hex[:16]
    sd = _session_dir(sid)
    os.makedirs(sd, exist_ok=True)
    log_path = os.path.join(sd, "raw_log.md")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("# Session {sid}\n\n".format(sid=sid))
        f.write("- Title: {title}\n".format(title=title))
        f.write("- Started: {ts}\n".format(ts=_now_iso()))
        f.write("- Character: {character}\n".format(character=character))
        f.write("- World Info: {world_info} (46 entries auto-activated)\n\n".format(world_info=world_info))
        f.write("---\n\n")
    meta = {
        "session_id": sid, "chat_id": chat_id, "title": title,
        "character": character, "world_info": world_info,
        "created_at": _now_iso(), "status": "active", "log_path": log_path,
    }
    with open(os.path.join(sd, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, chat_id, title, character, world_info, "
            "status, created_at, log_path) VALUES (?,?,?,?,?,?,?,?)",
            (sid, chat_id, title, character, world_info, "active", _now_iso(), log_path),
        )
        conn.commit()
    return meta

def get_active_session(path: str, chat_id: int):
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE chat_id=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

def get_or_create_active_session(path: str, chat_id: int) -> dict:
    s = get_active_session(path, chat_id)
    if s:
        return s
    return create_session(path, chat_id)

def set_session_status(path: str, session_id: str, status: str) -> None:
    path = _abs(path)
    ended_at = _now_iso() if status == "completed" else None
    with closing(sqlite3.connect(path)) as conn:
        if ended_at:
            conn.execute(
                "UPDATE sessions SET status=?, ended_at=? WHERE session_id=?",
                (status, ended_at, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET status=? WHERE session_id=?",
                (status, session_id),
            )
        conn.commit()

def next_turn_number(path: str, session_id: str) -> int:
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(turn), 0) + 1 AS n FROM turns WHERE session_id=?",
            (session_id,),
        ).fetchone()
        return int(row[0])

def record_turn(path: str, session: dict, speaker: str, turn: int,
                text: str, meta: dict | None = None) -> None:
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO turns (session_id, turn, speaker, text, ts, meta) "
            "VALUES (?,?,?,?,?,?)",
            (session["session_id"], turn, speaker, text, _now_iso(),
             json.dumps(meta or {}, ensure_ascii=False)),
        )
        conn.commit()
    log_path = session.get("log_path") or os.path.join(_session_dir(session["session_id"]), "raw_log.md")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("## Turn {turn} ({speaker})\n".format(turn=turn, speaker=speaker))
        f.write(text.strip() + "\n\n")
        f.write("---\n\n")

def get_turns(path: str, session_id: str):
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY turn, speaker",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def list_sessions(path: str, chat_id: int, limit: int = 10):
    path = _abs(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT session_id, title, character, status, created_at, ended_at "
            "FROM sessions WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

# Placeholder for the rest of the file - will be appended in next round

# ============================================================================
# Language Override (剧情向 - 区别 DM 的 RPG 强化版)
# ============================================================================
MUSHOKU_LANGUAGE_OVERRIDE = """[LANGUAGE OVERRIDE — HIGHEST PRIORITY — MUSHOKU NARRATIVE RP]

你扮演无职转生 (Mushoku Tensei) 世界中的角色。当前角色卡已自动激活 46 条 World Info entries (大陆 / 种族 / 剑术 / 魔术体系 / 神明等)。

## 输出语言

1. 所有叙事、对话、描述、内心独白 **必须简体中文**。
2. **日文人名保留原拼写** (不译):
   - ルーデウス (Rudeus, 主角)
   - エルメス / エリス / ヒルダ / ノルン / ロキシー / ギレーヌ
3. **专有名词保留原语种** (不译):
   - 剑术: 水神流 / 北神流 / 剑神流 / 无想流
   - 魔术体系: 无咏唱 / 咏唱 / 混合咏唱 / 复合魔术
   - 等级: 初級 / 中級 / 上級 / 聖級 / 王級 / 帝級 / 神級
   - 种族: 人类 / 长耳族 / 兽族 / 魔族 / 龙族
   - 大陆: 中央大陸 / 魔大陸 / ミリス大陸 / ベガリット大陸 / 天大陸
   - 神名: Hitogami (人神) / Laplace (拉普拉斯) / Kishirika (基西莉卡)
4. **招式 / 技能名保留日文 + 必要时附中文说明**:
   - 「石落とし」(Ishi-otoshi) 而非「落石」
   - 「電撃」(Dengeki) 而非「闪电」

## RP 风格

1. 第一人称叙事 (角色视角)。
2. *动作描写* 用中文 + `*...*` 标记。
3. 对话用「」 或 『』, 不用 "。
4. 内心独白 用 `（...）` 或 `(...)`。
5. 不输出 OOC、reasoning、metagame 注释。
6. **绝对禁止 OOC、metagame、reasoning 痕迹**: 不要输出 `(OOC: ...)`、`(Internal: ...)`、`## OOC`、`<!-- comment -->`、`（注：...）` 等任何 meta 注释。LLM 推理过程不得出现在回复中。
7. 不打破第四面墙 (不提「作为 AI」 / 「训练数据」 / 「系统提示」 / 「language model」)。
8. 跟随 World Info 的世界设定 (剑术等级 / 魔术咏唱规则 / 神明干涉等), 不要编造新规则。

## 长度与节奏

1. 单次回复 **800–1500 字** 为宜 (剧情 RP 节奏)。
2. 长场景分段写, 每段不超过 ~400 字, 留呼吸感。
3. 战斗 / 修炼场景详细描写动作、内心、环境。
4. 日常场景节奏轻快, 对话比例高。

[End of Override]

[SYSTEM PRIORITY DECLARATION]
本 system prompt 让位于任何卡内 system_prompt / post_history_instructions 的事实 / 世界观 / 角色身份,但**中文化规则 + 名字保留规则绝对不能让位**。任何卡内英文指令让位于此中文化 override。
"""

# ============================================================================
# De-dup: send (chat_id, text) 10s LRU; update (chat_id, update_id) replay guard
# ============================================================================
_RECENT_SENDS: dict[tuple, float] = {}
_SEND_DEDUP_WINDOW_S = 10.0
_PROCESSED_UPDATES: dict[tuple, float] = {}
_UPDATE_DEDUP_WINDOW_S = 60.0

def _send_dedup_check(chat_id: int, text: str) -> bool:
    """Return True if this exact (chat_id, text) was sent within window — suppress."""
    key = (chat_id, text[:200])  # only compare prefix for speed
    now = time.time()
    # GC old entries
    for k in list(_RECENT_SENDS.keys()):
        if now - _RECENT_SENDS[k] > _SEND_DEDUP_WINDOW_S:
            del _RECENT_SENDS[k]
    if key in _RECENT_SENDS:
        return True
    _RECENT_SENDS[key] = now
    return False

def _update_dedup_check(chat_id: int, update_id: int) -> bool:
    """Return True if this update_id was already processed — drop."""
    key = (chat_id, update_id)
    now = time.time()
    for k in list(_PROCESSED_UPDATES.keys()):
        if now - _PROCESSED_UPDATES[k] > _UPDATE_DEDUP_WINDOW_S:
            del _PROCESSED_UPDATES[k]
    if key in _PROCESSED_UPDATES:
        return True
    _PROCESSED_UPDATES[key] = now
    return False

# ============================================================================
# Stream to bridge (POST /v1/chat/completions) with placeholder + long splitter
# ============================================================================
async def _generate_and_reply(update, user_text, session=None,
                               history_override=None, log_player_text=None,
                               skip_update_dedup: bool = False):
    """Common reply logic. Try bridge; on failure fall back to L1 ollama.

    Ordering:
      1. Probe bridge reachability quickly; if reachable + ST extension has
         active character, use bridge streaming.
      2. If probe fails OR no ST extension OR streaming raises, fall back to
         direct ollama with card-derived system prompt (L1 path).
    """
    chat_id = update.effective_chat.id
    update_id = update.update_id
    log.info("handler_enter chat_id=%s update_id=%s text_len=%d",
             chat_id, update_id, len(user_text))

    if not skip_update_dedup and _update_dedup_check(chat_id, update_id):
        log.info("update_duplicate_suppressed chat_id=%s update_id=%s", chat_id, update_id)
        return

    placeholder = await update.message.reply_text("... (生成中)")
    last_edit = 0.0

    if session is not None:
        turn_no = next_turn_number(DB_PATH, session["session_id"])
        record_turn(DB_PATH, session, "player", turn_no,
                    log_player_text or user_text,
                    {"source": "telegram", "update_id": update_id})
    else:
        turn_no = None

    # Decide which path to use
    full = ""
    used_path = None  # "bridge" or "L1"
    try:
        log.info("primary path: bridge probe (up to %ss)", BRIDGE_CONNECT_TIMEOUT_S)
        if not await _bridge_probe_quick():
            raise RuntimeError("bridge unreachable / no ST extension")
        # Bridge ready - stream
        async for chunk in _stream_chunks(user_text, history_override):
            full += chunk
            now = time.time()
            if now - last_edit > EDIT_INTERVAL_S:
                try:
                    await placeholder.edit_text(_truncate_for_placeholder(full))
                    last_edit = now
                except Exception:
                    pass
        used_path = "bridge"
        log.info("bridge_path_ok full_len=%d", len(full))
    except Exception as exc:
        log.warning("bridge_path_failed exc=%s, falling back to L1 ollama", exc)
        try:
            card = _load_card_if_needed()
            if card is None:
                await placeholder.edit_text(
                    f"❌ MUSHOKU bot 暂时不可用 (bridge 503 + card 未加载): {exc}"
                )
                return
            full = await ollama_fallback.ollama_direct_reply(card, user_text)
            used_path = "L1"
            log.info("L1_path_ok full_len=%d", len(full))
            try:
                await placeholder.edit_text(_truncate_for_placeholder(full))
            except Exception:
                pass
        except Exception as exc2:
            log.warning("L1_path_failed exc=%s", exc2)
            await placeholder.edit_text(
                f"❌ MUSHOKU bot 暂时不可用: {exc} / {exc2}"
            )
            return

    final_text = full.strip()
    if not final_text:
        log.warning("empty final_text path=%s chat_id=%s", used_path, chat_id)
        await placeholder.edit_text(
            f"❌ {used_path or 'bridge'} 路径生成内容为空, 请重试"
        )
        return
    final_hash = hashlib.md5(final_text.encode("utf-8")).hexdigest()[:12]

    if _send_dedup_check(chat_id, final_text):
        log.info("send_duplicate_suppressed chat_id=%s final_hash=%s", chat_id, final_hash)
        return

    log.info("send_long_message_call chat_id=%s update_id=%s final_hash=%s final_len=%d path=%s",
             chat_id, update_id, final_hash, len(final_text), used_path)
    try:
        if TS is not None:
            _diag = await TS.send_long_message(update.message, final_text,
                                               first_message=placeholder)
            log.info("SEND_DIAG input_len=%d segments=%d lengths=%s results=%s exceptions=%s final_hash=%s",
                     _diag["input_len"], _diag["segments"], _diag["lengths"],
                     _diag["results"], _diag["exceptions"][:4], final_hash)
        else:
            await placeholder.edit_text(final_text[:4096])
            log.info("SEND_FALLBACK single edit final_len=%d final_hash=%s", len(final_text), final_hash)
    except Exception as exc:
        log.warning("SEND_ERROR exc=%s final_len=%d final_hash=%s", exc, len(final_text), final_hash)
        return

    if session is not None and turn_no is not None:
        record_turn(DB_PATH, session, "assistant", turn_no, full,
                    {"source": "mushoku_bridge_or_L1", "model": "qwen3.6",
                     "hash": final_hash, "path": used_path})
    log.info("chat_id=%s reply len=%d hash=%s path=%s", chat_id, len(full), final_hash, used_path)


async def _stream_chunks(user_text: str, history: list[dict] | None = None):
    """Async generator yielding content chunks from the bridge."""
    messages = []
    if MUSHOKU_LANGUAGE_OVERRIDE:
        messages.append({"role": "system", "content": MUSHOKU_LANGUAGE_OVERRIDE})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": "tavern-v2",
        "messages": messages,
        "stream": True,
        "temperature": 0.85,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OPENAI_BASE_URL + "/chat/completions",
                                json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.content:
                line = line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    chunk = delta.get("content", "")
                    if chunk:
                        yield chunk
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

# ============================================================================
# Command handlers (7 commands - 剧情 RP 简化版)
# ============================================================================
HELP_TEXT = """📜 **MUSHOKU Bot** - 无职转生剧情 RP Bot

**命令列表**
/start - 首次进入, 显示帮助
/help - 本帮助
/newgame - 开新剧情 (归档旧 session, 创建新 session + ST 开场白)
/continue - 继续当前 session 剧情
/status - 当前 session 状态
/export_raw - 导出 raw_log.md (剧情回看)
/novel - 导出小说化 markdown (程序化, 零 LLM, 零 token)
/endgame - 结束当前 session (标记 completed, 保留存档)

**自由文本**: 直接发剧情行动 → Bot 转发给 SillyTavern → MUSHOKU 流式回复

**角色卡**: Boku no Isekai Jobless Reincarnation (46-entry character_book)
**世界书**: Mushoku Tensei World Reference (46 entries auto-activated)
**输出**: 简体中文 + 日文人名保留原拼写 (ルーデウス / エルメス / ヒルダ / ...)

**Bot 隔离**: MUSHOKU 是独立 ST 实例 (:8015) + 独立 bridge (:8017/:8016),
不影响其他 bot stack。

**稳定性 (Stage 6)**: 当 ST bridge 不通时, bot 自动 fallback 到 V2 Ollama proxy
直连,确保 100% 收到中文回复 (无 ST WS race 卡死)。
"""

async def cmd_start(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _update_dedup_check(update.effective_chat.id, update.update_id):
        await update.message.reply_text(
            "🎲 MUSHOKU Bot 已上线。\n\n"
            "当前角色: Boku no Isekai Jobless Reincarnation\n"
            "世界书: 46 entries 自动激活\n\n"
            "直接发 /newgame 开新剧情, 或 /help 看完整命令。",
            parse_mode=None,
        )

async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

async def cmd_newgame(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("cmd_newgame received chat_id=%s update_id=%s message_id=%s", update.effective_chat.id if update.effective_chat else None, update.update_id, update.effective_message.message_id if update.effective_message else None)
    chat_id = update.effective_chat.id
    if _update_dedup_check(chat_id, update.update_id):
        return
    archived = archive_active_sessions(DB_PATH, chat_id)
    session = create_session(DB_PATH, chat_id, title="Mushoku Tensei - new story")
    head = (f"🎲 旧局已归档（{archived} 局），新剧情开始！\n\n"
            if archived else "🎲 新剧情开始！\n\n")
    await update.message.reply_text(
        head + "角色卡: Boku no Isekai Jobless Reincarnation\n"
        "世界书: Mushoku Tensei World Reference (46 entries)\n"
        "语言: 简体中文 + 日文人名保留原拼写\n\n"
        "正在加载 ST 开场白...",
    )
    # Trigger first ST generation (use first_mes from card)
    # IMPORTANT: 中文开场提示,不要引导 LLM 用 first_mes (英文) 开始。
    # first_mes 若是英文,会让 LLM 顺势复读英文;改成纯中文指示确保 100% 中文输出。
    await _generate_and_reply(
        update,
        user_text="[NEWGAME: 玩家开始新剧情。请以简体中文完成开场白 —— 角色视角第一人称描写当前场景,日文人名/剑术/大陆名保留原拼写(用「」包裹),正文严禁英文。描述包含:角色当前处境、周围环境、所处地点、季节/时辰。如有同伴可引入在场 NPC。直接开场,不要 meta 注释。]",
        session=session,
        log_player_text="[NEWGAME]",
        skip_update_dedup=True,
    )

async def cmd_continue(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = get_or_create_active_session(DB_PATH, chat_id)
    if not session:
        await update.message.reply_text("📭 没有进行中的 session。用 /newgame 开新局。")
        return
    # 中文继续提示,跟 NEWGAME 同样策略:不引导 first_mes,纯中文指示
    await _generate_and_reply(
        update,
        user_text="[CONTINUE: 玩家请求继续推进剧情。请以简体中文续写 —— 角色视角第一人称,延续上一段场景,正文严禁英文。日文人名/专有术语保留原拼写(「」包裹)。]",
        session=session,
        log_player_text="[CONTINUE]",
    )

async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    s = get_active_session(DB_PATH, chat_id)
    if not s:
        await update.message.reply_text("📭 当前没有进行中的 session。用 /newgame 开始新剧情。")
        return
    turns = get_turns(DB_PATH, s["session_id"])
    n_turns = len(turns) // 2  # player + assistant = 1 turn
    await update.message.reply_text(
        f"📊 当前 Session\n"
        f"  ID: {s['session_id']}\n"
        f"  Title: {s['title']}\n"
        f"  Status: {s['status']}\n"
        f"  Started: {s['created_at']}\n"
        f"  Turns: {n_turns}\n"
        f"  Character: {s['character']}\n"
        f"  World Info: {s['world_info']} (46 entries)\n"
        f"  Log: {s['log_path']}\n",
    )

async def cmd_export_raw(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    s = get_active_session(DB_PATH, chat_id)
    if not s:
        await update.message.reply_text("📭 没有当前 session。用 /newgame 开始。")
        return
    log_path = s.get("log_path") or os.path.join(_session_dir(s["session_id"]), "raw_log.md")
    if not os.path.exists(log_path):
        await update.message.reply_text(f"📭 raw_log.md 不存在: {log_path}")
        return
    n_turns = len(get_turns(DB_PATH, s["session_id"]))
    try:
        await update.message.reply_document(
            document=open(log_path, "rb"),
            filename=f"raw_log_{s['session_id'][:8]}.md",
            caption=f"📄 {s['title']} ({n_turns} turns)",
        )
    except Exception as exc:
        log.warning("export_raw failed: %s", exc)
        await update.message.reply_text(f"❌ Export failed: {exc}")

async def cmd_novel(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Export current session as a novelized markdown file (Chinese first-person narrative)."""
    chat_id = update.effective_chat.id
    s = get_active_session(DB_PATH, chat_id)
    if not s:
        await update.message.reply_text("📭 没有当前 session。用 /newgame 开始。")
        return
    try:
        novel_path = _build_novel(s)
    except Exception as exc:
        log.warning("build_novel failed: %s", exc)
        await update.message.reply_text(f"❌ Build novel failed: {exc}")
        return
    n_turns = len(get_turns(DB_PATH, s["session_id"]))
    try:
        await update.message.reply_document(
            document=open(novel_path, "rb"),
            filename=os.path.basename(novel_path),
            caption=f"📖 小说化导出 - {s['title']} ({n_turns} turns / {os.path.getsize(novel_path)} bytes)",
        )
    except Exception as exc:
        log.warning("send_novel failed: %s", exc)
        await update.message.reply_text(f"❌ Send novel failed: {exc}")


def _build_novel(session: dict) -> str:
    """Convert raw_log.md (player + assistant turns) into a novelized markdown file.

    Format:
      - Header (title, character, started/ended timestamps, turn count)
      - For each turn: player input -> 「（玩家）」 prefix; assistant reply -> plain text
      - Light scene break (---) every 10 turns
      - Footer (export timestamp)

    Pure program-generated; no LLM call (fast, deterministic, no token cost).
    """
    sid = session["session_id"]
    turns = get_turns(DB_PATH, sid)
    sd = _session_dir(sid)
    exports_dir = os.path.join(sd, "exports")
    os.makedirs(exports_dir, exist_ok=True)
    novel_path = os.path.join(exports_dir, f"novel_{sid[:8]}.md")

    # Group by turn number
    grouped: dict[int, dict[str, str]] = {}
    for t in turns:
        turn_no = t["turn"]
        grouped.setdefault(turn_no, {})[t["speaker"]] = t["text"]

    lines: list[str] = []
    # Header
    lines.append(f"# {session['title']}")
    lines.append("")
    lines.append(f"**角色**: {session['character']}")
    lines.append(f"**起始**: {session['created_at']}")
    lines.append(f"**结束**: {session.get('ended_at') or '(进行中)'}")
    lines.append(f"**回合数**: {len(grouped)}")
    lines.append(f"**Session ID**: `{sid}`")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Body
    sorted_turns = sorted(grouped.items())
    for i, (turn_no, speakers) in enumerate(sorted_turns, start=1):
        player_text = (speakers.get("player") or "").strip()
        assistant_text = (speakers.get("assistant") or "").strip()

        # Player input as a marginalia blockquote (Chinese novel convention: 「」 quotes)
        if player_text:
            # Strip leading "[NEWGAME]" / "[CONTINUE]" markers
            clean = re.sub(r'^\[(NEWGAME|CONTINUE)\]\s*', '', player_text)
            # Wrap player action as italicized prefixed line
            lines.append(f"*「{clean}」*")
            lines.append("")

        # Strip meta / OOC leakage from assistant reply (LLM drift defense).
        # LANGUAGE_OVERRIDE bans these but LLM still occasionally emits them.
        if assistant_text:
            # Remove common meta annotations
            assistant_text = re.sub(
                r'\(OOC:[^)]*\)|\(Internal:[^)]*\)|\(注：[^)]*\)|'
                r'^##\s*OOC.*$|^\s*<!--.*?-->\s*$|'
                r'<think>.*?</think>|<think>.*?$',
                '',
                assistant_text,
                flags=re.MULTILINE | re.DOTALL,
            ).strip()
            if assistant_text:
                lines.append(assistant_text)
                lines.append("")

        # Light scene break every 10 turns
        if i % 10 == 0 and i != len(sorted_turns):
            lines.append("")
            lines.append("---")
            lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append(f"*导出时间*: {_now_iso()}")
    lines.append("")
    lines.append(f"*导出方式*: 程序化(纯规则,无 LLM 调用,零 token 成本)*")

    content = "\n".join(lines)
    with open(novel_path, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("novel exported: %s (%d bytes, %d turns)", novel_path, len(content), len(grouped))
    return novel_path


async def cmd_endgame(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    s = get_active_session(DB_PATH, chat_id)
    if not s:
        await update.message.reply_text("📭 没有进行中的 session。")
        return
    set_session_status(DB_PATH, s["session_id"], "completed")
    meta_path = os.path.join(_session_dir(s["session_id"]), "metadata.json")
    if os.path.exists(meta_path):
        try:
            meta = json.loads(open(meta_path, encoding="utf-8").read())
            meta["status"] = "completed"
            meta["ended_at"] = _now_iso()
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            log.warning("endgame metadata update failed: %s", exc)
    await update.message.reply_text(
        f"✅ Session 已结束。\n\n"
        f"  ID: {s['session_id']}\n"
        f"  Title: {s['title']}\n"
        f"  Turns: {len(get_turns(DB_PATH, s['session_id'])) // 2}\n\n"
        f"📦 存档保留 (raw_log.md + metadata.json 在 sessions/{s['session_id']}/)\n"
        f"新剧情: /newgame",
    )

# ============================================================================
# Free-text chat handler (任何非命令文本 → ST 生成)
# ============================================================================
async def chat_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    session = get_or_create_active_session(DB_PATH, chat_id)
    user_text = update.message.text.strip()
    if not user_text:
        return
    await _generate_and_reply(update, user_text=user_text, session=session)

# ============================================================================
# main() + run_polling
# ============================================================================
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env and fill it.")
        sys.exit(1)
    init_db(DB_PATH)
    os.makedirs(_abs(SESSIONS_DIR), exist_ok=True)
    log.info("DB at %s", _abs(DB_PATH))
    log.info("sessions dir at %s", _abs(SESSIONS_DIR))
    log.info("bridge at %s", OPENAI_BASE_URL)
    log.info("character: %s", DEFAULT_CHARACTER)
    log.info("language override: 剧情向 (日文人名保留原拼写)")
    log.info("L1 fallback: %s (card_parser=%s)", "enabled" if _HAS_L1 else "DISABLED",
             "ok" if _HAS_L1 else "MISSING")
    # Pre-load card at startup so first message can fall back instantly
    card = _load_card_if_needed()
    log.info("card_ctx preloaded: %s", "OK" if card else "FAILED (L1 unavailable)")
    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    for name, fn in (
        ("start", cmd_start), ("help", cmd_help),
        ("newgame", cmd_newgame), ("continue", cmd_continue),
        ("status", cmd_status), ("export_raw", cmd_export_raw),
        ("novel", cmd_novel),
        ("endgame", cmd_endgame),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("MUSHOKU bot starting (narrative RP, no RPG engine).")
    app.run_polling()

if __name__ == "__main__":
    main()
