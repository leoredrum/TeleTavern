"""Engine — runs every enabled bot from bots/*.yaml in one asyncio loop."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
from pathlib import Path

from .config import BotConfig, ensure_data_dir, load_bots, validate_bot
from .modes.dialogue import DialogueBot
from .modes.rpg import RPGBot

log = logging.getLogger("tavern.engine")


def setup_logging(data_dir: Path, level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_tavern", False) for h in root.handlers):
        return
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(data_dir / "logs" / "engine.log", maxBytes=5_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh._tavern = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    sh._tavern = True  # type: ignore[attr-defined]
    root.addHandler(sh)
    for noisy in ("httpx", "httpcore", "telegram.request", "telegram.bot", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_bot(cfg: BotConfig, data_dir: Path):
    return RPGBot(cfg, data_dir) if cfg.mode == "rpg" else DialogueBot(cfg, data_dir)


def hot_add_character(bot, card_file: str, data_dir: Path) -> bool:
    """Add a card to a RUNNING DialogueBot without restarting (RPG bots need a restart)."""
    if not isinstance(bot, DialogueBot) or card_file in bot.runtimes:
        return isinstance(bot, DialogueBot)
    from .engine import CharacterRuntime
    bot.runtimes[card_file] = CharacterRuntime(bot.cfg, card_file, data_dir)
    if card_file not in bot.cfg.characters:
        bot.cfg.characters.append(card_file)
    return True


class Engine:
    def __init__(self, data_dir: Path):
        self.data_dir = ensure_data_dir(Path(data_dir))
        self.running: dict[str, object] = {}     # name -> telegram Application
        self.bots: dict[str, object] = {}        # name -> DialogueBot / RPGBot
        self.status: dict[str, str] = {}

    def plan(self) -> list[tuple[BotConfig, list[str]]]:
        return [(b, validate_bot(b, self.data_dir)) for b in load_bots(self.data_dir)]

    async def run(self, stop_event: asyncio.Event) -> None:
        apps = []
        for cfg, problems in self.plan():
            if not cfg.enabled:
                self.status[cfg.name] = "disabled"
                continue
            if cfg.kind == "local":
                self.status[cfg.name] = "local"
                continue
            fatal = [p for p in problems if not p.startswith("rpg 模式只使用")]
            if fatal:
                self.status[cfg.name] = "config error: " + "; ".join(fatal)
                log.error("[%s] not started: %s", cfg.name, "; ".join(fatal))
                continue
            try:
                bot = build_bot(cfg, self.data_dir)
                app = bot.build_application()
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                me = await app.bot.get_me()
                apps.append((cfg, app))
                self.running[cfg.name] = app
                self.bots[cfg.name] = bot
                self.status[cfg.name] = f"running as @{me.username}"
                log.info("[%s] polling as @%s (%s mode)", cfg.name, me.username, cfg.mode)
            except Exception as exc:  # noqa: BLE001
                self.status[cfg.name] = f"failed: {str(exc)[:160]}"
                log.exception("[%s] failed to start: %s", cfg.name, exc)
        if not apps:
            log.warning("no bot started")
        try:
            await stop_event.wait()
        finally:
            for cfg, app in apps:
                try:
                    await app.updater.stop()
                    await app.stop()
                    await app.shutdown()
                    self.status[cfg.name] = "stopped"
                except Exception as exc:  # noqa: BLE001
                    log.warning("[%s] shutdown error: %s", cfg.name, exc)
            self.running.clear()
            self.bots.clear()
            log.info("engine stopped")
