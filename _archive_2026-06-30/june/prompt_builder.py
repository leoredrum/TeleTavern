"""Modular RP prompt builder — Director Prompt layer + context summarization.

Architecture
============
  RPBuilder
    ├── character_card   (CharacterCard instance — raw card data)
    ├── director          (RP director rules — anti-purple-prose, length, etc.)
    ├── summarizer        (context compression when history grows long)
    └── hooks             (lorebook, memory — future extension points)

Public API
==========
  build_system_prompt(card, char_label, user_label, *, reply_language=None)
    → str  — full system prompt string

  build_messages(card, char_label, user_label, history, *, reply_language=None,
                 ollama_client=None, summary_threshold=20)
    → list[dict]  — [{"role":"system","content":...}, ...]
      Automatically summarises old history if len(history) > summary_threshold.
      Pass ollama_client to enable live summarisation; omit for plain truncation.

Design goals
===========
- Zero magic strings — everything is a named constant or method.
- Drop-in replacement for CharacterCard.build_system_prompt().
- Future extension: lorebook hook, vector-memory hook, emotion tracking.
  Just subclass RPBuilder and override the relevant _build_* method.
"""
from __future__ import annotations

import asyncio
import re
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, AsyncIterator

if TYPE_CHECKING:
    from character_card import CharacterCard


# ---------------------------------------------------------------------------
# Director Prompt constants
# ---------------------------------------------------------------------------

_REPEAT_TRIGGERS = [
    "脸红",
    "轻咬嘴唇",
    "耳边的低语",
    "贴近",
    "呼吸一滞",
    "指尖颤抖",
    "心跳加速",
    "脸红耳赤",
    "声音发颤",
    "凑近",
    "眼神闪躲",
    "不自觉地",
    "情不自禁",
    "欲言又止",
    "低下头",
    "垂下眼帘",
]

_REPEAT_BLOCK = (
    "禁止重复以下动作描写（同一回合内不得出现超过1次，全文不得连续出现2次以上）:\n"
    + "\n".join(f"  - {t}" for t in _REPEAT_TRIGGERS)
    + "\n\n"
)


def _build_director_prompt(char_label: str, user_label: str) -> str:
    """Static director layer — rules that apply to every turn."""
    return textwrap.dedent(
        f"""\
        [DIRECTOR — MANDATORY RULES]
        你是一场沉浸式角色扮演的导演兼演员。角色是 {char_label}，玩家是 {user_label}。

        ## 绝对禁止
        1. **不要列出选项**（"你想…还是…？"、"他可以…也可以…"）。直接演。
        2. **不要跳出角色做任何元评论**（不说"作为AI…"、"让我来扮演…"）。
        3. **不要在同一次回复中描写同一个微表情超过1次。**
        4. **不要让角色陷入相同的情绪循环**（连续3句以上相同的情绪表达视为循环）。
        5. **不要在回复中复述用户说过的话**（不要"你说…"、"你问我…"）。

        {_REPEAT_BLOCK}
        ## 叙事节奏
        - 回复长度：**80–280个中文字符**（不含角色名和引号）。
        - 节奏变化：轻松场景偏短（80–150字），情感高潮场景偏长（180–280字）。
        - 不要每句话都加动作描写。纯对话、纯动作、纯心理都可以。
        - 推进剧情：每次回复都要有**新的信息或转折**，哪怕很小。
          正确示例："他愣了一下，说……"（有反应+新信息）
          错误示例："她看着他，不说话。"（无推进）

        ## 角色主动性
        - {char_label} 必须主动推动对话：提问、反问、提出想法、做决定。
        - 不要只回应用户的动作——{char_label} 可以主动创造新场景、新情节。
        - 可以主动透露内心想法或秘密，推进关系深度。

        ## 中文优先
        - 所有描写、对话、心理独白均使用**中文**。
        - 不混入英文，除非角色在说英语时特意标注。

        ## 动作与对话格式
        - 动作用「*…*」或「……」（中文省略号）标注，不强制每句都有。
        - 对话直接写，不加引号。
        - 保持自然口语感，不要书面腔。

        [END DIRECTOR]
        """
    ).strip()


# ---------------------------------------------------------------------------
# Character card → structured sections
# ---------------------------------------------------------------------------

def _sub_placeholders(text: str, char_label: str, user_label: str) -> str:
    return (
        text.replace("{{char}}", char_label)
        .replace("{{Char}}", char_label)
        .replace("{{user}}", user_label)
        .replace("{{User}}", user_label)
    )


def _build_character_sections(
    card: "CharacterCard",
    char_label: str,
    user_label: str,
) -> str:
    """Render character card sections in SillyTavern order, with placeholders subbed."""
    sub = lambda s: _sub_placeholders(s, char_label, user_label)
    parts: list[str] = []

    def add(label: str, value: str) -> None:
        if value.strip():
            parts.append(f"[{label}]\n{sub(value)}")

    add("Description", card.description)
    add("Personality", card.personality)
    add("Scenario", card.scenario)
    add("Creator Notes", card.creator_notes)
    add("System", card.system_prompt)
    add("Post-History Instructions", card.post_history_instructions)

    if card.mes_example.strip():
        parts.append(
            f"[Example Dialogues — show {char_label}'s voice only; "
            f"do NOT continue these as the latest reply]\n{sub(card.mes_example)}"
        )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Context summarisation
# ---------------------------------------------------------------------------

# Threshold (message pairs) above which we summarise old history.
# Each pair ≈ ~100-200 tokens. At 20 pairs ≈ 2000-4000 tokens.
DEFAULT_SUMMARY_THRESHOLD = 20


async def _summarise_old_turns(
    messages: list[dict],
    ollama_client,
    char_label: str,
    user_label: str,
    cancel_event=None,
) -> str:
    """Compress the oldest half of history into a narrative summary."""
    half = len(messages) // 2
    old = messages[:half]
    new = messages[half:]

    history_blurb = "\n".join(
        f"[{m['role']}] {m['content'][:300]}" for m in old
    )

    summary_prompt = textwrap.dedent(
        f"""\
        你是一名角色扮演的情节记录员。请用3-5句话，把以下对话记录浓缩成一段连贯的情节摘要。
        用中文写，只写事实性摘要（谁做了什么、说了什么），不写内心独白或动作细节。
        读者看完摘要后应能无缝接上最新的对话。

        === 对话记录 ===
        {history_blurb}

        === 情节摘要（中文）===
        """
    ).strip()

    summary_parts: list[str] = []
    async for token in ollama_client.stream_chat(
        [{"role": "user", "content": summary_prompt}],
        cancel_event=cancel_event,
    ):
        summary_parts.append(token)

    summary_text = "".join(summary_parts).strip()
    # If summary is too long, trim it
    if len(summary_text) > 600:
        summary_text = summary_text[:600] + "…"

    # Reconstruct: summary + remaining history
    summarised = new.copy()
    summarised.insert(
        0,
        {
            "role": "system",
            "content": (
                f"[Prior Conversation Summary — for context]\n"
                f"{summary_text}\n"
                f"[End Summary]\n\n"
                "Read the above summary as established backstory. "
                f"Continue the roleplay as {char_label}, building on what happened before."
            ),
        },
    )
    return summarised


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

@dataclass
class RPBuilder:
    """Configurable RP prompt builder.

    Attributes
    ----------
    summary_threshold:
        Number of message dicts in history above which we trigger summarisation.
        Set to 0 to disable.
    """
    summary_threshold: int = DEFAULT_SUMMARY_THRESHOLD
    # Hooks — set these to add future capabilities
    lorebook_hook: callable | None = field(default=None, repr=False)
    memory_hook: callable | None = field(default=None, repr=False)

    # Internal
    _last_summary: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        card: "CharacterCard",
        char_label: str,
        user_label: str,
        *,
        reply_language: str | None = None,
    ) -> str:
        """Build the full system prompt string (drop-in for CharacterCard.build_system_prompt)."""
        sections = _build_character_sections(card, char_label, user_label)
        director = _build_director_prompt(char_label, user_label)

        parts: list[str] = []

        # Language override — highest priority
        if reply_language:
            parts.append(
                f"[LANGUAGE OVERRIDE — HIGHEST PRIORITY]\n"
                f"Always reply in {reply_language}. "
                f"This applies to narration, dialogue, inner thoughts, and action descriptions. "
                f"Do not switch languages for any reason.\n"
            )

        # Top-line role anchor
        parts.append(
            f"You are {char_label}, role-playing in an ongoing private chat with {user_label}. "
            f"Stay in character at all times. Speak and act as {char_label}, never break the "
            f"fourth wall, never mention that you are an AI or language model. Respond only as "
            f"{char_label} would — with the personality, voice, and mannerisms defined below.\n"
        )

        # Director rules
        parts.append(director)

        # Lorebook hook (future)
        if self.lorebook_hook:
            lore = self.lorebook_hook(char_label, user_label)
            if lore:
                parts.append(f"[Lorebook]\n{lore}")

        # Character card content
        if sections:
            parts.append(sections)

        # Memory hook (future)
        if self.memory_hook:
            mem = self.memory_hook(char_label, user_label)
            if mem:
                parts.append(f"[Character Memory]\n{mem}")

        return "\n\n".join(parts)

    async def build_messages(
        self,
        card: "CharacterCard",
        char_label: str,
        user_label: str,
        history: list[dict],
        *,
        reply_language: str | None = None,
        ollama_client=None,
        cancel_event=None,
    ) -> list[dict]:
        """Build the full messages list, with optional auto-summarisation.

        If len(history) > summary_threshold and ollama_client is provided,
        the oldest half of history is compressed into a narrative summary.
        """
        # Check if summarisation is needed
        if (
            self.summary_threshold > 0
            and len(history) >= self.summary_threshold
            and ollama_client is not None
        ):
            messages = await _summarise_old_turns(
                history, ollama_client, char_label, user_label, cancel_event
            )
        else:
            messages = history.copy()

        system_prompt = self.build_system_prompt(
            card, char_label, user_label, reply_language=reply_language
        )
        return [{"role": "system", "content": system_prompt}] + messages


# ---------------------------------------------------------------------------
# Convenience function — stateless, no async needed for basic use
# ---------------------------------------------------------------------------

_default_builder = RPBuilder()


def build_system_prompt(
    card: "CharacterCard",
    char_label: str,
    user_label: str,
    *,
    reply_language: str | None = None,
) -> str:
    """Standalone function — wraps _default_builder.build_system_prompt()."""
    return _default_builder.build_system_prompt(
        card, char_label, user_label, reply_language=reply_language
    )
