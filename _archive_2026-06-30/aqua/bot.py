"""SillyTavern-character Telegram bridge — Phase 1 Pipeline Edition.

Run with:  python bot.py

Phase 1 changes
================
- PromptPipeline replaces direct system_prompt + history concatenation
- Director Prompt as a configurable PromptItem
- ExtendedCharacterCard with DepthPrompts support
- /debug command shows full PromptItem breakdown with token estimates

Architecture
============
Telegram user -> PTB 20.x async dispatcher
              -> SessionStore (SQLite) for persistence
              -> PromptPipeline (modular SillyTavern-style assembly)
              -> OllamaClient (streaming /v1/chat/completions)
                 -> editMessageText incrementally to show partial replies
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import signal
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from character_card import ExtendedCharacterCard, load_character
from config import config
from db import ROLE_ASSISTANT, ROLE_USER, SessionStore
from director import DirectorConfig
from ollama_client import OllamaChatError, OllamaClient
from pipeline import PipelineContext, Persona, PromptPipeline
import pipeline as pipeline_module

logging.basicConfig(
    level=logging.DEBUG if os.environ.get("DEBUG_PROMPT") else logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("st-tg-bot")

# Expose DEBUG_PROMPT to pipeline module
pipeline_module.DEBUG_PROMPT = bool(os.environ.get("DEBUG_PROMPT"))


# ---------- Application state ----------

@dataclass
class AppState:
    card: ExtendedCharacterCard
    pipeline: PromptPipeline
    client: OllamaClient
    store: SessionStore
    # Per-chat generation state: generation token + cancel event + current message_id
    generating: dict[int, asyncio.Event] = None  # type: ignore[assignment]
    # Per-chat lock to serialize generations in a chat
    locks: dict[int, asyncio.Lock] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.generating is None:
            self.generating = {}
        if self.locks is None:
            self.locks = {}


def thread_id_for(update: Update) -> str:
    """One thread per private chat; in groups, one thread per group."""
    chat = update.effective_chat
    if chat is None:
        return "unknown"
    if chat.type == "private":
        return f"u:{chat.id}"
    return f"g:{chat.id}"


def user_label_for(update: Update) -> str:
    u = update.effective_user
    if u is None:
        return config.user_label
    if u.username:
        return u.username
    if u.full_name:
        return u.full_name
    return config.user_label


# ---------- Handlers ----------

HELP_TEXT = (
    "🤖 *Telegram 酒馆 — Phase 1*\n\n"
    "角色：`{char}`\n"
    "模型：`{model}`\n"
    "引擎：PromptPipeline\n\n"
    "命令：\n"
    "  /start   — 重新开始对话（清空历史，播放 first message）\n"
    "  /reset   — 只清空对话历史，保留角色\n"
    "  /character — 查看当前角色卡信息\n"
    "  /model   — 查看当前模型\n"
    "  /debug   — 显示当前 Prompt 结构（Phase 1 新增）\n"
    "  /help    — 显示本帮助\n\n"
    "私聊我直接发消息就行。\n"
    "在群里请 *回复* 我的消息（@mention 也可）以触发对话。"
).format(char=config.char_label, model=config.ollama_model)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: AppState = ctx.bot_data["state"]
    thread = thread_id_for(update)
    state.store.reset(thread)
    # Inject the first message as if Penelope said it
    first_mes = state.card.first_mes or f"*hi* i'm {config.char_label}."
    state.store.append(thread, ROLE_ASSISTANT, first_mes)
    label = user_label_for(update)
    body = (
        f"✨ *{config.char_label}* 准备好了。\n"
        f"_角色卡:_ `{state.card.spec}`\n"
        f"_用户:_ {label}\n"
        f"_模型:_ `{config.ollama_model}`\n\n"
        "输入 `/reset` 重新开始，`/help` 看命令。"
    )
    await update.effective_message.reply_text(
        body, parse_mode=ParseMode.MARKDOWN
    )
    # Then send the character's greeting
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    placeholder = await update.effective_message.reply_text(
        first_mes, parse_mode=ParseMode.MARKDOWN
    )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: AppState = ctx.bot_data["state"]
    thread = thread_id_for(update)
    n = state.store.reset(thread)
    await update.effective_message.reply_text(
        f"🧹 已清空 {n} 条历史。发消息即可重新开始。"
    )


async def cmd_character(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: AppState = ctx.bot_data["state"]
    c = state.card
    parts = [
        f"*Name:* {c.name}",
        f"*Spec:* `{c.spec}`",
        f"*Description:* {len(c.description)} chars",
        f"*Personality:* {len(c.personality)} chars",
        f"*Scenario:* {len(c.scenario)} chars",
        f"*First message:* {len(c.first_mes)} chars",
        f"*Example dialogues:* {len(c.mes_example)} chars",
    ]
    if c.creator:
        parts.append(f"*Creator:* {c.creator}")
    if c.tags:
        parts.append(f"*Tags:* {', '.join(c.tags)}")
    if c.alternate_greetings:
        parts.append(f"*Alternate greetings:* {len(c.alternate_greetings)}")
    await update.effective_message.reply_text(
        "\n".join(parts), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: AppState = ctx.bot_data["state"]
    await update.effective_message.reply_text(
        f"Model: `{state.client.model}`\nEndpoint: `{state.client.base_url}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_debug(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current prompt structure — Phase 1 debug feature (enhanced).

    Sections (separated by markers):
      A. PromptItem table (pos / id / role / depth / tokens / enabled / source)
      B. Final rendered messages (what Ollama actually receives, with token est.)
      C. Full prompt dump (all message contents)
    """
    state: AppState = ctx.bot_data["state"]
    thread = thread_id_for(update)
    history = state.store.history(thread, config.history_limit_messages)

    # Use the last user message as a stand-in for the debug report
    user_sample = history[-1]["content"][:80] if history else "(no history)"

    report = state.pipeline.debug_report(history, user_sample)

    # Telegram 4096 char limit — split into chunks if needed
    CHUNK = 3800
    if len(report) <= CHUNK:
        await update.effective_message.reply_text(
            f"```\n{report}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        # Send header, then chunks
        chunks = [report[i:i + CHUNK] for i in range(0, len(report), CHUNK)]
        await update.effective_message.reply_text(
            f"```\n{chunks[0]}\n```\n… (共 {len(chunks)} 段)",
            parse_mode=ParseMode.MARKDOWN,
        )
        for i, chunk in enumerate(chunks[1:], start=2):
            await update.effective_message.reply_text(
                f"```\n[第 {i}/{len(chunks)} 段]\n{chunk}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)


async def on_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    ev = ctx.bot_data["state"].generating.get(chat_id)
    if ev and not ev.is_set():
        ev.set()
        await query.edit_message_text("⏹ cancelled.")
    else:
        await query.edit_message_text("没有正在进行的生成。")


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    state: AppState = ctx.bot_data["state"]
    chat_id = update.effective_chat.id
    lock = state.locks.setdefault(chat_id, asyncio.Lock())

    # Drop early if a generation is already running
    if state.generating.get(chat_id, asyncio.Event()).is_set():
        await update.effective_message.reply_text(
            "⏳ 上一条回复还没生成完，先等一下或按 /reset。"
        )
        return

    cancel_event = asyncio.Event()
    state.generating[chat_id] = cancel_event

    async with lock:
        try:
            await _generate_reply(update, ctx, cancel_event)
        except OllamaChatError as e:
            log.exception("ollama error")
            await update.effective_message.reply_text(f"❌ ollama error:\n`{e}`", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:  # noqa: BLE001
            log.exception("unexpected error")
            await update.effective_message.reply_text(f"❌ bot error: `{e}`", parse_mode=ParseMode.MARKDOWN)
        finally:
            cancel_event.set()
            state.generating.pop(chat_id, None)


async def _generate_reply(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    cancel_event: asyncio.Event,
) -> None:
    state: AppState = ctx.bot_data["state"]
    chat_id = update.effective_chat.id
    thread = thread_id_for(update)
    user_text = (update.effective_message.text or "").strip()
    if not user_text:
        return

    # Persist user turn
    state.store.append(thread, ROLE_USER, user_text)

    # Build messages via PromptPipeline (Phase 1) — or naive path for AB test
    history = state.store.history(thread, config.history_limit_messages)
    if pipeline_module.PHASE1_ENABLED:
        messages = state.pipeline.assemble(history, user_text)
    else:
        messages = pipeline_module.naive_assemble(state.card, history, user_text)
        log.info("PHASE1_ENABLED=false — using naive prompt path (AB test)")

    if pipeline_module.DEBUG_PROMPT:
        report = state.pipeline.debug_report(history, user_text)
        log.debug("PROMPT ASSEMBLY:\n%s", report)

    # Show "typing" + draft message
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⏹ Cancel", callback_data="cancel")]]
    )
    placeholder = await update.effective_message.reply_text(
        "…", reply_markup=cancel_kb
    )

    accumulated = ""
    last_edit = 0.0
    last_sent_text = ""

    async for piece in state.client.stream_chat(messages, cancel_event=cancel_event):
        accumulated += piece
        now = asyncio.get_event_loop().time()
        # Throttle edits
        if now - last_edit < config.edit_interval:
            continue
        last_edit = now
        rendered = _render_for_telegram(accumulated)
        if rendered != last_sent_text and len(rendered) > len(last_sent_text):
            try:
                await placeholder.edit_text(
                    rendered, parse_mode=ParseMode.MARKDOWN
                )
                last_sent_text = rendered
            except Exception as e:  # noqa: BLE001
                # Telegram rejects identical messages or messages that exceed limit
                log.debug("edit skipped: %s", e)

    # Final edit
    final = _render_for_telegram(accumulated) or "（空回复）"
    try:
        await placeholder.edit_text(final, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fall back to plain text if markdown parse fails
        await placeholder.edit_text(final)

    # Persist assistant turn
    state.store.append(thread, ROLE_ASSISTANT, accumulated)


def _render_for_telegram(text: str) -> str:
    """Trim and convert to a telegram-friendly form.

    - markdown is already supported by telegram if we escape with ParseMode.MARKDOWN
    - we keep actions (*italics*) and dialogue, but trim very long output.
    """
    if not text:
        return ""
    # Telegram message limit is 4096 chars. We truncate to leave room for edits.
    if len(text) > 3800:
        text = text[:3800] + "…"
    return text


# ---------- Bootstrap ----------

def build_state() -> AppState:
    log.info("loading character card from %s", config.character_path)
    card: ExtendedCharacterCard = load_character(config.character_path)
    log.info(
        "loaded character '%s' (spec=%s, desc=%d, scen=%d, first_mes=%d, "
        "depth_prompts=%d, system_prompt=%d, phi=%d)",
        card.name,
        card.spec,
        len(card.description),
        len(card.scenario),
        len(card.first_mes),
        len(card.depth_prompts),
        len(card.system_prompt),
        len(card.post_history_instructions),
    )

    # Phase 1: DirectorConfig — all RP rules in one place
    director_cfg = DirectorConfig(
        enabled=True,
        no_option_listing=True,
        no_meta_commentary=True,
        no_prompt_explanation=True,
        no_fourth_wall=True,
        no_repeat_micro_expressions=True,
        no_emotion_loops=True,
        no_user_repeat=True,
        advance_narrative=True,
        proactive_role=True,
        avoid_purple_prose=True,
        reply_length_min=80,
        reply_length_max=280,
    )

    # PromptPipeline — modular SillyTavern-style assembly
    pipeline = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        persona=None,
        char_label=config.char_label,
        user_label=config.user_label,
        reply_language=config.reply_language or None,
        lorebook=None,
        memory_manager=None,
    )

    client = OllamaClient(
        config.ollama_url,
        config.ollama_model,
        max_tokens=config.max_tokens,
    )
    store = SessionStore(config.db_path)
    return AppState(
        card=card,
        pipeline=pipeline,
        client=client,
        store=store,
    )


def main() -> None:
    state = build_state()
    log.info(
        "starting bot: ollama=%s model=%s nsfw=%s",
        config.ollama_url,
        config.ollama_model,
        config.nsfw_enabled,
    )

    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["state"] = state

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("character", cmd_character))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(on_cancel, pattern="^cancel$"))
    # Handle direct messages + bot mentions/replies in groups
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.PRIVATE | filters.REPLY | filters.Mention(config.char_label)),
            handle_message,
        )
    )

    # Run with signal-friendly shutdown
    async def _shutdown(app: Application) -> None:
        log.info("shutting down")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()