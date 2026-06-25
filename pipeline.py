"""PromptPipeline — modular SillyTavern-style prompt assembly.

Architecture
============
  PromptPipeline takes raw inputs (character card, history, user message, config)
  and produces a sorted list of PromptItems, then renders them into a
  messages list ready for the LLM API.

  Order (mirrors SillyTavern Prompt Manager):
    [0]  System Anchor         — "You are X, role-playing..."
    [1]  Language Override     — "Always reply in Chinese..."
    [2]  Director             — RP rules (no looping, advance narrative...)
    [3]  Lore Before           — Lorebook (before char defs)     [Phase 2]
    [4]  Character Defs        — Description / Personality / Scenario
    [5]  Lore After           — Lorebook (after char defs)      [Phase 2]
    [6]  Persona               — User persona description
    [7]  Lore Examples         — Lorebook (example position)     [Phase 2]
    [8]  Examples              — Example dialogues
    [9]  Summary               — Memory / conversation summary   [Phase 3]
    [10] Chat History          — Actual messages
    [11] Depth Injections      — In-chat depth prompts (interleaved)
    [12] Post-History Instruc.  — PHI (last, highest priority)

Phase 1 scope
============
  Full pipeline skeleton with Director, Character Defs, Examples, Chat History,
  Depth Injections, and Post-History Instructions.  Lorebook and Summary are
  stubbed (Phase 2 / 3).  Every item is a PromptItem.

Debug
=====
  Set DEBUG_PROMPT=True or call .debug_report() to get a full item listing
  with token estimates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from character_card import ExtendedCharacterCard

from prompt_item import PromptItem, PromptPosition

log = logging.getLogger("prompt-pipeline")


# ---------------------------------------------------------------------------
# Global debug flag — flip to True to enable verbose item logging
# ---------------------------------------------------------------------------
DEBUG_PROMPT = False


def _sub_placeholders(text: str, char_label: str, user_label: str) -> str:
    return (
        text.replace("{{char}}", char_label)
        .replace("{{Char}}", char_label)
        .replace("{{user}}", user_label)
        .replace("{{User}}", user_label)
    )


# ---------------------------------------------------------------------------
# PromptPipeline
# ---------------------------------------------------------------------------

@dataclass
class Persona:
    """User persona — mirrors SillyTavern's User Persona concept.

    Phase 1 only uses .description.  Future phases add lore, memory, etc.
    """
    name: str = "User"
    description: str = ""


@dataclass
class PipelineContext:
    """Context passed to every builder method during pipeline execution.

    Collects all inputs in one place so builder methods are stateless.
    """
    char_label: str
    user_label: str
    reply_language: str | None = None
    persona: Persona | None = None
    generation_type: str = "normal"   # "normal" | "continue" | "swipe" | ...


@dataclass
class PromptPipeline:
    """Assembles a prompt from modular PromptItems.

    Args:
        card: ExtendedCharacterCard for the active character
        director_cfg: DirectorConfig (None = disabled)
        persona: Persona for the user (None = minimal)
        char_label: Name substitution for {{char}}
        user_label: Name substitution for {{user}}
        reply_language: Language override (None = model decides)
        lorebook: Lorebook engine instance (Phase 2, None for Phase 1)
        memory_manager: MemoryManager instance (Phase 3, None for Phase 1)
    """

    card: "ExtendedCharacterCard"
    director_cfg: Any = None          # DirectorConfig or None
    persona: Persona | None = None
    char_label: str = "Character"
    user_label: str = "User"
    reply_language: str | None = None
    lorebook: Any = None               # Phase 2
    memory_manager: Any = None         # Phase 3

    def build_items(
        self,
        history: list[dict],
        user_message: str,
    ) -> list[PromptItem]:
        """Build all PromptItems for one generation turn.

        Returns items in pipeline order (sorted by position + priority).
        Depth Injection items are included but not yet interleaved.
        """
        ctx = PipelineContext(
            char_label=self.char_label,
            user_label=self.user_label,
            reply_language=self.reply_language,
            persona=self.persona,
        )

        items: list[PromptItem] = []

        # ── 0: System Anchor ────────────────────────────────────────────────
        items.append(self._build_system_anchor(ctx))

        # ── 1: Language Override ────────────────────────────────────────────
        lang_item = self._build_language_override(ctx)
        if lang_item:
            items.append(lang_item)

        # ── 2: Director ──────────────────────────────────────────────────────
        director_item = self._build_director(ctx)
        if director_item:
            items.append(director_item)

        # ── 3/5: Lorebook (Phase 2 — stubbed) ──────────────────────────────
        # items.extend(self._build_lorebook(ctx, history))  # Phase 2

        # ── 4: Character Definitions ─────────────────────────────────────────
        items.extend(self._build_character_defs(ctx))

        # ── 6: Persona ──────────────────────────────────────────────────────
        persona_item = self._build_persona(ctx)
        if persona_item:
            items.append(persona_item)

        # ── 8: Example Dialogues ────────────────────────────────────────────
        items.extend(self._build_examples(ctx))

        # ── 9: Summary (Phase 3 — stubbed) ─────────────────────────────────
        # items.extend(self._build_summary(ctx))  # Phase 3

        # ── 10: Chat History ────────────────────────────────────────────────
        items.extend(self._build_chat_history(history, ctx))

        # ── 11: Depth Injections ────────────────────────────────────────────
        items.extend(self._build_depth_injections(ctx))

        # ── 12: Post-History Instructions ───────────────────────────────────
        phi_item = self._build_post_history(ctx)
        if phi_item:
            items.append(phi_item)

        # ── Final: user message as assistant trigger ───────────────────────
        # (actually appended by bot.py; pipeline returns items only)

        if DEBUG_PROMPT:
            self._log_items(items)

        return items

    def assemble(
        self,
        history: list[dict],
        user_message: str,
    ) -> list[dict]:
        """Build full messages list for the LLM API.

        Returns a list of {"role": "...", "content": "..."} dicts,
        ready to pass to ollama_client.stream_chat().
        """
        items = self.build_items(history, user_message)

        # Sort: absolute items by position+priority, depth items separate
        absolute_items = sorted(
            [i for i in items if i.is_absolute],
            key=lambda i: (i.position.value, i.priority)
        )
        depth_items = sorted(
            [i for i in items if i.is_in_chat],
            key=lambda i: (i.depth, i.priority)
        )

        messages: list[dict] = []

        # 1. Add all absolute items (system role = rendered as text)
        for item in absolute_items:
            if item.enabled and item.content:
                messages.append({"role": item.role, "content": item.content})

        # 2. Add chat history, interleaving depth injections at correct depth
        # depth=0 means after last message (before the new user message)
        # depth=N means before the N-th-from-last message
        history_msgs = [m for m in history if m.get("content")]
        depth_idx = 0  # pointer into depth_items

        if depth_items:
            depth_map: dict[int, list[PromptItem]] = {}
            for di in depth_items:
                if di.enabled and di.content:
                    depth_map.setdefault(di.depth, []).append(di)

            for i, msg in enumerate(history_msgs):
                effective_depth = len(history_msgs) - i - 1
                if effective_depth in depth_map:
                    for di in depth_map[effective_depth]:
                        messages.append({"role": di.role, "content": di.content})
                messages.append({"role": msg["role"], "content": msg["content"]})
        else:
            for msg in history_msgs:
                if msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})

        return messages

    def debug_report(
        self,
        history: list[dict],
        user_message: str,
        chars_per_token: float = 2.0,
    ) -> str:
        """Return a multi-line debug report of all items and token estimates."""
        items = self.build_items(history, user_message)
        lines = ["=== PROMPT DEBUG REPORT ===", f"char={self.char_label}  user={self.user_label}"]
        lines.append(f"{'Pos':>3}  {'ID':<30}  {'Role':<8}  {'Depth':>5}  {'TokEst':>6}  {'Ena':>4}  {'Source'}")
        lines.append("-" * 100)

        total_tokens = 0
        for item in sorted(items, key=lambda i: (i.position.value, i.priority)):
            tok = item.token_estimate(chars_per_token)
            total_tokens += tok
            lines.append(
                f"{item.position.value:3d}  "
                f"{item.id:<30}  "
                f"{item.role:<8}  "
                f"{item.depth:5d}  "
                f"{tok:6d}  "
                f"{str(item.enabled):>4}  "
                f"{item.source}"
            )

        lines.append("-" * 100)
        lines.append(f"{'TOTAL TOKENS (est)':>66}  {total_tokens:6d}")
        lines.append("=== END REPORT ===")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Builder methods — each creates specific PromptItems
    # -------------------------------------------------------------------------

    def _build_system_anchor(self, ctx: PipelineContext) -> PromptItem:
        anchor = (
            f"You are {ctx.char_label}, role-playing in an ongoing private chat with {ctx.user_label}. "
            f"Stay in character at all times. Speak and act as {ctx.char_label}, never break the fourth wall, "
            f"never mention that you are an AI or language model. Respond only as {ctx.char_label} would, "
            f"with the personality, voice, and mannerisms defined below."
        )
        return PromptItem(
            id="system_anchor",
            role="system",
            content=anchor,
            enabled=True,
            position=PromptPosition.SYSTEM_ANCHOR,
            priority=0,
            depth=0,
            source="pipeline",
            metadata={"generated": True},
        )

    def _build_language_override(self, ctx: PipelineContext) -> PromptItem | None:
        if not ctx.reply_language:
            return None
        return PromptItem(
            id="language_override",
            role="system",
            content=(
                f"[LANGUAGE OVERRIDE — HIGHEST PRIORITY]\n"
                f"Always reply in {ctx.reply_language}. "
                f"All narration, dialogue, inner thoughts, and action descriptions must be in {ctx.reply_language}. "
                f"Do not switch languages for any reason.\n"
            ),
            enabled=True,
            position=PromptPosition.LANGUAGE_OVERRIDE,
            priority=0,
            depth=0,
            source="pipeline",
        )

    def _build_director(self, ctx: PipelineContext) -> PromptItem | None:
        if self.director_cfg is None:
            return None
        from director import build_director_item
        item = build_director_item(ctx.char_label, ctx.user_label, self.director_cfg)
        return item

    def _build_character_defs(self, ctx: PipelineContext) -> list[PromptItem]:
        """Character card sections: Description / Personality / Scenario."""
        card = self.card
        parts: list[str] = []

        def add(label: str, value: str) -> None:
            if value.strip():
                parts.append(f"[{label}]\n{_sub_placeholders(value, ctx.char_label, ctx.user_label)}")

        add("Description", card.description)
        add("Personality", card.personality)
        add("Scenario", card.scenario)
        add("Creator Notes", card.creator_notes)

        # V3 system_prompt override — add it here if present
        # (it already gets its own PromptItem via .get_system_prompt_item())
        # so we don't duplicate it here

        if not parts:
            return []

        content = "\n\n".join(parts)
        return [
            PromptItem(
                id="char_definitions",
                role="system",
                content=content,
                enabled=True,
                position=PromptPosition.CHARACTER_DEFS,
                priority=0,
                depth=0,
                source="character_card",
                metadata={
                    "has_description": bool(card.description.strip()),
                    "has_personality": bool(card.personality.strip()),
                    "has_scenario": bool(card.scenario.strip()),
                    "has_creator_notes": bool(card.creator_notes.strip()),
                },
            )
        ]

    def _build_persona(self, ctx: PipelineContext) -> PromptItem | None:
        if ctx.persona is None or not ctx.persona.description.strip():
            return None
        return PromptItem(
            id="user_persona",
            role="system",
            content=f"[User Persona — {ctx.user_label}]\n{ctx.persona.description}",
            enabled=True,
            position=PromptPosition.PERSONA,
            priority=0,
            depth=0,
            source="persona",
        )

    def _build_examples(self, ctx: PipelineContext) -> list[PromptItem]:
        """Example dialogues from the character card."""
        raw = self.card.mes_example.strip()
        if not raw:
            return []
        subbed = _sub_placeholders(raw, ctx.char_label, ctx.user_label)
        return [
            PromptItem(
                id="example_dialogues",
                role="system",
                content=(
                    f"[Example Dialogues — show {ctx.char_label}'s voice only; "
                    f"do NOT continue these as the latest reply]\n{subbed}"
                ),
                enabled=True,
                position=PromptPosition.EXAMPLES,
                priority=0,
                depth=0,
                source="character_card",
                metadata={"field": "mes_example"},
            )
        ]

    def _build_chat_history(self, history: list[dict], ctx: PipelineContext) -> list[PromptItem]:
        """Wrap raw history messages as PromptItems with CHAT_HISTORY position."""
        items = []
        for i, msg in enumerate(history):
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if not content:
                continue
            items.append(PromptItem(
                id=f"history_{i}",
                role=role,
                content=content,
                enabled=True,
                position=PromptPosition.CHAT_HISTORY,
                priority=0,
                depth=0,
                source="chat_history",
                metadata={"index": i},
            ))
        return items

    def _build_depth_injections(self, ctx: PipelineContext) -> list[PromptItem]:
        """Depth Prompt items from the character card."""
        return self.card.get_depth_prompt_items()

    def _build_post_history(self, ctx: PipelineContext) -> PromptItem | None:
        """Post-History Instructions from the character card."""
        return self.card.get_post_history_item(ctx.char_label, ctx.user_label)

    def _log_items(self, items: list[PromptItem]) -> None:
        for item in sorted(items, key=lambda i: (i.position.value, i.priority)):
            log.info("PROMPT_ITEM %s", item.debug_summary())
