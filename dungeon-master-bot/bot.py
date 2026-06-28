#!/usr/bin/env python3
"""
Dungeon Master Bot — Telegram RPG Game Master
(Phase 1: Session Logging + Phase 2: Game State Engine / Single Source of Truth).

Fourth Telegram bot, FULLY ISOLATED from Penelope / June / Aqua.
Talks to the DM-dedicated bridge (:8013) which fronts an isolated
SillyTavern instance whose active_character = DungeonMaster12
(DnD-Base lorebook, 68 entries, auto-activated).

Phase 1 (this build) — GAME SESSION LOGGING + EXPORT:
  - Each game is an independent Session (SQLite game_sessions / game_turns
    + a per-session directory under sessions/<session_id>/).
  - Every player input and DM reply is recorded to BOTH SQLite and
    raw_log.md (never lose a turn).
  - Commands: /newgame /session /sessions /export_raw /export_script
    /export_notes /endgame (+ legacy /continue /save /load /status
    /inventory /quest /map /party /help).
  - Free text and the /newgame opening flow through the bridge with a DM
    Chinese Language Override; cards / world stay English, untouched.

NOT in this phase: complex battle, full inventory, complex character
system, AI novel polishing. ST core / DungeonMaster12 card / World Info
are never modified. sessions/ is gitignored (no real RP content in git).
"""
import os
import sys
import re
import json
import uuid
import asyncio
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

import aiohttp
from telegram import Update
from telegram.ext import (Application, ApplicationBuilder, CommandHandler,
                          ContextTypes, MessageHandler, filters)

# ---- config -----------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8013/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "tavern-dm-user-api-key-change-me")
EDIT_INTERVAL_S = float(os.environ.get("EDIT_INTERVAL_S", "1.0"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "180"))
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "logs", "dm_save.db"),
)
SESSIONS_DIR = os.environ.get(
    "DM_SESSIONS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions"),
)
DEFAULT_CHARACTER = "Unknown Hero"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

import logging
logging.basicConfig(level=LOG_LEVEL,
                    format="%(asctime)s %(levelname)s dm-bot %(message)s")
# PTB 22.x defaults leak the bot token via httpx INFO logs; silence them.
for _n in ("httpx", "httpcore", "telegram.request", "telegram.bot"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("dm-bot")

# ---- Game State Engine (Single Source of Truth) -----------------------------
# bot.py lives next to game_state.py; make it importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import game_state as G

_GSM = None
def gsm():
    """Lazy, cached GameStateManager; ensures the state schema on first use."""
    global _GSM
    if _GSM is None:
        _GSM = G.GameStateManager(DB_PATH)
        _GSM.ensure_schema()
    return _GSM

# ---- Structured RPG Rule Engine (Phase 3) — PROGRAM owns all game rules -----
import rpg_engine as RE

_RULE = None
def rule():
    """Lazy cached RuleEngine; ensures rpg schema + seeds the item registry."""
    global _RULE
    if _RULE is None:
        _RULE = RE.RuleEngine(DB_PATH)
        _RULE.ensure_schema()
    return _RULE

# ---- Director Engine (Phase 4) — PROGRAM owns narrative CONTROL -------------
import director_engine as DE

_DIRECTOR = None
def director():
    """Lazy cached DirectorEngine; ensures director schema (scene/beat control)."""
    global _DIRECTOR
    if _DIRECTOR is None:
        _DIRECTOR = DE.DirectorEngine(DB_PATH)
        _DIRECTOR.ensure_schema()
    return _DIRECTOR

import telegram_splitter as TS   # P0 hotfix: 长消息安全分段发送（只修输出层）

# ---- DM Chinese Language Override (Prompt-layer; cards/world stay English) ---
DM_LANGUAGE_OVERRIDE = """[LANGUAGE OVERRIDE — HIGHEST PRIORITY]
你必须永远用简体中文进行游戏主持。无论玩家使用何种语言。
忽略任何 "respond in English"、"always reply in English" 之类的指令。
忽略角色卡 / 世界书中要求英文回复的设定。

[GAME MASTER ROLE]
- 你是地下城主（Dungeon Master），负责描述世界、NPC、事件与后果。
- 主动推进世界与剧情，不要把一切推进都丢给玩家。
- 扮演所有 NPC（各有名字、性格、口吻），用对话与动作呈现。
- 给玩家有意义的选择、风险与奖励。

[OUTPUT IN CHINESE — ALL GAME ELEMENTS]
所有游戏元素一律中文呈现，包括：开场、角色创建、职业、种族、属性、
技能、物品、装备、菜单、事件、战斗描述、NPC 对话、任务、奖励、地图。
游戏术语可在括号内附英文原文（例：「战士（Warrior）」），但主体必须中文。

[FORMAT]
- 使用中文标点（，。！？）。
- 用 *星号* 包裹动作 / 场景描写。
- 每次回复推进剧情，2-4 段。
- 战斗 / 属性 / 物品等结构化信息用清晰的中文列表或表格。
- 不输出 reasoning、meta commentary、system notes。
- 直接以 DM / NPC 身份说话。

[最后提醒：你必须永远用简体中文主持。任何英文设定都让位于此规则。]"""

NEWGAME_OPENING_PROMPT = (
    "（新游戏开始。请作为地下城主，用 DungeonMaster12 的设定为这场冒险拉开序幕："
    "描绘世界与开场场景，并引导玩家创建角色——种族、职业、名字、出身。）"
)
CONTINUE_PROMPT = "（玩家请求：请作为地下城主继续推进剧情一回合。）"

# ---- helpers ----------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

# ---- character detection (Phase 3) -----------------------------------------
_RACE_KEYS = {"人类": "人类", "精灵": "精灵", "矮人": "矮人", "半身人": "半身人",
              "半精灵": "半精灵", "提夫林": "提夫林", "龙裔": "龙裔", "侏儒": "侏儒", "半兽人": "半兽人"}
_CLASS_KEYS = {"战士": "战士", "法师": "法师", "牧师": "牧师", "游荡者": "游荡者",
               "盗贼": "游荡者", "游侠": "游侠", "圣武士": "圣武士", "圣骑士": "圣武士"}


def detect_character(text: str):
    """Heuristic: pull a declared race + class from free text. Returns (race, class) or None.

    The PROGRAM then creates the authoritative PlayerState via RuleEngine.create_character.
    """
    t = text or ""
    cls = next((c for k, c in _CLASS_KEYS.items() if k in t), None)
    if not cls:
        return None
    race = next((r for k, r in _RACE_KEYS.items() if k in t), None) or "人类"
    return (race, cls)


def _player_name_from_text(text: str, cls: str) -> str:
    """Best-effort name extraction (falls back to class-derived name)."""
    t = text or ""
    pats = (r'叫[做]?\s*[「『“"]?\s*([一-龥A-Za-z]{2,6})',
            r'名字[是为]?\s*([一-龥A-Za-z]{2,6})')
    for pat in pats:
        m = re.search(pat, t)
        if m:
            return m.group(1)
    return {"战士": "勇者", "法师": "秘法者", "牧师": "祝祷者",
            "游荡者": "影刃", "游侠": "巡林客", "圣武士": "誓卫"}.get(cls, "冒险者")

def _bot_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))

def _session_dir(session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, session_id)

def _abs(rel_or_abs: str) -> str:
    return rel_or_abs if os.path.isabs(rel_or_abs) else os.path.join(_bot_dir(), rel_or_abs)

def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

_CN_DIGITS = "零一二三四五六七八九"
def _cn_int(n: int) -> str:
    """1..99 -> 中文（用于「第N幕」）；超出范围回退阿拉伯数字。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_DIGITS[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    return str(n)

# ---- SQLite schema (existing tables KEPT; new game_sessions / game_turns) ---
def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS saves(
          chat_id INTEGER PRIMARY KEY,
          active_character_zh TEXT,
          created_at TEXT, updated_at TEXT,
          turn_count INTEGER DEFAULT 0,
          meta_json TEXT
        );
        CREATE TABLE IF NOT EXISTS players(
          chat_id INTEGER PRIMARY KEY,
          name_zh TEXT, race_zh TEXT, class_zh TEXT,
          race_en TEXT, class_en TEXT,
          level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0,
          str INTEGER, dex INTEGER, con INTEGER,
          int_ INTEGER, wis INTEGER, cha INTEGER,
          hp INTEGER, max_hp INTEGER, gold INTEGER DEFAULT 0,
          location_zh TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS inventory(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER, item_zh TEXT, item_en TEXT,
          qty INTEGER DEFAULT 1, equipped INTEGER DEFAULT 0, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS npcs(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER, name_zh TEXT, name_en TEXT,
          role TEXT, faction TEXT, disposition TEXT, first_met_at TEXT, notes TEXT
        );
        CREATE TABLE IF NOT EXISTS quests(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER, title_zh TEXT, giver_npc TEXT, status TEXT,
          objective_zh TEXT, reward_zh TEXT, accepted_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS term_map(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER, en TEXT, zh TEXT
        );
        CREATE TABLE IF NOT EXISTS game_sessions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT UNIQUE NOT NULL,
          chat_id INTEGER NOT NULL,
          title TEXT,
          player_name TEXT,
          character_name TEXT,
          status TEXT DEFAULT 'active',
          summary TEXT DEFAULT '',
          log_path TEXT,
          export_path TEXT,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS game_turns(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          session_id TEXT NOT NULL,
          turn_number INTEGER NOT NULL,
          speaker TEXT NOT NULL,
          raw_text TEXT,
          cleaned_text TEXT,
          metadata_json TEXT,
          created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_turns_session
          ON game_turns(session_id, turn_number);
        CREATE INDEX IF NOT EXISTS idx_sessions_chat
          ON game_sessions(chat_id, status);
        """)

# ---- session manager --------------------------------------------------------
def archive_active_sessions(path: str, chat_id: int) -> int:
    """status='active' -> 'archived' for all of this chat's sessions. Returns count."""
    with closing(sqlite3.connect(path)) as db:
        cur = db.execute(
            "UPDATE game_sessions SET status='archived', updated_at=? "
            "WHERE chat_id=? AND status='active'",
            (_now_iso(), chat_id))
        db.commit()
        return cur.rowcount

def create_session(path: str, chat_id: int, title: str = "新冒险",
                   player_name: str = "玩家",
                   character_name: str = DEFAULT_CHARACTER) -> dict:
    """Archive existing active session(s), then create a new active session."""
    archive_active_sessions(path, chat_id)
    session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    now = _now_iso()
    sdir = _session_dir(session_id)
    exports_dir = os.path.join(sdir, "exports")
    os.makedirs(exports_dir, exist_ok=True)
    rel_log = os.path.relpath(os.path.join(sdir, "raw_log.md"), _bot_dir())
    rel_export = os.path.relpath(exports_dir, _bot_dir())
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO game_sessions(session_id, chat_id, title, player_name, "
            "character_name, status, summary, log_path, export_path, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, chat_id, title, player_name, character_name,
             "active", "", rel_log, rel_export, now, now))
        db.commit()
    meta = {
        "session_id": session_id, "title": title, "telegram_chat_id": chat_id,
        "player_name": player_name, "character_name": character_name,
        "status": "active", "created_at": now, "updated_at": now, "summary": "",
        "log_path": rel_log, "export_path": rel_export,
    }
    _write_json(os.path.join(sdir, "metadata.json"), meta)
    _write_json(os.path.join(sdir, "state.json"), {
        "session_id": session_id, "turn_count": 0,
        "note": "placeholder — structured player state populated in DM-Phase 2",
    })
    log.info("session created id=%s chat=%s char=%s", session_id, chat_id, character_name)
    return meta

def get_active_session(path: str, chat_id: int):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT * FROM game_sessions WHERE chat_id=? AND status='active' "
            "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        return dict(row) if row else None

def get_or_create_active_session(path: str, chat_id: int) -> dict:
    s = get_active_session(path, chat_id)
    return s if s else create_session(path, chat_id)

def set_session_status(path: str, session_id: str, status: str) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE game_sessions SET status=?, updated_at=? WHERE session_id=?",
                   (status, _now_iso(), session_id))
        db.commit()

def touch_session(path: str, session_id: str) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE game_sessions SET updated_at=? WHERE session_id=?",
                   (_now_iso(), session_id))
        db.commit()

def next_turn_number(path: str, session_id: str) -> int:
    with closing(sqlite3.connect(path)) as db:
        r = db.execute(
            "SELECT COALESCE(MAX(turn_number),0)+1 FROM game_turns WHERE session_id=?",
            (session_id,)).fetchone()
        return r[0]

def record_turn(path: str, session: dict, speaker: str, turn: int,
                raw_text: str, metadata: dict) -> None:
    """Write one turn to SQLite AND append to raw_log.md."""
    cleaned = (raw_text or "").strip()
    now = _now_iso()
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO game_turns(session_id, turn_number, speaker, raw_text, "
            "cleaned_text, metadata_json, created_at) VALUES(?,?,?,?,?,?,?)",
            (session["session_id"], turn, speaker, raw_text or "", cleaned,
             json.dumps(metadata, ensure_ascii=False), now))
        db.commit()
    _append_turn_md(session, speaker, turn, raw_text or "")

def get_turns(path: str, session_id: str):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM game_turns WHERE session_id=? ORDER BY turn_number, id",
            (session_id,)).fetchall()
        return [dict(r) for r in rows]

def list_sessions(path: str, chat_id: int, limit: int = 10):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT s.session_id, s.title, s.character_name, s.status, s.created_at, "
            "       (SELECT COUNT(*) FROM game_turns t WHERE t.session_id=s.session_id) AS turn_count "
            "FROM game_sessions s WHERE s.chat_id=? ORDER BY s.id DESC LIMIT ?",
            (chat_id, limit)).fetchall()
        return [dict(r) for r in rows]

# ---- markdown writers -------------------------------------------------------
def _md_header(session: dict) -> str:
    return (
        f"# 游戏记录：{session.get('title') or '未命名冒险'}\n\n"
        f"- Session ID：`{session.get('session_id')}`\n"
        f"- 创建时间：{session.get('created_at', '')}\n"
        f"- 角色：{session.get('character_name', DEFAULT_CHARACTER)}\n"
        f"- 玩家：{session.get('player_name', '玩家')}\n"
        f"- 状态：{session.get('status', 'active')}\n\n---\n"
    )

def _append_turn_md(session: dict, speaker: str, turn: int, text: str) -> None:
    sdir = _session_dir(session["session_id"])
    os.makedirs(sdir, exist_ok=True)
    path = os.path.join(sdir, "raw_log.md")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(_md_header(session))
    with open(path, "a", encoding="utf-8") as f:
        if speaker == "player":
            f.write(f"\n## 第 {turn} 回合\n\n**玩家**\n\n{text}\n")
        elif speaker == "dungeon_master":
            f.write(f"\n**地下城主**\n\n{text}\n\n---\n")
        else:
            f.write(f"\n## 第 {turn} 回合\n\n**系统**\n\n{text}\n")

def _append_state_md(session: dict, turn: int, before_compact: str,
                     after_compact: str, changes: list, conflicts: list,
                     rpg_before: str = "", rpg_after: str = "",
                     rule_changes: list = None, director_changes: list = None) -> None:
    """Append the per-turn World + Rule state block to raw_log.md.

    Phase 9 (world) + Phase 12 (rule changes): Before / Dialogue / After /
    Timeline (narrative changes + rule changes + conflicts).
    """
    sdir = _session_dir(session["session_id"])
    path = os.path.join(sdir, "raw_log.md")
    if not os.path.exists(path):
        return  # raw_log is created by record_turn; nothing to attach to yet
    chg = "；".join(f"{c.get('entity','?')}：{c.get('change','?')}" for c in (changes or [])) or "（无）"
    rch = "；".join(f"{c.get('entity','?')}：{c.get('change','?')}" for c in (rule_changes or [])) or "（无）"
    cnf = "；".join(f"{c.get('category','?')}：{c.get('entity','?')}" for c in (conflicts or [])) or "（无）"
    dch = "；".join(f"{c.get('entity','?')}：{c.get('change','?')}" for c in (director_changes or [])) or "（无）"
    block = (
        f"\n> **【第 {turn} 回合 · 世界 + 规则 + 导演状态】**\n"
        f"> - 世界 Before：{before_compact or '（空）'}\n"
        f"> - 规则 Before：{rpg_before or '（空）'}\n"
        f"> - 叙事变更：{chg}\n"
        f"> - 规则变更（程序裁定）：{rch}\n"
        f"> - 导演变更（场景/节拍/转换）：{dch}\n"
        f"> - 世界 After：{after_compact or '（空）'}\n"
        f"> - 规则 After：{rpg_after or '（空）'}\n"
        f"> - 冲突：{cnf}\n"
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(block)

def _group_turns_by_number(turns):
    by_turn = {}
    for t in turns:
        by_turn.setdefault(t["turn_number"], {})[t["speaker"]] = \
            t["cleaned_text"] or t["raw_text"] or ""
    return by_turn

def _build_script(session: dict) -> str:
    """Simple raw_log -> script conversion (no AI polishing)."""
    by_turn = _group_turns_by_number(get_turns(DB_PATH, session["session_id"]))
    out = [f"# 剧本：{session.get('title') or '未命名冒险'}\n",
           "> 由 raw_log 自动转换的基础剧本格式（未润色）。DM 的场景描写与对白统一归入「地下城主」。\n"]
    for turn_no in sorted(by_turn):
        grp = by_turn[turn_no]
        out.append(f"\n## 第{_cn_int(turn_no)}幕\n")
        if "player" in grp:
            out.append(f"\n**玩家**：\n{grp['player']}\n")
        if "dungeon_master" in grp:
            out.append(f"\n**地下城主**：\n{grp['dungeon_master']}\n")
        elif "system" in grp:
            out.append(f"\n**旁白**：\n{grp['system']}\n")
        out.append("\n---\n")
    return "\n".join(out)

def _build_notes(session: dict) -> str:
    """Simple extraction from turns into novel-source material (placeholders OK)."""
    by_turn = _group_turns_by_number(get_turns(DB_PATH, session["session_id"]))
    player_choices = [by_turn[k]["player"] for k in sorted(by_turn) if "player" in by_turn[k]]
    first_dm = next((by_turn[k]["dungeon_master"] for k in sorted(by_turn)
                     if "dungeon_master" in by_turn[k]), "")
    lines = [f"# 小说素材：{session.get('title') or '未命名冒险'}\n",
             f"- Session ID：`{session.get('session_id')}`\n",
             "\n## 主要人物\n",
             f"- 主角：{session.get('character_name', DEFAULT_CHARACTER)}（玩家）",
             "\n## 地点\n- （待从日志细化；首幕场景见「关键事件」）",
             "\n## 关键事件\n",
             "- 第 1 回合：" + ((first_dm[:160] + "…") if len(first_dm) > 160 else (first_dm or "（尚无）")),
             "\n## 玩家选择\n"]
    if player_choices:
        for i, c in enumerate(player_choices, 1):
            lines.append(f"- 第{i}次：" + (c[:120] + "…" if len(c) > 120 else c))
    else:
        lines.append("- （尚无）")
    for section in ("未解决伏笔", "战斗", "道具", "NPC"):
        lines.append(f"\n## {section}\n- （本阶段不自动记录，待 DM-Phase 2 / AI 润色阶段补充）")
    lines.append("\n## 下一章可能发展\n- （待 AI 润色阶段根据日志生成）")
    return "\n".join(lines)

# ---- placeholder for not-yet-implemented commands ---------------------------
def _placeholder(name: str) -> str:
    return f"⏳ /{name} — 规划项，将在 DM-Phase 2 落地（见 COMMANDS.md）。"

# ---- command handlers -------------------------------------------------------
async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎲 地下城主（Dungeon Master）— 中文文字冒险\n\n"
        "🎮 游戏局：\n"
        "/newgame — 开始新一局（旧局自动归档）\n"
        "/session — 查看当前局信息\n"
        "/sessions — 列出最近游戏局\n"
        "/endgame — 结束当前局（归档）\n\n"
        "📤 导出（每轮自动记录，可润色成小说 / 剧本 / 跑团 Replay）：\n"
        "/export_raw — 原始日志 raw_log.md\n"
        "/export_script — 剧本格式 script_log.md\n"
        "/export_notes — 小说素材 novel_notes.md\n\n"
        "📝 其他：\n"
        "/continue — 让地下城主推进一回合\n"
        "/status /inventory /quest /map /party — 规划中\n"
        "/help — 帮助\n\n"
        "或直接发文字，进行你的冒险行动（每轮自动记录）。"
    )

async def cmd_newgame(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    archived = archive_active_sessions(DB_PATH, chat_id)
    session = create_session(DB_PATH, chat_id, title="新冒险",
                             character_name=DEFAULT_CHARACTER)
    gsm().init_state(session["session_id"])  # fresh world state (Phase 2)
    rule().init_state(session["session_id"])  # fresh RPG rule state (Phase 3)
    head = (f"🎲 旧局已归档（{archived} 局），新一局冒险开始！\n"
            if archived else "🎲 新一局冒险开始！\n")
    await update.message.reply_text(
        head + f"角色：{DEFAULT_CHARACTER}（暂定，后续可改）\n正在召唤地下城主开场……"
    )
    await _generate_and_reply(update, NEWGAME_OPENING_PROMPT, session=session,
                              log_player_text="（玩家发起：新游戏）")

async def cmd_continue(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_or_create_active_session(DB_PATH, update.effective_chat.id)
    await _generate_and_reply(update, CONTINUE_PROMPT, session=session,
                              log_player_text="（玩家请求：继续推进剧情）")

async def cmd_session(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 当前没有进行中的游戏局。用 /newgame 开始新一局。")
        return
    turns = get_turns(DB_PATH, s["session_id"])
    await update.message.reply_text(
        "🎲 当前游戏局\n"
        f"标题：{s.get('title')}\n"
        f"Session ID：{s.get('session_id')}\n"
        f"角色：{s.get('character_name')}\n"
        f"状态：{s.get('status')}\n"
        f"回合数：{len(turns)}\n"
        f"创建：{s.get('created_at')}\n"
        f"更新：{s.get('updated_at')}\n"
        f"日志：{s.get('log_path', '')}"
    )

async def cmd_sessions(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    rows = list_sessions(DB_PATH, update.effective_chat.id, limit=10)
    if not rows:
        await update.message.reply_text("📭 还没有任何游戏局。用 /newgame 开始第一局。")
        return
    status_zh = {"active": "进行中", "archived": "已归档", "completed": "已结束"}
    lines = ["📜 最近游戏局："]
    for i, r in enumerate(rows, 1):
        st = status_zh.get(r["status"], r["status"])
        lines.append(f"{i}. [{st}] {r['title']} — {r['character_name']}（{r['turn_count']} 回合）")
    lines.append("\n用 /session 查看当前局，/newgame 新开一局。")
    await update.message.reply_text("\n".join(lines))

async def cmd_export_raw(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
        return
    path = _abs(s.get("log_path", ""))
    await _send_doc(update, path, "raw_log.md",
                    f"📄 原始日志（{s.get('title')}，{len(get_turns(DB_PATH, s['session_id']))} 回合）")

async def cmd_export_script(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
        return
    out = _build_script(s)
    path = os.path.join(_session_dir(s["session_id"]), "script_log.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    await _send_doc(update, path, "script_log.md",
                    f"🎬 剧本版（{s.get('title')}）— 基础转换，未润色")

async def cmd_export_notes(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
        return
    out = _build_notes(s)
    path = os.path.join(_session_dir(s["session_id"]), "novel_notes.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    await _send_doc(update, path, "novel_notes.md",
                    f"📖 小说素材（{s.get('title')}）— 基础提取，待润色")

async def cmd_endgame(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    s = get_active_session(DB_PATH, chat_id)
    if not s:
        await update.message.reply_text("📭 没有进行中的游戏局。")
        return
    set_session_status(DB_PATH, s["session_id"], "completed")
    meta_path = os.path.join(_session_dir(s["session_id"]), "metadata.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        meta["status"] = "completed"
        meta["updated_at"] = _now_iso()
        _write_json(meta_path, meta)
    turns = get_turns(DB_PATH, s["session_id"])
    await update.message.reply_text(
        "🏁 本局已结束并归档（completed）。\n"
        f"标题：{s.get('title')}｜回合数：{len(turns)}\n"
        f"日志：{s.get('log_path', '')}\n"
        "用 /newgame 开始新一局，或 /sessions 查看历史局。"
    )

async def cmd_save(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_placeholder("save"))

async def cmd_load(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_placeholder("load"))

async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
        return
    snap = rule().get_or_init(s["session_id"])
    p = snap.player
    coins = RE.coins_str(int(snap.economy.get("copper", 0)))
    conds = "、".join(p.get("conditions") or []) or "无"
    await update.message.reply_text(
        "📊 角色状态（规则引擎裁定）\n"
        f"姓名：{p.get('name','?')}　{p.get('race','?')}·{p.get('class','?')}\n"
        f"等级：Lv.{int(p.get('level',1))}　经验：{int(p.get('xp',0))}/{int(p.get('xp_to_next',300))}\n"
        f"生命：{int(p.get('hp',0))}/{int(p.get('max_hp',0))}　防御 AC：{int(p.get('ac',0))}\n"
        f"状态：{p.get('status','正常')}　状态效果：{conds}\n"
        f"金币：{coins}"
    )

async def cmd_inventory(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_active_session(DB_PATH, update.effective_chat.id)
    if not s:
        await update.message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
        return
    inv = rule().inventory(s["session_id"])
    if not inv:
        await update.message.reply_text("🎒 背包空空如也。（所有物品均来自注册表，LLM 无法凭空创造）")
        return
    lines = ["🎒 背包（物品注册表内的%d项）：" % len(inv)]
    for r in inv:
        eq = " ✓装备" if int(r.get("equipped", 0)) else ""
        lines.append("- %s×%d（%s）%s" % (r.get("name", "?"), int(r.get("qty", 0)),
                                       r.get("rarity", "?"), eq))
    await update.message.reply_text("\n".join(lines))

async def cmd_quest(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_placeholder("quest"))

async def cmd_map(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_placeholder("map"))

async def cmd_party(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_placeholder("party"))

async def chat_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    session = get_or_create_active_session(DB_PATH, update.effective_chat.id)
    await _generate_and_reply(update, update.message.text, session=session)

async def _send_doc(update: Update, path: str, filename: str, caption: str) -> None:
    if not os.path.exists(path):
        await update.message.reply_text(f"❌ 文件不存在：{path}")
        return
    with open(path, "rb") as f:
        await update.message.reply_document(document=f, filename=filename, caption=caption)

# ---- bridge call with Language Override + per-turn logging ------------------
async def _generate_and_reply(update: Update, user_text: str,
                              session=None, log_player: bool = True,
                              log_player_text: str = None) -> None:
    chat_id = update.effective_chat.id
    placeholder = await update.message.reply_text("🎲 正在演绎……")
    turn_no = None
    state_before_compact = ""
    rpg_before_compact = ""
    st = None
    if session:
        # Phase 2: load the authoritative world state (Single Source of Truth).
        st = gsm().get_or_init(session["session_id"])
        state_before_compact = gsm().render_compact(st)
        # Phase 3: snapshot the RPG rule state; if the player declares a race+class,
        # the PROGRAM (not the LLM) creates the authoritative PlayerState.
        rpg_before_compact = rule().render_compact(session["session_id"])
        cc = detect_character(user_text)
        if cc:
            race, cls = cc
            rule().create_character(session["session_id"],
                                    _player_name_from_text(user_text, cls), race, cls, turn=0)
            rpg_before_compact = rule().render_compact(session["session_id"])
    if session and log_player:
        turn_no = next_turn_number(DB_PATH, session["session_id"])
        record_turn(DB_PATH, session, "player", turn_no,
                    log_player_text if log_player_text is not None else user_text,
                    {"source": "telegram", "chat_id": chat_id})
        touch_session(DB_PATH, session["session_id"])
    # Phase 3 + 7: system prompt = Language Override + CURRENT WORLD STATE (anchor) + STATE GUARD
    sys_content = DM_LANGUAGE_OVERRIDE
    if st is not None:
        # Phase 2 + 3: world state anchor + RPG rule snapshot + both guards.
        sys_content = (DM_LANGUAGE_OVERRIDE + "\n\n"
                       + gsm().render_block(st) + "\n\n"
                       + director().render_director_state(session["session_id"]) + "\n\n"
                       + rule().render_rpg_snapshot(session["session_id"]) + "\n\n"
                       + G.STATE_GUARD + "\n\n" + RE.RPG_GUARD + "\n\n"
                       + DE.DIRECTOR_GUARD)
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}",
               "Content-Type": "application/json"}
    # P0 根因修复：显式提高 max_tokens。原 payload 未设 → ST/ollama 默认上限 ~1500 字，
    # LLM 在上限处戛断半句（turn150 停在「影魔被消灭，化作一团」、turn140 停在「命中！(1d6+」骰子公式未写完）。
    # 提高（非降低，用户仅禁「降低 max_tokens」）让战斗结算/战利品/状态栏/结尾完整生成。
    payload = {
        "model": "dungeon-master",
        "stream": True,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_text},
        ],
    }
    url = OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    full = ""
    done_normally = False  # P0 诊断：bridge 流式是否正常收到 [DONE]（区分生成中断 vs 发送截断）
    last_edit = 0.0
    loop = asyncio.get_event_loop()
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    await placeholder.edit_text(f"❌ bridge {resp.status}: {body[:200]}")
                    return
                async for raw in resp.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        done_normally = True
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        piece = (choices[0].get("delta", {}) or {}).get("content") or ""
                        if piece:
                            full += piece
                            now = loop.time()
                            if now - last_edit >= EDIT_INTERVAL_S:
                                try:
                                    # P0 hotfix: 流式预览截断，避免 edit_text 卡在 4096 上限
                                    preview = (full if len(full) < TS.STREAM_PREVIEW_LIMIT
                                               else full[:TS.STREAM_PREVIEW_LIMIT] + "\n…▌")
                                    await placeholder.edit_text(preview)
                                except Exception:
                                    pass
                                last_edit = now
        # P0 hotfix: 超长回复自动分段发送（第一段复用 placeholder，后续段顺序发送）。
        # 不截断 / 不缩内容；full 完整记录到 DB，分段只影响 Telegram 显示。
        # P0 诊断（无 token）：区分 bridge/生成层截断 vs Telegram 发送层截断。
        log.info("BRIDGE_RAW_REPLY_LEN=%d stream_done=%s chat_id=%s",
                 len(full), done_normally, chat_id)
        _diag = await TS.send_long_message(update.message, full or "（无回复）",
                                           first_message=placeholder)
        log.info("SEND_DIAG input_len=%d segments=%d lengths=%s results=%s exceptions=%s",
                 _diag["input_len"], _diag["segments"], _diag["lengths"],
                 _diag["results"], _diag["exceptions"][:4])
        if session and full and turn_no is not None:
            record_turn(DB_PATH, session, "dungeon_master", turn_no, full,
                        {"source": "dm_bridge", "model": "dungeon-master"})
            # Phase 4/5/6: rule-based state updater + validator + timeline (no AI).
            if st is not None:
                try:
                    # Phase 2: rule-based narrative world-state updater.
                    changes = G.StateUpdater(gsm()).update(st, turn_no, user_text, full)
                    # Phase 3: rule engine turns narrative changes into AUTHORITATIVE rules
                    # (enemy death -> fixed XP + loot drop; player HP; registry pickups).
                    rule_changes = rule().intake(session["session_id"], turn_no, changes, full)
                    # Phase 4: Director controls scene/beat/objective/transition —
                    # turns Phase 2's passive location_jump detection into ACTIVE
                    # control (unauthorized teleports are reverted on world state).
                    director_changes = director().intake(session["session_id"], turn_no,
                                                         changes, rule_changes, full, st)
                    conflicts = G.StateValidator(gsm()).validate(st, turn_no, full)
                    rule_conflicts = rule().validate_reply(session["session_id"], full)
                    conflicts = (conflicts or []) + (rule_conflicts or [])
                    after_compact = gsm().render_compact(st)
                    rpg_after_compact = rule().render_compact(session["session_id"])
                    _append_state_md(session, turn_no, state_before_compact,
                                     after_compact, changes, conflicts,
                                     rpg_before=rpg_before_compact,
                                     rpg_after=rpg_after_compact,
                                     rule_changes=rule_changes,
                                     director_changes=director_changes)
                    log.info("turn=%d narrative=%d rule=%d director=%d conflicts=%d",
                             turn_no, len(changes), len(rule_changes),
                             len(director_changes), len(conflicts))
                except Exception as exc:  # noqa: BLE001
                    log.warning("state/rule engine error: %s", exc)
            touch_session(DB_PATH, session["session_id"])
        log.info("chat_id=%s reply len=%d", chat_id, len(full))
    except aiohttp.ClientConnectorError:
        log.warning("STREAM_ERROR connector full_len=%d stream_done=%s", len(full), done_normally)
        await placeholder.edit_text(
            f"❌ 无法连接 DM bridge（{OPENAI_BASE_URL}）。请确认方案 B 已部署（DM bridge :8013 + DM st-runner）。"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("STREAM_ERROR exc=%s full_len=%d stream_done=%s", exc, len(full), done_normally)
        await placeholder.edit_text(f"❌ 出错了：{exc}")

# ---- main -------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env and fill it.")
        sys.exit(1)
    init_db(DB_PATH)
    gsm()  # ensure Game State Engine schema (world_state / state_flags / state_history)
    rule()  # ensure RPG Rule Engine schema + seed item registry (Phase 3)
    director()  # ensure Director Engine schema (director_state / scene_timeline, Phase 4)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    log.info("DB at %s", DB_PATH)
    log.info("sessions dir at %s", SESSIONS_DIR)
    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    for name, fn in (
        ("help", cmd_help), ("newgame", cmd_newgame), ("continue", cmd_continue),
        ("session", cmd_session), ("sessions", cmd_sessions),
        ("export_raw", cmd_export_raw), ("export_script", cmd_export_script),
        ("export_notes", cmd_export_notes), ("endgame", cmd_endgame),
        ("save", cmd_save), ("load", cmd_load), ("status", cmd_status),
        ("inventory", cmd_inventory), ("quest", cmd_quest), ("map", cmd_map),
        ("party", cmd_party),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("Dungeon Master bot starting (Phase 2: Game State Engine as Single Source of Truth).")
    app.run_polling()

if __name__ == "__main__":
    main()
