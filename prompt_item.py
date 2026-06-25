"""PromptItem — the atomic unit of the SillyTavern-style prompt pipeline.

Each PromptItem is a self-contained prompt fragment with its own
metadata, role, position, and budget tracking.

Phase 1 scope
============
- id, role, content, enabled, position, priority, depth
- token_budget_estimate, source, metadata
- Position enum: ABSOLUTE | IN_CHAT
- Basic sorting and debug helpers

Future phases extend with: triggers, resolver callable, sticky/cooldown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class PromptPosition(IntEnum):
    """Where this item lives in the assembled prompt.

    Items with LOWER position numbers are placed earlier.
    Position determines the "vertical" location in the prompt stack.
    IN_CHAT items are interleaved into chat history at their depth.
    """
    SYSTEM_ANCHOR = 0          # Top-level role anchor
    LANGUAGE_OVERRIDE = 1      # Language instruction (highest priority signal)
    DIRECTOR = 2               # RP Director rules
    LORE_BEFORE = 3            # Lorebook entries (before char defs)
    CHARACTER_DEFS = 4          # Description / Personality / Scenario
    LORE_AFTER = 5             # Lorebook entries (after char defs)
    PERSONA = 6                # User persona description
    LORE_EXAMPLES = 7          # Lorebook entries (example position)
    EXAMPLES = 8               # Example dialogues
    SUMMARY = 9                # Memory / conversation summary
    CHAT_HISTORY = 10         # Actual chat messages
    DEPTH_INJECTIONS = 11      # In-chat depth injections (placeholder marker)
    POST_HISTORY = 12          # Post-history instructions (last, highest priority)


@dataclass
class PromptItem:
    """A single prompt fragment in the pipeline."""

    # Identification
    id: str                       # Unique identifier, e.g. "char_description", "phi_1"

    # Content
    role: str                     # "system" | "user" | "assistant"
    content: str = ""             # The actual text content

    # Behavior
    enabled: bool = True           # Can be toggled without removing the item
    position: PromptPosition = PromptPosition.CHARACTER_DEFS
    priority: int = 0             # Tiebreaker when position is the same
    depth: int = 0               # For IN_CHAT: 0=last msg, 1=second-last, etc.
    token_budget: float = 0.0     # Max token share (0=unlimited) — future use

    # Provenance
    source: str = ""              # Where this came from: "character_card", "director",
                                  # "lorebook", "memory", "user", "bot"
    metadata: dict[str, Any] = field(default_factory=dict)  # Extra info for debug/logging

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @property
    def is_system(self) -> bool:
        return self.role == "system"

    @property
    def is_user(self) -> bool:
        return self.role == "user"

    @property
    def is_assistant(self) -> bool:
        return self.role == "assistant"

    @property
    def is_in_chat(self) -> bool:
        return self.position == PromptPosition.DEPTH_INJECTIONS

    @property
    def is_absolute(self) -> bool:
        return self.position != PromptPosition.DEPTH_INJECTIONS

    def token_estimate(self, chars_per_token: float = 2.0) -> int:
        """Rough token estimate using chars/token ratio.

        2.0 is conservative for Chinese-heavy text.  Use 2.5 for Latin.
        This is an estimate only — the LLM counts differently.
        """
        return max(1, int(len(self.content) / chars_per_token))

    def debug_summary(self, chars_per_token: float = 2.0) -> str:
        """One-line debug summary for logging."""
        tok = self.token_estimate(chars_per_token)
        return (
            f"[{self.position.value:02d}] {self.id:30s} "
            f"role={self.role:8s} depth={self.depth} "
            f"tokens≈{tok:4d} enabled={str(self.enabled):5s} "
            f"source={self.source}"
        )

    def __repr__(self) -> str:
        preview = self.content[:60].replace("\n", "\\n")
        return f"PromptItem(id={self.id!r}, pos={self.position.name}, content={preview!r}...)"
