#!/usr/bin/env python3
"""
Dungeon Master Bot — Telegram RPG Game Master (skeleton, DM-Phase 0).

Fourth Telegram bot, FULLY ISOLATED from Penelope / June / Aqua.
Talks to the DM-dedicated bridge (:8013 by default) which fronts an
isolated SillyTavern instance whose active_character = DungeonMaster12
(with the DnD-Base lorebook, 68 entries, auto-activated).

This is a SKELETON:
  - routing for all 10 commands is registered;
  - SQLite save schema is initialised (see SAVE_SYSTEM.md / PLAYER_STATE.md);
  - free-text roleplay flows through the bridge with a DM Chinese Language
    Override injected as the system prompt (cards/world stay English);
  - RPG game logic (battle / quest / inventory mutations) is STUBBED and
    tagged TODO(DM-Phase 1/2).

DO NOT run until BOTH are true:
  1. a fourth Telegram Bot Token is set in .env, AND
  2. the DM bridge (:8013/:8011) + DM st-runner (isolated Chromium with
     DungeonMaster12 active) are deployed — ARCHITECTURE.md plan B.
"""
import os
import sys
import json
import asyncio
import sqlite3
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
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

import logging
logging.basicConfig(level=LOG_LEVEL,
                    format="%(asctime)s %(levelname)s dm-bot %(message)s")
# PTB 22.x defaults leak the bot token via httpx INFO logs; silence them.
for _n in ("httpx", "httpcore", "telegram.request", "telegram.bot"):
    logging.getLogger(_n).setLevel(logging.WARNING)
log = logging.getLogger("dm-bot")

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

# ---- SQLite save schema (SAVE_SYSTEM.md / PLAYER_STATE.md) ------------------
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
        """)

def _placeholder(name: str) -> str:
    return f"⏳ /{name} — 规划项，将在 DM-Phase 1/2 落地（见 COMMANDS.md）。"

# ---- command handlers (skeleton) --------------------------------------------
async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🎲 地下城主（Dungeon Master）\n"
        "命令：\n"
        "/newgame — 开始新游戏（角色创建）\n"
        "/continue — 继续剧情 / 推进一回合\n"
        "/save — 保存进度\n"
        "/load — 读取存档\n"
        "/status — 查看角色\n"
        "/inventory — 背包\n"
        "/quest — 任务\n"
        "/map — 地图\n"
        "/party — 队伍\n"
        "/help — 帮助\n\n"
        "或直接发文字，进行你的冒险行动。"
    )

async def cmd_newgame(update: Update, _ctx) -> None:
    # TODO(DM-Phase 1): INSERT save row + ask ST (via bridge) for the Chinese
    # character-creation opening using DungeonMaster12 + DnD-Base lorebook.
    await update.message.reply_text(
        _placeholder("newgame") + "\n（将创建存档并由 ST 生成中文开场与角色创建菜单）"
    )

async def cmd_continue(update: Update, _ctx) -> None:
    await _generate_and_reply(update, "（玩家请求：请作为地下城主继续推进剧情一回合。）")

async def cmd_save(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("save"))   # TODO UPDATE saves

async def cmd_load(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("load"))

async def cmd_status(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("status"))  # TODO render players

async def cmd_inventory(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("inventory"))

async def cmd_quest(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("quest"))

async def cmd_map(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("map"))

async def cmd_party(update: Update, _ctx) -> None:
    await update.message.reply_text(_placeholder("party"))

async def chat_handler(update: Update, _ctx) -> None:
    await _generate_and_reply(update, update.message.text)

# ---- bridge call with Language Override -------------------------------------
async def _generate_and_reply(update: Update, user_text: str) -> None:
    chat_id = update.effective_chat.id
    placeholder = await update.message.reply_text("🎲 正在演绎……")
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "model": "dungeon-master",
        "stream": True,
        "messages": [
            {"role": "system", "content": DM_LANGUAGE_OVERRIDE},
            {"role": "user", "content": user_text},
        ],
    }
    url = OPENAI_BASE_URL.rstrip("/") + "/chat/completions"
    full = ""
    last_edit = 0.0
    loop = asyncio.get_event_loop()
    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
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
                                    await placeholder.edit_text(full + "▌")
                                except Exception:
                                    pass
                                last_edit = now
        await placeholder.edit_text(full or "（无回复）")
        log.info("chat_id=%s reply len=%d", chat_id, len(full))
    except aiohttp.ClientConnectorError:
        await placeholder.edit_text(
            f"❌ 无法连接 DM bridge（{OPENAI_BASE_URL}）。请确认方案 B 已部署（DM bridge :8013 + DM st-runner）。"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("generate failed: %s", exc)
        await placeholder.edit_text(f"❌ 出错了：{exc}")

# ---- main -------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env and fill it.")
        sys.exit(1)
    init_db(DB_PATH)
    log.info("DB at %s", DB_PATH)
    app: Application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    for name, fn in (
        ("help", cmd_help), ("newgame", cmd_newgame), ("continue", cmd_continue),
        ("save", cmd_save), ("load", cmd_load), ("status", cmd_status),
        ("inventory", cmd_inventory), ("quest", cmd_quest), ("map", cmd_map),
        ("party", cmd_party),
    ):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))
    log.info("Dungeon Master bot starting (isolated instance, plan B).")
    app.run_polling()

if __name__ == "__main__":
    main()
