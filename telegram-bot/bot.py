"""
Telegram Tavern V2 bot.

A thin Python Telegram bot that talks OpenAI-format to the bridge
(`bridge/st_bridge.py`). SillyTavern holds the canonical chat state
via its own WebUI / persistence; this bot does not duplicate history.

Run:
    cd ~/Documents/SillyTavern/connector/telegram-bot
    cp .env.example .env   # then fill TELEGRAM_BOT_TOKEN
    ../venv/bin/python bot.py
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import signal
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

# V2.3: reuse the DM long-message splitter (copied into this dir at deploy).
# Handles >4096-char replies by splitting on natural boundaries and sending
# sequential segments, instead of hard-truncating to 4000 chars.
import telegram_splitter
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("st-tg-bot-v2")

# PTB uses httpx under the hood and at INFO level prints the full request URL,
# which embeds the Telegram bot token (path segment `bot<TOKEN>/...`).
# Drop httpx to WARNING so token never lands in our logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# PTB's own Application logger prints token-free messages, leave at INFO.

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8003/v1")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "tavern-v2-user-api-key-change-me")
EDIT_INTERVAL_S = float(os.environ.get("EDIT_INTERVAL_S", "1.0"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "180"))

# V2.3 multi-char — bridge control endpoints (same host:port as the chat endpoint).
_BRIDGE_ROOT = OPENAI_BASE_URL.rstrip("/").removesuffix("/v1")
CHARACTERS_URL = _BRIDGE_ROOT + "/v1/characters"
SELECT_CHAR_URL = _BRIDGE_ROOT + "/v1/select_character"
CLEAR_CHAT_URL = _BRIDGE_ROOT + "/v1/clear_chat"
AUTH_HEADERS = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

# Per-chat lock so two messages in the same chat serialize through ST.
_chat_locks: dict[int, asyncio.Lock] = {}


def lock_for(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


HELP_TEXT = (
    "🤖 *Telegram Tavern V2 (SillyTavern + Qwen3.6)*\n\n"
    "命令：\n"
    "  /start   — 重置当前对话（开新 ST 聊天）\n"
    "  /character — 列出角色并点击按钮切换（⭐当前 🎬Director）\n"
    "  /use <名称> — 切换角色（如 /use Penelope3），切换即开新对话\n"
    "  /where   — 查看当前角色 / 消息数 / Director 状态\n"
    "  /reset   — 清空当前角色对话（开新聊天，保留历史文件）\n"
    "  /ping    — 检查 bridge / ST / Ollama 健康\n"
    "  /help    — 显示此帮助\n\n"
    "直接发文本即可对话。"
)


async def ping_bridge(session: aiohttp.ClientSession) -> tuple[bool, str]:
    """Hit bridge /v1/models and report. Returns (ok, detail)."""
    try:
        async with session.get(
            OPENAI_BASE_URL.rstrip("/").removesuffix("/chat/completions") + "/models",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                return True, "bridge OK"
            return False, f"bridge HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001
        return False, f"bridge unreachable: {exc}"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return
    # Best-effort: send a 1-message "system" prompt through bridge to flush ST chat.
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(
                OPENAI_BASE_URL.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "st-bridge",
                    "messages": [{"role": "user", "content": "/start"}],
                    "stream": False,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                _ = await resp.text()
        except Exception as exc:  # noqa: BLE001
            log.warning("/start: bridge call failed: %s", exc)
    await update.effective_message.reply_text(
        "对话已重置。直接发消息开始。",
    )


async def cmd_reset(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    # V2.3: ST-native clearChat for the active character — starts a fresh chat.
    # ST keeps the old chat file in its chats/ history; this only clears the
    # live session. Needs bridge + extension connected and ST to export clearChat.
    try:
        async with aiohttp.ClientSession() as session:
            await clear_active_chat(session)
        await update.effective_message.reply_text("♻️ 当前角色对话已清空，下一条消息开始新会话。")
    except Exception as exc:  # noqa: BLE001
        log.warning("/reset clear_chat failed: %s", exc)
        await update.effective_message.reply_text(
            f"❌ 清空失败: {exc}\n（bridge/extension 未连接，或 ST 未导出 clearChat）")


async def cmd_ping(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    async with aiohttp.ClientSession() as session:
        ok, detail = await ping_bridge(session)
    icon = "🟢" if ok else "🔴"
    await update.effective_message.reply_text(f"{icon} {detail}")


async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


# ---------- V2.3 multi-char: character list / switch / where ----------



CHARACTER_MENU_LABELS = {
    "Abby - Your Daughter's Friend.png": ("艾比", "女儿的朋友，青春敏感的熟人关系"),
    "Abby - Your Wife is a Lesbian Pornstar.png": ("艾比", "成熟女演员，婚姻与秘密纠葛"),
    "Aoi - Mystery Romance.png": ("葵", "悬疑恋爱，冷静神秘的现代女性"),
    "Aurelias Edge.png": ("奥蕾莉亚健身馆", "高端健身工作室与训练氛围"),
    "Charlie Moonwhisper.png": ("查莉·月语", "召唤者关系，带奇幻契约感"),
    "Chloe - Property of the mean girl.png": ("克洛伊", "校园强势女孩与权力关系"),
    "ChronoKeeper-Cherry.png": ("樱桃时计官", "时间管理与叙事控制工具"),
    "Clara -  Your Girlfriend Wants to Watch You with Other Women.png": ("克拉拉", "安静的女友，关系边界测试"),
    "Clara-Your-Girlfriend-Wants-to-Watch-You-with-Other-Women-aicharactercards.com_.png": ("克拉拉", "安静的女友，关系边界测试"),
    "Corin-Wickes-Your-personal-Maid-aicharactercards.com_.png": ("科林·威克斯", "私人女仆，娇小而忠诚"),
    "Diana-Game-Master-aicharactercards.com_-1.png": ("黛安娜", "宇宙女神型游戏主持人"),
    "Emi, the Tsundere Forced into Fashion Photoshoots.png": ("田中绘美", "傲娇好友，时尚拍摄事件"),
    "Free Use World.png": ("自由使用世界", "法律设定驱动的开放世界"),
    "Kira -  The Abandoned Android Maid.png": ("绮拉", "被遗弃的家政仿生女仆"),
    "Lily-Your-Stepstister-caught-you-masturbating-aicharactercards.com_.png": ("莉莉", "继妹，尴尬事件后的同居张力"),
    "Melissa-aicharactercards.com_.png": ("梅丽莎", "外表保守的大学生"),
    "Narrator.png": ("旁白", "中立叙事者，适合世界推进"),
    "Nikki's diary of torments.png": ("妮基日记", "约会模拟与校园压迫剧情"),
    "Omni-Tower Management System & Residents.png": ("全域塔管理系统", "建筑 AI 与住户群像"),
    "Princess-Aazka-of-Dalvasca-aicharactercards.com_.png": ("达尔瓦斯卡公主阿兹卡", "异国公主，权力与宫廷冒险"),
    "Red.png": ("红", "龙裔角色，奇幻体质与冲突"),
    "Sam Harper.png": ("山姆·哈珀", "阳光感的现代熟人角色"),
    "Sarah and Stella - Your Mother's Twin Moves In.png": ("莎拉与斯黛拉", "母亲的双胞胎姐妹搬来同住"),
    "She Was Everyones Favorite. Now Shes Yours to Deal With.png": ("克洛伊·斯特林", "昔日万人迷，如今需要你处理"),
    "Sophia - The Audition.png": ("索菲娅", "艺术学生，试镜场景"),
    "The Bells Farm.png": ("贝尔农场", "1950 年代农场设定"),
    "The Chronicles of Cyraeth.png": ("赛瑞斯编年史", "成熟向中世纪高奇幻世界"),
    "Wise wolf Holo to Glory.png": ("赫萝", "贤狼女神，智慧与旅途"),
    "Yamamoto Aiko.png": ("山本爱子", "高中生，日常关系向"),
    "Zoe-Your-Sister-Became-a-Nudist-aicharactercards.com_.png": ("佐伊", "姐姐/妹妹家庭日常冲突"),
    "aicc-2025-07-24_A-Narrated-Life.png": ("你是旁白：雪纪的人生", "你作为她听得见的旁白声音，实时干预她的日常与命运"),
    "aicc-2025-07-24_June_.png": ("琼", "红发红眼的强势角色"),
    "aicc-2026-06-15_Penelope3.png": ("佩内洛普", "Director 启用，主线推荐角色"),
}


def character_menu_label(c: dict) -> tuple[str, str]:
    avatar = c.get("avatar") or ""
    if avatar in CHARACTER_MENU_LABELS:
        return CHARACTER_MENU_LABELS[avatar]
    raw = (c.get("display_name") or c.get("name") or avatar or "未命名角色").strip()
    desc = (c.get("description") or "").replace("\r", " ").replace("\n", " ").strip()
    if len(desc) > 28:
        desc = desc[:28].rstrip() + "..."
    return raw, desc or "暂无简介"


def character_menu_items(chars: list[dict]) -> list[dict]:
    """Return menu-visible characters with Chinese labels and exact duplicates removed."""
    items = []
    seen = set()
    for c in chars:
        name, intro = character_menu_label(c)
        desc_key = ((c.get("description") or "").strip()[:220]).lower()
        key = ((c.get("name") or name).strip().lower(), desc_key)
        if key in seen:
            continue
        seen.add(key)
        cc = dict(c)
        cc["menu_name"] = name
        cc["menu_intro"] = intro
        items.append(cc)
    return items

def match_character(characters: list[dict], query: str) -> dict | None:
    """Find a character by exact name → case-insensitive name → avatar → substring.

    Pure function (no I/O) so it is unit-testable without the bridge.
    """
    if not characters or not query:
        return None
    q = query.strip()
    ql = q.lower()
    # 1) exact name
    for c in characters:
        if (c.get("name") or "").strip() == q:
            return c
    # 2) case-insensitive exact name
    for c in characters:
        if (c.get("name") or "").strip().lower() == ql:
            return c
    # 3) avatar exact
    for c in characters:
        if (c.get("avatar") or "") == q:
            return c
    # 4) substring (name contains query) — pick the shortest matching name
    #    (least ambiguous), e.g. /use pen → Penelope3 over Penelope-Long-Name.
    subs = [c for c in characters if ql in (c.get("name") or "").lower()]
    if subs:
        return min(subs, key=lambda c: len(c.get("name") or ""))
    return None


async def fetch_characters(session: aiohttp.ClientSession) -> list[dict]:
    """GET /v1/characters → list of char dicts (name/avatar/active/director_enabled/...)."""
    async with session.get(
        CHARACTERS_URL, headers=AUTH_HEADERS,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"bridge characters HTTP {resp.status}: {data}")
        return data.get("characters", [])


async def select_character(session: aiohttp.ClientSession, avatar: str) -> dict:
    """POST /v1/select_character {avatar} → {ok,name,active_character,director_enabled}."""
    async with session.post(
        SELECT_CHAR_URL,
        headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        json={"avatar": avatar},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if resp.status != 200 or not data.get("ok"):
            raise RuntimeError(data.get("error") or f"select HTTP {resp.status}")
        return data


async def clear_active_chat(session: aiohttp.ClientSession) -> dict:
    """POST /v1/clear_chat → ST clears the active character's chat (new chat)."""
    async with session.post(
        CLEAR_CHAT_URL, headers=AUTH_HEADERS,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        data = await resp.json()
        if resp.status != 200 or not data.get("ok"):
            raise RuntimeError(data.get("error") or f"clear HTTP {resp.status}")
        return data


async def cmd_character(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    # TeleTervan (2026-06-30): list cards WITHOUT a world book (the extension
    # already filters) as an inline keyboard — one button per card, tap to
    # switch. callback_data "switch:<avatar>" is handled by on_switch_callback.
    try:
        async with aiohttp.ClientSession() as session:
            chars = await fetch_characters(session)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"❌ 无法获取角色列表: {exc}")
        return
    chars = character_menu_items(chars)
    if not chars:
        await update.effective_message.reply_text("🎭 暂无可用角色（已过滤掉带世界书的卡）。")
        return
    keyboard = []
    lines = []
    for idx, c in enumerate(chars):
        nm = c.get("menu_name") or c.get("display_name") or c.get("name") or c.get("avatar") or "?"
        intro = c.get("menu_intro") or "暂无简介"
        mark = "⭐ " if c.get("active") else ""
        director = " 🎬" if c.get("director_enabled") else ""
        button_text = f"{idx + 1}. {mark}{nm}{director}｜{intro}"
        keyboard.append([InlineKeyboardButton(
            text=button_text[:96],
            callback_data=f"switchidx:{idx}",
        )])
    head = f"🎭 选择角色（共 {len(chars)} 个，⭐=当前，🎬=Director）："
    await update.effective_message.reply_text(head, reply_markup=InlineKeyboardMarkup(keyboard))


async def on_switch_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    # TeleTervan (2026-06-30): tap a /character button → switch_character via
    # bridge + confirm in place (clears the keyboard). callback_data starts
    # with "switch:".
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data or ""
    if not data.startswith("switchidx:"):
        return
    try:
        idx = int(data[len("switchidx:"):])
    except ValueError:
        await query.edit_message_text("❌ 切换失败：无效的角色索引。")
        return
    try:
        async with aiohttp.ClientSession() as session:
            chars = character_menu_items(await fetch_characters(session))
            if idx < 0 or idx >= len(chars):
                await query.edit_message_text("❌ 切换失败：角色列表已变化，请重新发送 /character。")
                return
            target = chars[idx]
            avatar = target.get("avatar") or ""
            if not avatar:
                await query.edit_message_text("❌ 切换失败：角色卡缺少 avatar。")
                return
            res = await select_character(session, avatar)
    except Exception as exc:  # noqa: BLE001
        await query.edit_message_text(f"❌ 切换失败: {exc}")
        return
    name = (target and (target.get("menu_name") or target.get("display_name") or target.get("name"))) or res.get("name") or avatar
    d = " 🎬(Director 已启用)" if res.get("director_enabled") else ""
    await query.edit_message_text(f"✅ 已切换到 {name}{d}\n新对话已开始，发消息开始聊天。")


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args if context and context.args else []
    query = " ".join(args).strip()
    if not query:
        await update.effective_message.reply_text("用法：/use <角色名>\n例如：/use Penelope3\n发 /chars 查看列表。")
        return
    try:
        async with aiohttp.ClientSession() as session:
            chars = await fetch_characters(session)
            target = match_character(chars, query)
            if target is None:
                await update.effective_message.reply_text(
                    f"❌ 找不到角色「{query}」。发 /chars 查看列表。")
                return
            res = await select_character(session, target.get("avatar"))
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"❌ 切换失败: {exc}")
        return
    name = res.get("name") or target.get("name") or "?"
    d = " 🎬(Director 已启用)" if res.get("director_enabled") else ""
    await update.effective_message.reply_text(f"✅ 已切换到 {name}{d}\n新对话已开始。")


async def cmd_where(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            chars = await fetch_characters(session)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"❌ 无法获取当前角色: {exc}")
        return
    active = next((c for c in chars if c.get("active")), None)
    if active is None:
        await update.effective_message.reply_text("⚠️ 当前没有活动角色（ST 可能处于 Assistant 模式）。")
        return
    name = active.get("name") or "?"
    msgs = active.get("chat_messages", 0)
    d = "🎬 Director：已启用" if active.get("director_enabled") else "🎬 Director：关闭"
    await update.effective_message.reply_text(
        f"📍 当前角色：{name}\n💬 当前会话消息数：{msgs}\n{d}")


async def chat_handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    user_text = msg.text.strip()
    if not user_text:
        return

    lock = lock_for(chat_id)
    async with lock:
        # Send "typing" action continuously while generating.
        typing_task = asyncio.create_task(_keep_typing(update, context=None))
        try:
            await _generate_and_reply(update, user_text)
        finally:
            typing_task.cancel()
            try:
                await typing_task
            except asyncio.CancelledError:
                pass


async def _keep_typing(update: Update, context) -> None:
    try:
        while True:
            await update.effective_chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def _generate_and_reply(update: Update, user_text: str) -> None:
    msg = update.effective_message
    log.info(f"received msg from chat_id={update.effective_chat.id}: {user_text!r}")
    placeholder = await msg.reply_text("… 思考中 …")

    payload = {
        "model": "st-bridge",
        "messages": [{"role": "user", "content": user_text}],
        "stream": True,
    }

    full_text = ""
    last_edit = 0.0
    edit_interval = EDIT_INTERVAL_S
    loop = asyncio.get_event_loop()
    chunk_count = 0
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENAI_BASE_URL.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S),
            ) as resp:
                log.info(f"bridge response: status={resp.status} ct={resp.headers.get('Content-Type')}")
                if resp.status != 200:
                    body = await resp.text()
                    log.error(f"bridge HTTP {resp.status}: {body[:200]}")
                    await placeholder.edit_text(f"❌ bridge HTTP {resp.status}: {body[:200]}")
                    return
                buffer = ""
                async for raw_chunk in resp.content.iter_any():
                    buffer += raw_chunk.decode("utf-8", errors="replace")
                    chunk_count += 1
                    log.debug(f"raw chunk #{chunk_count}: {raw_chunk[:200]!r}")
                    while "\n\n" in buffer:
                        line, buffer = buffer.split("\n\n", 1)
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[len("data:"):].strip()
                        if data == "[DONE]":
                            log.debug("got [DONE]")
                            break
                        try:
                            evt = json.loads(data)
                        except Exception as exc:
                            log.warning(f"failed to parse SSE: {data[:200]!r} ({exc})")
                            continue
                        delta = (
                            (evt.get("choices") or [{}])[0]
                            .get("delta", {})
                            .get("content")
                        )
                        log.debug(f"SSE delta #{chunk_count}: {delta!r}")
                        if not delta:
                            continue
                        full_text += delta
                        now = loop.time()
                        if now - last_edit >= edit_interval:
                            last_edit = now
                            preview = full_text[:3800] + ("…" if len(full_text) > 3800 else "")
                            try:
                                await placeholder.edit_text(preview or "…")
                            except Exception as exc:
                                log.warning(f"edit_text failed: {exc}")
    except asyncio.TimeoutError:
        log.error("bridge request timeout")
        await placeholder.edit_text("❌ 生成超时。")
        return
    except aiohttp.ClientError as exc:
        log.error(f"bridge client error: {exc}")
        await placeholder.edit_text(f"❌ 网络错误: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("generate failed")
        await placeholder.edit_text(f"❌ 错误: {exc}")
        return

    log.info(f"stream done: chunks={chunk_count}, full_text len={len(full_text)}")
    if not full_text:
        log.warning("full_text empty after streaming — sending fallback")
        full_text = "（空回复）"
    # V2.3: reuse DM telegram_splitter — split long replies on natural
    # boundaries and send sequential segments (first segment edits the
    # streaming placeholder). No more 4000-char hard truncation.
    diag = await telegram_splitter.send_long_message(
        message=update.effective_message, text=full_text, first_message=placeholder)
    log.info("send_long_message: segs=%s lens=%s exc=%s",
             diag.get("segments"), diag.get("lengths"), diag.get("exceptions"))


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env and fill it.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_start))  # Chinese UX alias: start a fresh chat
    app.add_handler(CommandHandler("character", cmd_character))
    app.add_handler(CommandHandler("chars", cmd_character))  # backward-compat alias
    app.add_handler(CommandHandler("use", cmd_use))
    app.add_handler(CommandHandler("where", cmd_where))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_switch_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat_handler))

    log.info("V2 bot starting; bridge=%s", OPENAI_BASE_URL)

    # PTB 22.x: signal handlers must be installed before run_polling.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, app.stop_running)
        except NotImplementedError:
            pass

    app.run_polling(drop_pending_updates=True, close_loop=False)


if __name__ == "__main__":
    main()
