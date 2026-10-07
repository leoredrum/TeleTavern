"""Local chat — drive the same DialogueBot / RPGBot logic from the desktop window.

A LocalUpdate mimics the handful of python-telegram-bot attributes the modes
use (effective_chat.id, effective_message.text/reply_text/reply_document,
placeholder.edit_text), so Telegram and in-app chat share one code path,
storage and world state. Local conversations use chat_id -1.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from .config import BotConfig, load_bots
from .modes.dialogue import DialogueBot
from .modes.rpg import RPGBot

log = logging.getLogger("tavern.local")
LOCAL_CHAT_ID = -1


class Conversation:
    def __init__(self):
        self.messages: list[dict] = []     # {role: user|assistant|system|file, text, ts}
        self.busy = False
        self.error = ""
        self.version = 0

    def add(self, role: str, text: str, **extra) -> dict:
        m = {"role": role, "text": text, "ts": time.time(), **extra}
        self.messages.append(m)
        self.version += 1
        return m

    def snapshot(self) -> dict:
        return {"messages": self.messages[-200:], "busy": self.busy, "error": self.error, "version": self.version}


class _Reply:
    """Object returned by reply_text(); edit_text() rewrites that message in place."""

    def __init__(self, convo: Conversation, msg: dict):
        self.convo, self.msg = convo, msg

    async def edit_text(self, text: str, **_kw):
        self.msg["text"] = text
        self.convo.version += 1


class _Message:
    def __init__(self, convo: Conversation, text: str):
        self.convo = convo
        self.text = text
        self.chat = SimpleNamespace(id=LOCAL_CHAT_ID)

    async def reply_text(self, text: str, **_kw):
        return _Reply(self.convo, self.convo.add("assistant", text))

    async def reply_document(self, document=None, filename: str = "", caption: str = "", **_kw):
        path = getattr(document, "name", "")
        self.convo.add("file", caption or filename, path=str(path), filename=filename)
        return None


class LocalUpdate:
    def __init__(self, convo: Conversation, text: str):
        self.effective_message = _Message(convo, text)
        self.message = self.effective_message
        self.effective_chat = SimpleNamespace(id=LOCAL_CHAT_ID)
        self.callback_query = None


class LocalChat:
    """Per-bot local conversations running on a private asyncio loop thread."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.bots: dict[str, object] = {}
        self.convos: dict[str, Conversation] = {}
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="tavern-local", daemon=True)
        self.thread.start()

    # ---- helpers --------------------------------------------------------------------
    def _cfg(self, name: str) -> BotConfig:
        for b in load_bots(self.data_dir):
            if b.name == name:
                return b
        raise KeyError(f"bot not found: {name}")

    def bot(self, name: str):
        if name not in self.bots:
            cfg = self._cfg(name)
            self.bots[name] = RPGBot(cfg, self.data_dir) if cfg.mode == "rpg" else DialogueBot(cfg, self.data_dir)
        return self.bots[name]

    def convo(self, name: str) -> Conversation:
        return self.convos.setdefault(name, Conversation())

    def reload(self, name: str | None = None) -> None:
        if name:
            self.bots.pop(name, None)
        else:
            self.bots.clear()

    def _run(self, name: str, coro_factory, user_text: str | None = None) -> None:
        convo = self.convo(name)
        if convo.busy:
            return
        convo.busy = True
        convo.error = ""
        if user_text is not None:
            convo.add("user", user_text)

        async def task():
            try:
                await coro_factory()
            except Exception as exc:  # noqa: BLE001
                log.exception("[local:%s] %s", name, exc)
                convo.error = str(exc)[:300]
                convo.add("system", f"❌ {str(exc)[:200]}")
            finally:
                convo.busy = False
                convo.version += 1

        asyncio.run_coroutine_threadsafe(task(), self.loop)

    # ---- public --------------------------------------------------------------------------
    def info(self, name: str) -> dict:
        bot = self.bot(name)
        cfg = bot.cfg
        out = {"name": name, "mode": cfg.mode, "characters": [], "current": None, "model": cfg.model}
        if isinstance(bot, DialogueBot):
            out["characters"] = [{"file": cf, "name": rt.card.name} for cf, rt in bot.menu()]
            b = bot.store.get_binding(LOCAL_CHAT_ID)
            out["current"] = b["card_file"] if b else (out["characters"][0]["file"] if len(out["characters"]) == 1 else None)
        else:
            out["characters"] = [{"file": bot.rt.card_file, "name": bot.rt.card.name}]
            out["current"] = bot.rt.card_file
            s = bot.sessions.active(LOCAL_CHAT_ID)
            out["session"] = {"id": s["session_id"], "title": s.get("title")} if s else None
        return out

    def rpg_state(self, name: str) -> dict | None:
        """Structured world + player state for the desktop app's side status bar.
        Returns None if the bot isn't RPG or has no active local session. Reflects
        whatever state exists, so it adapts to any scenario / world book."""
        bot = self.bot(name)
        if not isinstance(bot, RPGBot):
            return None
        s = bot.sessions.active(LOCAL_CHAT_ID)
        if not s:
            return None
        sid = s["session_id"]
        st = bot.gsm.get_or_init(sid)
        p = st.persistent.to_dict()
        n = st.narrative.to_dict()
        out = {
            "session_title": s.get("title"),
            "location": p.get("location"), "room": p.get("room") or p.get("area"),
            "scene": p.get("current_scene"), "objective": p.get("current_objective"),
            "quest": p.get("current_quest"),
            "environment": p.get("environment"), "weather": p.get("weather"),
            "time_of_day": p.get("time_of_day"),
            "scene_summary": n.get("scene_summary"),
            "enemies": [{"name": e.get("name"), "status": e.get("status")} for e in (p.get("enemies") or [])],
            "npcs": [{"name": e.get("name"), "status": e.get("status")} for e in (p.get("npcs") or [])],
            "loot": [{"name": l.get("name"), "taken": bool(l.get("taken"))} for l in (p.get("loot") or [])],
            "flags": bot.gsm.all_flags(sid),
            "player": None, "inventory": [], "quests": [],
        }
        if bot.rule:
            snap = bot.rule.get_or_init(sid)
            pl = snap.player
            player = {
                "name": pl.get("name"), "race": pl.get("race"), "class": pl.get("class"),
                "level": pl.get("level"), "xp": pl.get("xp"), "xp_to_next": pl.get("xp_to_next"),
                "hp": pl.get("hp"), "max_hp": pl.get("max_hp"), "ac": pl.get("ac"),
                "stats": pl.get("stats") or {}, "conditions": pl.get("conditions") or [],
                "abilities": pl.get("abilities") or [],
                "downed": int(pl.get("hp", 0)) <= 0 or "倒下" in (pl.get("conditions") or []),
            }
            try:
                from .rpg.rpg_engine import coins_str
                player["gold"] = coins_str(int((snap.economy or {}).get("copper", 0)))
            except Exception:  # noqa: BLE001
                player["gold"] = None
            out["player"] = player
            out["inventory"] = [{"name": r.get("name"), "qty": r.get("qty"),
                                 "equipped": bool(r.get("equipped"))} for r in bot.rule.inventory(sid)]
            out["quests"] = [{"title": q.get("title"), "objective": q.get("objective"),
                              "status": q.get("status"), "progress": q.get("progress", 0)}
                             for q in snap.quests if q.get("status") in ("active", "available")]
        return out

    def send(self, name: str, text: str) -> None:
        bot = self.bot(name)
        text = (text or "").strip()
        if not text:
            return
        upd = LocalUpdate(self.convo(name), text)
        self._run(name, lambda: bot.on_text(upd, SimpleNamespace(args=[])), user_text=text)

    def command(self, name: str, cmd: str, arg: str = "") -> None:
        bot = self.bot(name)
        convo = self.convo(name)
        handler = {
            "start": getattr(bot, "cmd_start", None), "newgame": getattr(bot, "cmd_newgame", None),
            "reset": getattr(bot, "cmd_reset", None), "continue": getattr(bot, "cmd_continue", None),
            "status": getattr(bot, "cmd_status", None), "where": getattr(bot, "cmd_where", None),
            "session": getattr(bot, "cmd_session", None), "endgame": getattr(bot, "cmd_endgame", None),
            "export_script": getattr(bot, "cmd_export_script", None),
            "export_notes": getattr(bot, "cmd_export_notes", None), "debug": getattr(bot, "cmd_debug", None),
        }.get(cmd)
        if handler is None:
            convo.add("system", f"该模式不支持 /{cmd}")
            return
        upd = LocalUpdate(convo, f"/{cmd} {arg}".strip())
        ctx = SimpleNamespace(args=[a for a in arg.split() if a])
        self._run(name, lambda: handler(upd, ctx), user_text=f"/{cmd}" + (f" {arg}" if arg else ""))

    def select_character(self, name: str, card_file: str) -> None:
        bot = self.bot(name)
        if not isinstance(bot, DialogueBot) or card_file not in bot.runtimes:
            return
        rt = bot.runtimes[card_file]
        bot.store.set_binding(LOCAL_CHAT_ID, card_file, rt.card.name)
        convo = self.convo(name)
        convo.messages.clear()
        convo.add("system", f"已切换到 {rt.card.name}，开始新对话。")
        upd = LocalUpdate(convo, "/start")
        self._run(name, lambda: bot.start_fresh(upd, rt))

    def history(self, name: str) -> dict:
        """Load the persisted thread so the transcript survives app restarts."""
        bot = self.bot(name)
        convo = self.convo(name)
        if convo.messages:
            return convo.snapshot()
        if isinstance(bot, DialogueBot):
            rt = bot.bound(LOCAL_CHAT_ID)
            if rt:
                for m in bot.store.history(bot.thread(LOCAL_CHAT_ID, rt), 60):
                    convo.add(m["role"], m["content"])
        else:
            s = bot.sessions.active(LOCAL_CHAT_ID)
            if s:
                for m in bot.store.history(bot.thread(LOCAL_CHAT_ID, s["session_id"]), 60):
                    convo.add(m["role"], m["content"])
        return convo.snapshot()

    def clear_view(self, name: str) -> None:
        self.convos.pop(name, None)

    def clear(self, name: str) -> int:
        """Delete the persisted transcript of the current local-chat thread and reset the
        on-screen view. Mirrors history()'s thread resolution so it works in both modes.
        Returns the number of messages removed."""
        bot = self.bot(name)
        n = 0
        if isinstance(bot, DialogueBot):
            rt = bot.bound(LOCAL_CHAT_ID)
            if rt:
                n = bot.store.reset(bot.thread(LOCAL_CHAT_ID, rt))
        else:
            s = bot.sessions.active(LOCAL_CHAT_ID)
            if s:
                n = bot.store.reset(bot.thread(LOCAL_CHAT_ID, s["session_id"]))
        self.convos.pop(name, None)
        return n
