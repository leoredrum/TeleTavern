"""SillyTavern V2 / V3 character card parser — Phase 1 extended.

Reads a PNG with embedded chara tEXt chunk (V2) or a V3 JSON file.
V2 spec stores the data as a base64-encoded JSON string under the 'chara' keyword.
V3 spec is identical but the embedded JSON object declares spec='chara_card_v3'.

Phase 1 additions
================
- DepthPrompt: in-chat depth injection (mirrors SillyTavern Character's Note)
- ExtendedCharacterCard: full card with depth_prompts, tags, metadata

Reference: https://github.com/malfoyslastname/character-card-spec-v3
"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass
class CharacterCard:
    spec: str
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator: str = ""
    tags: list[str] = field(default_factory=list)
    # V3 only:
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    alternate_greetings: list[str] = field(default_factory=list)
    # V3 character book (lorebook)
    character_book: dict | None = None
    # Raw data for debugging
    raw: dict = field(default_factory=dict)

    def build_system_prompt(
        self,
        char_label: str,
        user_label: str,
        *,
        reply_language: str | None = None,
    ) -> str:
        """Build the system prompt the model will see.

        Mirrors SillyTavern's order:
        1. description (with {{char}}/{{user}} placeholders replaced)
        2. personality
        3. scenario
        4. (V3) creator_notes / system_prompt / post_history_instructions
        5. example_dialogs (mes_example)

        We keep {{char}} and {{user}} literals in the *visible* messages (so the
        model learns the speaker convention) but replace them in the system prompt
        to give the model a stable name to think with.

        reply_language: if set, an explicit "always reply in <lang>" instruction
        is prepended to the prompt. This overrides the language bias from
        English-language character cards. Use ISO language names (e.g. "Chinese",
        "Japanese", "English") — qwen3 maps these reliably.
        """
        parts: list[str] = []

        def sub(s: str) -> str:
            return (
                s.replace("{{char}}", char_label)
                .replace("{{Char}}", char_label)
                .replace("{{user}}", user_label)
                .replace("{{User}}", user_label)
            )

        if self.description.strip():
            parts.append(f"[Description]\n{sub(self.description)}")
        if self.personality.strip():
            parts.append(f"[Personality]\n{sub(self.personality)}")
        if self.scenario.strip():
            parts.append(f"[Scenario]\n{sub(self.scenario)}")
        if self.creator_notes.strip():
            parts.append(f"[Creator Notes]\n{sub(self.creator_notes)}")
        if self.system_prompt.strip():
            parts.append(f"[System]\n{sub(self.system_prompt)}")
        if self.post_history_instructions.strip():
            parts.append(f"[Post-History Instructions]\n{sub(self.post_history_instructions)}")
        if self.mes_example.strip():
            parts.append(
                f"[Example dialogues — these show {char_label}'s voice; do NOT continue them as the latest reply]\n"
                + sub(self.mes_example)
            )

        # Top-line role anchor so the model knows who it is.
        anchor = (
            f"You are {char_label}, role-playing in an ongoing private chat with {user_label}. "
            f"Stay in character at all times. Speak and act as {char_label}, never break the fourth wall, "
            f"never mention that you are an AI or language model. Respond only as {char_label} would, "
            f"with the personality, voice, and mannerisms defined below. Use markdown lightly for actions (*italics*) "
            f"and dialogue; avoid headers and bullet lists."
        )

        # Language override — placed BEFORE the anchor so it has higher priority
        # than the English-heavy character card content. Empty string or None = no override.
        lang_block = ""
        if reply_language:
            lang_block = (
                f"[LANGUAGE OVERRIDE — HIGHEST PRIORITY]\n"
                f"Always reply in {reply_language}. This applies to every reply, every narration, "
                f"every character thought. Do not switch to any other language even if example dialogues "
                f"or descriptions are written in a different language. Internal monologue and action "
                f"descriptions must also be in {reply_language}.\n\n"
            )

        return lang_block + anchor + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Depth Prompt — in-chat injection at a specific history depth
# ---------------------------------------------------------------------------

@dataclass
class DepthPrompt:
    """In-chat prompt injection at a specific history depth.

    Mirrors SillyTavern's "Character's Note" with @depth.

    Args:
        depth: How deep to inject.  0 = immediately after the last message
               (before user input), 1 = before second-last message, etc.
        role:  Which role to attribute this message as.
               "system" / "user" / "assistant"
        content: The text to inject at that depth.
        enabled: Can be toggled without removing the item.
    """
    depth: int = 0
    role: Literal["system", "user", "assistant"] = "system"
    content: str = ""
    enabled: bool = True

    def to_prompt_item(self, source_id: str = "char_depth_prompt") -> "PromptItem":
        """Convert to a PromptItem with IN_CHAT position."""
        from prompt_item import PromptItem, PromptPosition
        return PromptItem(
            id=f"{source_id}_depth_{self.depth}",
            role=self.role,
            content=self.content,
            enabled=self.enabled,
            position=PromptPosition.DEPTH_INJECTIONS,
            priority=0,
            depth=self.depth,
            source="character_card",
            metadata={"depth_prompt": True},
        )


# ---------------------------------------------------------------------------
# Extended Character Card
# ---------------------------------------------------------------------------

@dataclass
class ExtendedCharacterCard(CharacterCard):
    """Extended character card compatible with both V2 and V3.

    Adds fields that SillyTavern stores in the chat metadata or V3 spec:

    - depth_prompts: list of DepthPrompt for in-chat injections
    - metadata: arbitrary extra data loaded from the card JSON
    - personality_summary: brief personality summary (V3 Advanced Definitions)

    All new fields have safe defaults so old cards (V2, PNG without these
    fields) load and run normally.
    """
    depth_prompts: list[DepthPrompt] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    personality_summary: str = ""

    @classmethod
    def from_card(cls, card: CharacterCard) -> "ExtendedCharacterCard":
        """Upgrade a plain CharacterCard to ExtendedCharacterCard.

        Safe: if card is already an ExtendedCharacterCard, returns it unchanged.
        """
        if isinstance(card, cls):
            return card
        return cls(
            spec=card.spec,
            name=card.name,
            description=card.description,
            personality=card.personality,
            scenario=card.scenario,
            first_mes=card.first_mes,
            mes_example=card.mes_example,
            creator=card.creator,
            tags=card.tags,
            creator_notes=card.creator_notes,
            system_prompt=card.system_prompt,
            post_history_instructions=card.post_history_instructions,
            alternate_greetings=card.alternate_greetings,
            character_book=card.character_book,
            raw=card.raw,
            # Extended fields default to empty
            depth_prompts=[],
            metadata={},
            personality_summary="",
        )

    def get_depth_prompt_items(self) -> list["PromptItem"]:
        """Return all enabled DepthPrompts as PromptItems."""
        items = []
        for dp in self.depth_prompts:
            if dp.enabled:
                items.append(dp.to_prompt_item(f"char_depth_{self.name}"))
        return items

    def get_post_history_item(self, char_label: str, user_label: str) -> "PromptItem | None":
        """Return the Post-History Instructions as a PromptItem, or None."""
        from prompt_item import PromptItem, PromptPosition

        phi = self.post_history_instructions.strip()
        if not phi:
            return None

        def sub(s: str) -> str:
            return (
                s.replace("{{char}}", char_label)
                .replace("{{Char}}", char_label)
                .replace("{{user}}", user_label)
                .replace("{{User}}", user_label)
            )

        return PromptItem(
            id="post_history_instructions",
            role="system",
            content=sub(phi),
            enabled=True,
            position=PromptPosition.POST_HISTORY,
            priority=0,
            depth=0,
            source="character_card",
            metadata={"field": "post_history_instructions"},
        )

    def get_system_prompt_item(
        self, char_label: str, user_label: str
    ) -> "PromptItem | None":
        """Return the card's system_prompt (Prompt Override) as a PromptItem."""
        from prompt_item import PromptItem, PromptPosition

        sp = self.system_prompt.strip()
        if not sp:
            return None

        def sub(s: str) -> str:
            return (
                s.replace("{{char}}", char_label)
                .replace("{{Char}}", char_label)
                .replace("{{user}}", user_label)
                .replace("{{User}}", user_label)
            )

        return PromptItem(
            id="char_system_prompt_override",
            role="system",
            content=sub(sp),
            enabled=True,
            position=PromptPosition.SYSTEM_ANCHOR,
            priority=1,  # Runs before the default anchor (priority 0)
            depth=0,
            source="character_card",
            metadata={"field": "system_prompt"},
        )


def _parse_png_chara(path: Path) -> bytes:
    """Extract the raw chara payload bytes from a PNG."""
    data = path.read_bytes()
    pos = 8  # past PNG signature
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8].decode("ascii", errors="replace")
        chunk = data[pos + 8 : pos + 8 + length]
        if ctype == "tEXt":
            nul = chunk.index(b"\0")
            kw = chunk[:nul].decode("latin-1")
            val = chunk[nul + 1 :].decode("latin-1", errors="replace")
            if kw == "chara":
                return val.encode("latin-1")
        elif ctype == "iTXt":
            nul = chunk.index(b"\0")
            kw = chunk[:nul].decode("latin-1")
            rest = chunk[nul + 1 :]
            comp_flag = rest[0]
            text = rest[3:]
            if kw == "chara":
                if comp_flag:
                    text = zlib.decompress(text)
                return text
        elif ctype == "IEND":
            break
        pos += 12 + length
    raise ValueError(f"No 'chara' chunk found in {path}")


def load_character(path: str | Path) -> ExtendedCharacterCard:
    """Load a character card and return it as an ExtendedCharacterCard.

    Supports:
    - PNG files with embedded chara tEXt / iTXt chunk (V2 and V3)
    - Standalone JSON files (V2 and V3)

    The returned card is always ExtendedCharacterCard so callers can safely
    access .depth_prompts, .metadata, .personality_summary without None checks.
    """
    p = Path(path)
    if p.suffix.lower() == ".json":
        obj = json.loads(p.read_text("utf-8"))
    else:
        # PNG with embedded chara
        raw = _parse_png_chara(p)
        # V2 stores it as base64-encoded JSON, but some tools write raw JSON.
        try:
            text = raw.decode("utf-8").strip()
            if text.startswith("{"):
                obj = json.loads(text)
            else:
                obj = json.loads(base64.b64decode(raw))
        except (UnicodeDecodeError, ValueError):
            obj = json.loads(base64.b64decode(raw))

    spec = obj.get("spec", "chara_card_v2")
    data = obj.get("data", obj)

    # Parse depth_prompts from V3 character_book.depth_prompts or flat data
    depth_prompts: list[DepthPrompt] = []
    raw_depths = data.get("depth_prompts", [])
    if raw_depths:
        for dp in raw_depths:
            if isinstance(dp, dict):
                depth_prompts.append(DepthPrompt(
                    depth=dp.get("depth", 0),
                    role=dp.get("role", "system"),
                    content=dp.get("content", ""),
                    enabled=dp.get("enabled", True),
                ))

    # Personality summary (V3 Advanced Definitions)
    personality_summary = data.get("personality_summary", "")

    # Extra metadata: everything we don't explicitly handle
    metadata = {k: v for k, v in data.items()
                if k not in (
                    "spec", "name", "description", "personality",
                    "personality_summary", "scenario", "first_mes",
                    "mes_example", "creator", "tags", "creator_notes",
                    "system_prompt", "post_history_instructions",
                    "alternate_greetings", "character_book", "depth_prompts",
                    "data",
                )}

    card = CharacterCard(
        spec=spec,
        name=data.get("name", "Character"),
        description=data.get("description", ""),
        personality=data.get("personality", ""),
        scenario=data.get("scenario", ""),
        first_mes=data.get("first_mes", ""),
        mes_example=data.get("mes_example", ""),
        creator=data.get("creator", ""),
        tags=list(data.get("tags", [])),
        creator_notes=data.get("creator_notes", ""),
        system_prompt=data.get("system_prompt", ""),
        post_history_instructions=data.get("post_history_instructions", ""),
        alternate_greetings=list(data.get("alternate_greetings", [])),
        character_book=data.get("character_book"),
        raw=obj,
    )

    return ExtendedCharacterCard(
        spec=spec,
        name=data.get("name", "Character"),
        description=data.get("description", ""),
        personality=data.get("personality", ""),
        scenario=data.get("scenario", ""),
        first_mes=data.get("first_mes", ""),
        mes_example=data.get("mes_example", ""),
        creator=data.get("creator", ""),
        tags=list(data.get("tags", [])),
        creator_notes=data.get("creator_notes", ""),
        system_prompt=data.get("system_prompt", ""),
        post_history_instructions=data.get("post_history_instructions", ""),
        alternate_greetings=list(data.get("alternate_greetings", [])),
        character_book=data.get("character_book"),
        raw=obj,
        depth_prompts=depth_prompts,
        metadata=metadata,
        personality_summary=personality_summary,
    )