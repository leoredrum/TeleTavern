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
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
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

# Per-chat lock so two messages in the same chat serialize through ST.
_chat_locks: dict[int, asyncio.Lock] = {}


def lock_for(chat_id: int) -> asyncio.Lock:
    if chat_id not in _chat_locks:
        _chat_locks[chat_id] = asyncio.Lock()
    return _chat_locks[chat_id]


HELP_TEXT = (
    "🤖 *Telegram Tavern V2 (SillyTavern + Qwen3.6)*\n\n"
    "命令：\n"
    "  /start   — 重新开始对话（清空当前 ST 聊天）\n"
    "  /reset   — 清空当前 ST 聊天\n"
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
    await update.effective_message.reply_text(
        "下一条消息会作为新对话的第一条发送。"
    )


async def cmd_ping(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    async with aiohttp.ClientSession() as session:
        ok, detail = await ping_bridge(session)
    icon = "🟢" if ok else "🔴"
    await update.effective_message.reply_text(f"{icon} {detail}")


async def cmd_help(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


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
    # Telegram limit ~4096 chars; keep room for HTML escape expansion.
    if len(full_text) > 4000:
        full_text = full_text[:4000] + "\n…(已截断)"
    try:
        await placeholder.edit_text(full_text)
    except Exception as exc:
        log.warning(f"final edit_text failed: {exc}")
        try:
            await placeholder.edit_text(html.escape(full_text))
        except Exception:
            pass


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is empty. Copy .env.example to .env and fill it.")
        raise SystemExit(1)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("help", cmd_help))
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