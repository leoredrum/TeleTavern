"""Dialogue mode — pure character chat with per-chat character switching.

Ported behaviour from connector/telegram-bot (SINGLE-CHAR): /character menu with
inline buttons, /use <name>, /where, per-Telegram-chat binding so a chat never
drifts to another character. Generation goes through CharacterRuntime
(card + World Info + Director/story hints) instead of SillyTavern.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from story_engine import StoryState

from ..config import BotConfig
from ..engine import CharacterRuntime
from ..postprocess import apply_translation
from ..storage import BotStorage
from ..telegram_common import chunk_text, stream_reply

log = logging.getLogger("tavern.dialogue")


class DialogueBot:
    def __init__(self, bot: BotConfig, data_dir: Path):
        self.cfg = bot
        self.data_dir = data_dir
        self.store = BotStorage(data_dir / "data" / bot.name / "chat.db")
        self.runtimes: dict[str, CharacterRuntime] = {}
        for cf in bot.character_files:
            try:
                self.runtimes[cf] = CharacterRuntime(bot, cf, data_dir)
            except Exception as exc:  # noqa: BLE001
                log.error("[%s] cannot load %s: %s", bot.name, cf, exc)
        if not self.runtimes:
            raise RuntimeError(f"{bot.name}: no loadable character cards")
        self.locks: dict[int, asyncio.Lock] = {}
        self.story: dict[str, StoryState] = {}

    # ---- helpers ------------------------------------------------------------------
    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

    def menu(self) -> list[tuple[str, CharacterRuntime]]:
        return [(cf, rt) for cf, rt in self.runtimes.items()]

    def bound(self, chat_id: int) -> CharacterRuntime | None:
        b = self.store.get_binding(chat_id)
        if b and b["card_file"] in self.runtimes:
            return self.runtimes[b["card_file"]]
        if len(self.runtimes) == 1:
            cf, rt = next(iter(self.runtimes.items()))
            self.store.set_binding(chat_id, cf, rt.card.name)
            return rt
        return None

    def thread(self, chat_id: int, rt: CharacterRuntime) -> str:
        return f"{chat_id}:{rt.card_file}"

    def match(self, query: str) -> CharacterRuntime | None:
        q = (query or "").strip()
        ql = q.lower()
        for cf, rt in self.runtimes.items():
            if rt.card.name.strip() == q or cf == q:
                return rt
        for cf, rt in self.runtimes.items():
            if rt.card.name.strip().lower() == ql:
                return rt
        subs = [rt for cf, rt in self.runtimes.items() if ql and (ql in rt.card.name.lower() or ql in cf.lower())]
        return min(subs, key=lambda r: len(r.card.name)) if subs else None

    def post(self, text: str) -> str:
        return apply_translation(text, self.cfg.translation_table)

    async def start_fresh(self, update: Update, rt: CharacterRuntime, greeting_index: int | None = None) -> None:
        chat_id = update.effective_chat.id
        th = self.thread(chat_id, rt)
        self.store.reset(th)
        self.story.pop(th, None)
        greeting = self.post(rt.greeting(greeting_index))
        self.store.append(th, "assistant", greeting)
        for chunk in chunk_text(greeting):
            await update.effective_message.reply_text(chunk)

    # ---- commands --------------------------------------------------------------------
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rt = self.bound(update.effective_chat.id)
        if rt is None:
            await self.cmd_character(update, ctx)
            return
        idx = None
        if ctx.args and ctx.args[0].isdigit():
            idx = int(ctx.args[0])
        await self.start_fresh(update, rt, idx)

    async def cmd_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rt = self.bound(update.effective_chat.id)
        if rt is None:
            await update.effective_message.reply_text("先用 /character 选一个角色。")
            return
        await self.start_fresh(update, rt)

    async def cmd_character(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        cur = self.store.get_binding(chat_id)
        cur_file = cur["card_file"] if cur else None
        rows, lines = [], ["🎭 可用角色（点按钮切换，切换会开新对话）："]
        for i, (cf, rt) in enumerate(self.menu()):
            star = "⭐" if cf == cur_file else ""
            director = "🎬" if cf in (self.cfg.director_characters or []) else ""
            desc = (rt.card.creator_notes or rt.card.description or "").replace("\n", " ").strip()
            lines.append(f"{i + 1}. {star}{director}{rt.card.name} — {desc[:28] + '…' if len(desc) > 28 else desc or '暂无简介'}")
            rows.append([InlineKeyboardButton(f"{star}{rt.card.name}", callback_data=f"switch:{i}")])
        await update.effective_message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))

    async def on_switch(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        await q.answer()
        try:
            idx = int((q.data or "").split(":", 1)[1])
            cf, rt = self.menu()[idx]
        except (ValueError, IndexError):
            await q.edit_message_text("❌ 无效选择，请重新 /character。")
            return
        chat_id = update.effective_chat.id
        self.store.set_binding(chat_id, cf, rt.card.name)
        await q.edit_message_text(f"✅ 已切换到 {rt.card.name}，开始新对话。")
        await self.start_fresh(update, rt)

    async def cmd_use(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rt = self.match(" ".join(ctx.args or []))
        if rt is None:
            await update.effective_message.reply_text("找不到这个角色。用 /character 查看列表。")
            return
        self.store.set_binding(update.effective_chat.id, rt.card_file, rt.card.name)
        await update.effective_message.reply_text(f"✅ 已切换到 {rt.card.name}，开始新对话。")
        await self.start_fresh(update, rt)

    async def cmd_where(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        rt = self.bound(chat_id)
        if rt is None:
            await update.effective_message.reply_text("尚未绑定角色。用 /character 选择。")
            return
        n = self.store.count(self.thread(chat_id, rt))
        director = "开" if rt.card_file in (self.cfg.director_characters or []) else "关"
        await update.effective_message.reply_text(
            f"🎭 当前角色：{rt.card.name}（{rt.card_file}）\n消息数：{n}\nDirector：{director}\n"
            f"世界书：{rt.worldinfo.summary()}\n模型：{self.cfg.model}")

    async def cmd_ping(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(self.cfg.ollama_url.rstrip("/") + "/api/tags") as r:
                    data = await r.json()
            names = [m.get("name") for m in data.get("models", [])]
            ok = self.cfg.model in names
            await update.effective_message.reply_text(
                f"Ollama ✅ {len(names)} 个模型\n{'✅' if ok else '❌ 未找到'} {self.cfg.model}")
        except Exception as exc:  # noqa: BLE001
            await update.effective_message.reply_text(f"Ollama ❌ {str(exc)[:120]}")

    async def cmd_debug(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        rt = self.bound(chat_id)
        if rt is None:
            return
        hist = self.store.history(self.thread(chat_id, rt), self.cfg.history_limit)
        report = rt.debug_prompt(hist, hist[-1]["content"] if hist else "")
        for chunk in chunk_text(report):
            await update.effective_message.reply_text(chunk)

    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"🤖 {self.cfg.name}\n\n"
            "/start [n] — 重新开始对话（n = 第 n 个备选开场白）\n"
            "/character — 列出角色并点击切换\n"
            "/use <名称> — 切换角色\n"
            "/where — 当前角色 / 消息数 / 世界书\n"
            "/reset — 清空当前角色对话\n"
            "/ping — 检查 Ollama 与模型\n"
            "/debug — 查看本轮 prompt 结构\n"
            "/help — 帮助\n\n直接发文字即可对话。")

    # ---- chat -----------------------------------------------------------------------------
    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        text = (msg.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id
        rt = self.bound(chat_id)
        if rt is None:
            await msg.reply_text("这个聊天还没绑定角色，先用 /character 选一个。")
            return
        lock = self.lock(chat_id)
        if lock.locked():
            await msg.reply_text("⏳ 上一条还在生成，稍等。")
            return
        async with lock:
            th = self.thread(chat_id, rt)
            self.store.append(th, "user", text)
            history = self.store.history(th, self.cfg.history_limit)
            ss = self.story.setdefault(th, StoryState()) if rt.pipeline.story_engine else None
            final = await stream_reply(msg, rt.stream(history, text, story_state=ss), postprocess=self.post)
            if final:
                self.store.append(th, "assistant", final)
                if ss is not None:
                    try:
                        rt.pipeline.story_engine.observe(ss, text, final)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("story observe failed: %s", exc)

    # ---- wiring --------------------------------------------------------------------------
    def build_application(self) -> Application:
        app = ApplicationBuilder().token(self.cfg.token).build()
        app.add_handler(CommandHandler(["start", "newgame"], self.cmd_start))
        app.add_handler(CommandHandler(["character", "chars"], self.cmd_character))
        app.add_handler(CommandHandler("use", self.cmd_use))
        app.add_handler(CommandHandler("where", self.cmd_where))
        app.add_handler(CommandHandler("reset", self.cmd_reset))
        app.add_handler(CommandHandler("ping", self.cmd_ping))
        app.add_handler(CommandHandler("debug", self.cmd_debug))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CallbackQueryHandler(self.on_switch, pattern=r"^switch:"))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app
