"""RPG mode — narrated adventure with an authoritative, program-owned world state.

Per turn:
  1. CURRENT WORLD STATE (+ optional RPG snapshot / Director state + guards) is
     injected IN_CHAT at depth 1, right before the newest player message;
  2. the model narrates;
  3. regex StateUpdater + LLM StateExtractor record new entities (with aliases),
     deaths, location, battle; optional RuleEngine / DirectorEngine intake;
  4. StateValidator conflicts become next turn's correction line.

Used by DungeonMaster (rules + scenes on), MUSHOKU and SAENGMYEONG (world state
+ extraction only, their language override and translation table from yaml).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from telegram import Update
from telegram.ext import (Application, ApplicationBuilder, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from ..config import BotConfig
from ..engine import CharacterRuntime
from ..postprocess import apply_translation
from ..rpg import director_engine as DE
from ..rpg import game_state as G
from ..rpg import rpg_engine as RE
from ..rpg import state_extractor as SX
from ..rpg.sessions import RPGSessions
from ..storage import BotStorage
from ..telegram_common import chunk_text, stream_reply

log = logging.getLogger("tavern.rpg")

_RACE_KEYS = {"人类": "人类", "精灵": "精灵", "矮人": "矮人", "半身人": "半身人", "半精灵": "半精灵",
              "提夫林": "提夫林", "龙裔": "龙裔", "侏儒": "侏儒", "半兽人": "半兽人"}
_CLASS_KEYS = {"战士": "战士", "法师": "法师", "牧师": "牧师", "游荡者": "游荡者", "盗贼": "游荡者",
               "游侠": "游侠", "圣武士": "圣武士", "圣骑士": "圣武士"}


def detect_character(text: str):
    t = text or ""
    cls = next((c for k, c in _CLASS_KEYS.items() if k in t), None)
    if not cls:
        return None
    race = next((r for k, r in _RACE_KEYS.items() if k in t), None) or "人类"
    return race, cls


def player_name_from_text(text: str, cls: str) -> str:
    for pat in (r'叫[做]?\s*[「『“"]?\s*([一-龥A-Za-z]{2,6})', r'名字[是为]?\s*([一-龥A-Za-z]{2,6})'):
        m = re.search(pat, text or "")
        if m:
            return m.group(1)
    return {"战士": "勇者", "法师": "秘法者", "牧师": "祝祷者", "游荡者": "影刃",
            "游侠": "巡林客", "圣武士": "誓卫"}.get(cls, "冒险者")


class RPGBot:
    def __init__(self, bot: BotConfig, data_dir: Path):
        self.cfg = bot
        self.data_dir = data_dir
        self.rt = CharacterRuntime(bot, bot.character_files[0], data_dir)
        bdir = data_dir / "data" / bot.name
        self.store = BotStorage(bdir / "chat.db")
        self.sessions = RPGSessions(bdir / "sessions.db", bdir / "sessions", self.rt.card.name)
        self.gsm = G.GameStateManager(str(bdir / "world_state.db"))
        self.gsm.ensure_schema()
        self.rule = None
        self.director = None
        if bot.rules:
            self.rule = RE.RuleEngine(str(bdir / "world_state.db"))
            self.rule.ensure_schema()
        if bot.scenes:
            self.director = DE.DirectorEngine(str(bdir / "world_state.db"))
            self.director.ensure_schema()
        self.locks: dict[int, asyncio.Lock] = {}

    # ---- helpers ---------------------------------------------------------------------
    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(chat_id, asyncio.Lock())

    # strip the DM's hidden HP machine tags (〔HP-5〕/【HP+3】/[HP-2]) from the
    # player-visible text — they are parsed authoritatively, not shown.
    _HP_TAG_STRIP = re.compile(r"[〔【\[]\s*HP\s*[+-]\s*\d+\s*[〕】\]]")

    def post(self, text: str) -> str:
        text = apply_translation(text, self.cfg.translation_table)
        text = self._HP_TAG_STRIP.sub("", text)
        return text

    def status_panel(self, st, session_id: str) -> str:
        """Compact, clearly-separated status panel appended AFTER the narration,
        so story and numbers never blur together (rules bots only)."""
        p = st.persistent.to_dict()
        loc = p.get("location") or "未知"
        ens = [e.get("name", "?") for e in (p.get("enemies") or []) if e.get("status") == "alive"]
        lines = ["━━━━━━ 状态 ━━━━━━"]
        snap = self.rule.get_or_init(session_id)
        pl = snap.player
        downed = int(pl.get("hp", 0)) <= 0 or "倒下" in (pl.get("conditions") or [])
        head = "💀 已倒下（濒死）" if downed else "❤️"
        lines.append("%s HP %d/%d　🛡 AC %d　⭐ Lv.%d" % (
            head, int(pl.get("hp", 0)), int(pl.get("max_hp", 0)),
            int(pl.get("ac", 0)), int(pl.get("level", 1))))
        conds = [c for c in (pl.get("conditions") or []) if c != "倒下"]
        if conds:
            lines.append("状态：" + "、".join(conds))
        lines.append("📍 " + loc + ("　⚔ 敌人：" + "、".join(ens) if ens else ""))
        return "\n".join(lines)

    def thread(self, chat_id: int, session_id: str) -> str:
        return f"{chat_id}:{session_id}"

    def anchor_text(self, st, session_id: str) -> str:
        parts = [self.gsm.render_block(st)]
        if self.director:
            parts.append(self.director.render_director_state(session_id))
        if self.rule:
            parts.append(self.rule.render_rpg_snapshot(session_id))
        parts.append(G.STATE_GUARD)
        if self.rule:
            parts.append(RE.RPG_GUARD)
        if self.director:
            parts.append(DE.DIRECTOR_GUARD)
        return "\n\n".join(p for p in parts if p)

    # ---- core turn ------------------------------------------------------------------------
    async def play_turn(self, update: Update, user_text: str, *, session: dict,
                        log_player_text: str | None = None) -> None:
        msg = update.effective_message
        chat_id = update.effective_chat.id
        sid = session["session_id"]
        lock = self.lock(chat_id)
        if lock.locked():
            await msg.reply_text("⏳ 上一回合还在演绎，稍等。")
            return
        async with lock:
            st = self.gsm.get_or_init(sid)
            before = self.gsm.render_compact(st)
            if self.rule:
                cc = detect_character(user_text)
                if cc:
                    race, cls = cc
                    self.rule.create_character(sid, player_name_from_text(user_text, cls), race, cls, turn=0)
            turn = self.sessions.next_turn(sid)
            self.sessions.record(session, "player", turn, log_player_text or user_text,
                                 {"source": "telegram", "chat_id": chat_id})

            th = self.thread(chat_id, sid)
            self.store.append(th, "user", user_text)
            history = self.store.history(th, self.cfg.history_limit)
            anchor = self.rt.anchor_item(self.anchor_text(st, sid), depth=1, iid="world_state")
            raw_sink: list = []
            final = await stream_reply(msg, self.rt.stream(history, user_text, extra_items=[anchor]),
                                       placeholder_text="🎲 正在演绎……", postprocess=self.post,
                                       raw_sink=raw_sink)
            if not final:
                return
            raw = raw_sink[0] if raw_sink else final  # pre-strip text, carries 〔HP±N〕 tags
            self.store.append(th, "assistant", final)
            self.sessions.record(session, "gm", turn, final, {"model": self.cfg.model})

            try:
                changes = G.StateUpdater(self.gsm).update(st, turn, user_text, final)
                data = await SX.extract(st.persistent.to_dict(), user_text, final,
                                        model=self.cfg.extract_model, ollama_url=self.cfg.ollama_url,
                                        enabled=self.cfg.extract)
                llm_changes = SX.apply_extraction(self.gsm, st, turn, data)
                changes = (changes or []) + llm_changes
                rule_changes = self.rule.intake(sid, turn, changes, final) if self.rule else []
                # authoritative player-HP update from the DM's 〔HP±N〕 tags
                if self.rule:
                    rule_changes = (rule_changes or []) + self.rule.apply_hp_tags(sid, turn, raw)
                director_changes = (self.director.intake(sid, turn, changes, rule_changes, final, st)
                                    if self.director else [])
                conflicts = G.StateValidator(self.gsm).validate(st, turn, final)
                if self.rule:
                    conflicts = (conflicts or []) + (self.rule.validate_reply(sid, final) or [])
                st.narrative.data["last_conflicts"] = [
                    f"{c.get('category', '?')}:{c.get('entity', '?')}" for c in conflicts][:6]
                self.gsm.save(st)
                self.sessions.append_state_md(session, turn, before, self.gsm.render_compact(st),
                                              changes, conflicts, rule_changes, director_changes)
                log.info("[%s] turn=%d narrative=%d llm=%d rule=%d director=%d conflicts=%d",
                         self.cfg.name, turn, len(changes), len(llm_changes), len(rule_changes),
                         len(director_changes), len(conflicts))
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] state engine error: %s", self.cfg.name, exc)
            # Telegram can't show a side panel, so append a compact status panel
            # as a separate message. In the desktop app (chat_id == -1) the UI's
            # side status bar handles this, so we don't clutter the transcript.
            if self.rule and chat_id != -1:
                try:
                    await msg.reply_text(self.status_panel(st, sid))
                except Exception:  # noqa: BLE001
                    pass
            self.sessions.touch(sid)

    # ---- commands ----------------------------------------------------------------------------
    async def cmd_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(
            f"🎲 {self.rt.card.name} — 文字冒险\n\n"
            "/newgame — 开始新一局（旧局归档）\n/continue — 让叙事者推进一回合\n"
            "/session — 当前局信息\n/sessions — 最近游戏局\n/status — 当前世界状态\n"
            "/export_raw — 原始日志\n/export_script — 剧本格式\n/export_notes — 小说素材\n"
            "/endgame — 结束当前局\n/help — 帮助\n\n直接发文字进行你的行动（每轮自动记录）。")

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if self.sessions.active(update.effective_chat.id):
            await update.effective_message.reply_text("已有进行中的游戏局。直接发文字继续，或 /newgame 重开。")
        else:
            await self.cmd_newgame(update, ctx)

    async def cmd_newgame(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        archived = self.sessions.archive_active(chat_id)
        session = self.sessions.create(chat_id)
        sid = session["session_id"]
        self.gsm.init_state(sid)
        if self.rule:
            self.rule.init_state(sid)
        if self.director:
            self.director.init_state(sid)
        head = f"🎲 旧局已归档（{archived} 局），新一局开始！" if archived else "🎲 新一局开始！"
        await update.effective_message.reply_text(head)
        if self.cfg.newgame_prompt.strip():
            await self.play_turn(update, self.cfg.newgame_prompt.strip(), session=session,
                                 log_player_text="（玩家发起：新游戏）")
        else:
            greeting = self.rt.greeting()
            if self.cfg.first_mes_translate:
                greeting = self.post(greeting)
            self.store.append(self.thread(chat_id, sid), "assistant", greeting)
            self.sessions.record(session, "gm", 0, greeting, {"source": "first_mes"})
            for chunk in chunk_text(greeting):
                await update.effective_message.reply_text(chunk)

    async def cmd_continue(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        session = self.sessions.get_or_create(update.effective_chat.id)
        await self.play_turn(update, self.cfg.continue_prompt, session=session,
                             log_player_text="（玩家请求：继续推进剧情）")

    async def cmd_session(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.sessions.active(update.effective_chat.id)
        if not s:
            await update.effective_message.reply_text("📭 当前没有进行中的游戏局。用 /newgame 开始。")
            return
        n = len([t for t in self.sessions.turns(s["session_id"]) if t["speaker"] == "player"])
        await update.effective_message.reply_text(
            f"🎲 当前游戏局\n标题：{s.get('title')}\nSession ID：{s.get('session_id')}\n"
            f"角色：{s.get('character_name')}\n回合数：{n}\n创建：{s.get('created_at')}\n更新：{s.get('updated_at')}")

    async def cmd_sessions(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        rows = self.sessions.list_recent(update.effective_chat.id)
        if not rows:
            await update.effective_message.reply_text("📭 还没有任何游戏局。用 /newgame 开始第一局。")
            return
        zh = {"active": "进行中", "archived": "已归档", "completed": "已结束"}
        lines = ["📜 最近游戏局："] + [
            f"{i}. [{zh.get(r['status'], r['status'])}] {r['title']} — {r['character_name']}（{r['turn_count']} 回合）"
            for i, r in enumerate(rows, 1)]
        await update.effective_message.reply_text("\n".join(lines))

    async def cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.sessions.active(update.effective_chat.id)
        if not s:
            await update.effective_message.reply_text("📭 没有进行中的游戏局。")
            return
        st = self.gsm.get_or_init(s["session_id"])
        text = self.gsm.render_block(st)
        if self.rule:
            text += "\n\n" + self.rule.render_rpg_snapshot(s["session_id"])
        for chunk in chunk_text(text):
            await update.effective_message.reply_text(chunk)

    async def _export(self, update: Update, kind: str) -> None:
        s = self.sessions.active(update.effective_chat.id)
        if not s:
            await update.effective_message.reply_text("📭 没有当前游戏局。用 /newgame 开始。")
            return
        if kind == "raw":
            path, caption = self.sessions.raw_log_path(s["session_id"]), f"📄 原始日志（{s.get('title')}）"
        elif kind == "script":
            path = self.sessions.write_export(s, "script_log.md", self.sessions.build_script(s))
            caption = f"🎬 剧本版（{s.get('title')}）— 基础转换，未润色"
        else:
            path = self.sessions.write_export(s, "novel_notes.md", self.sessions.build_notes(s))
            caption = f"📖 小说素材（{s.get('title')}）— 基础提取，待润色"
        if not path.exists():
            await update.effective_message.reply_text("❌ 还没有日志内容。")
            return
        with path.open("rb") as f:
            await update.effective_message.reply_document(document=f, filename=path.name, caption=caption)

    async def cmd_export_raw(self, update: Update, _ctx) -> None:
        await self._export(update, "raw")

    async def cmd_export_script(self, update: Update, _ctx) -> None:
        await self._export(update, "script")

    async def cmd_export_notes(self, update: Update, _ctx) -> None:
        await self._export(update, "notes")

    async def cmd_endgame(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        s = self.sessions.active(update.effective_chat.id)
        if not s:
            await update.effective_message.reply_text("📭 没有进行中的游戏局。")
            return
        self.sessions.set_status(s["session_id"], "completed")
        n = len([t for t in self.sessions.turns(s["session_id"]) if t["speaker"] == "player"])
        await update.effective_message.reply_text(f"🏁 本局结束并归档：{s.get('title')}（{n} 回合）。/newgame 再来一局。")

    async def on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        session = self.sessions.get_or_create(update.effective_chat.id)
        await self.play_turn(update, text, session=session)

    # ---- wiring --------------------------------------------------------------------------------
    def build_application(self) -> Application:
        app = ApplicationBuilder().token(self.cfg.token).build()
        for names, fn in ((["start"], self.cmd_start), (["help"], self.cmd_help),
                          (["newgame"], self.cmd_newgame), (["continue"], self.cmd_continue),
                          (["session"], self.cmd_session), (["sessions"], self.cmd_sessions),
                          (["status"], self.cmd_status), (["export_raw"], self.cmd_export_raw),
                          (["export_script"], self.cmd_export_script),
                          (["export_notes", "novel"], self.cmd_export_notes),
                          (["endgame"], self.cmd_endgame)):
            app.add_handler(CommandHandler(names, fn))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        return app
