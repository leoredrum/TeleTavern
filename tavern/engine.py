"""CharacterRuntime — one loaded character: card + World Info + pipeline + LLM client.

Both bot modes build their prompt through here so SillyTavern-equivalent
assembly (card defs, lore, examples, history, depth injections, PHI) is shared.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import AsyncIterator, Iterable

from character_card import ExtendedCharacterCard, load_character
from director import DirectorConfig
from ollama_client import OllamaClient
from pipeline import PromptPipeline, _sub_placeholders
from prompt_item import PromptItem, PromptPosition
from story_engine import StoryEngine

from .config import BotConfig
from .worldinfo import WorldInfo

log = logging.getLogger("tavern.engine")


class CharacterRuntime:
    def __init__(self, bot: BotConfig, card_file: str, data_dir: Path):
        self.bot = bot
        self.card_file = card_file
        self.card: ExtendedCharacterCard = load_character(data_dir / "characters" / card_file)
        world_files = [data_dir / "worlds" / w for w in bot.worlds if (data_dir / "worlds" / w).exists()]
        self.worldinfo = WorldInfo.from_sources(
            card_book=self.card.character_book, world_files=world_files, card_name=self.card.name,
            scan_depth=2, budget_tokens=max(512, int(bot.max_context * 0.25)))
        director_cfg = DirectorConfig() if card_file in (bot.director_characters or []) else None
        self.pipeline = PromptPipeline(
            card=self.card,
            director_cfg=director_cfg,
            persona=None,
            char_label=self.card.name,
            user_label=bot.user_label,
            # a custom full-text override replaces the generic one
            reply_language=None if bot.language_override else _language_name(bot.reply_language),
            lorebook=self.worldinfo,
            story_engine=StoryEngine() if (bot.story_engine and bot.mode == "dialogue") else None,
            max_context_tokens=bot.max_context,
            reserve_tokens=bot.max_tokens + 512,
        )
        self.client = OllamaClient(bot.ollama_url, bot.model, max_tokens=bot.max_tokens,
                                   temperature=bot.temperature,
                                   repeat_penalty=bot.repeat_penalty, repeat_last_n=bot.repeat_last_n,
                                   presence_penalty=bot.presence_penalty,
                                   frequency_penalty=bot.frequency_penalty,
                                   top_k=bot.top_k, min_p=bot.min_p)
        log.info("[%s] loaded %s (%s) — worldinfo: %s", bot.name, self.card.name, card_file,
                 self.worldinfo.summary())

    # ---- prompt pieces --------------------------------------------------------
    def language_item(self) -> PromptItem | None:
        if not self.bot.language_override:
            return None
        return PromptItem(id="language_override_custom", role="system",
                          content=self.bot.language_override.strip(), enabled=True,
                          position=PromptPosition.LANGUAGE_OVERRIDE, priority=0, depth=0,
                          source="bot_config")

    @staticmethod
    def anchor_item(text: str, depth: int = 1, iid: str = "anchor") -> PromptItem:
        """A block injected IN_CHAT at `depth` (1 = right before the newest user message)."""
        return PromptItem(id=iid, role="system", content=text.strip(), enabled=True,
                          position=PromptPosition.DEPTH_INJECTIONS, priority=-100, depth=depth,
                          source="anchor")

    def greeting(self, index: int | None = None) -> str:
        """first_mes, or alternate greeting #index (1-based); 0/None = first_mes."""
        opts = [self.card.first_mes or ""] + list(self.card.alternate_greetings or [])
        opts = [o for o in opts if o and o.strip()]
        if not opts:
            return f"*{self.card.name} 看着你。*"
        if index is None or index <= 0 or index > len(opts):
            text = opts[0]
        else:
            text = opts[index - 1]
        return _sub_placeholders(text, self.card.name, self.bot.user_label)

    def random_greeting(self) -> str:
        n = 1 + len(self.card.alternate_greetings or [])
        return self.greeting(random.randint(1, n))

    # ---- generation ----------------------------------------------------------
    def build_messages(self, history: list[dict], user_message: str,
                       extra_items: Iterable[PromptItem] = (), story_state=None) -> list[dict]:
        self.pipeline.story_state = story_state
        self.pipeline.extra_items = [self.language_item(), *extra_items]
        return self.pipeline.assemble(history, user_message)

    async def stream(self, history: list[dict], user_message: str,
                     extra_items: Iterable[PromptItem] = (), story_state=None,
                     cancel_event=None) -> AsyncIterator[str]:
        messages = self.build_messages(history, user_message, extra_items, story_state)
        async for piece in self.client.stream_chat(messages, cancel_event=cancel_event):
            yield piece

    def debug_prompt(self, history: list[dict], user_message: str) -> str:
        msgs = self.build_messages(history, user_message)
        lines = [f"[{m['role']}] {len(m['content'])}c: {m['content'][:70].replace(chr(10), ' ')}"
                 for m in msgs]
        wi = getattr(self.pipeline, "last_worldinfo", None)
        if wi is not None:
            lines.append(f"worldinfo: {len(wi.activated)} active, {wi.tokens_used} tok, "
                         f"{len(wi.dropped_for_budget)} dropped")
        return "\n".join(lines)


def _language_name(code: str) -> str:
    return {"zh-cn": "Simplified Chinese", "zh": "Chinese", "zh-tw": "Traditional Chinese",
            "en": "English", "ja": "Japanese", "ko": "Korean"}.get((code or "").lower(), code or "Chinese")
