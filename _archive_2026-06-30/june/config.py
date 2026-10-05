"""Configuration loader for the SillyTavern Telegram bridge."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Config:
    telegram_token: str = field(default_factory=lambda: os.environ["TELEGRAM_BOT_TOKEN"])
    ollama_url: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_URL", "http://localhost:11434")
    )
    ollama_model: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_MODEL", "qwen3:32b")
    )
    character_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("CHARACTER_PATH", str(ROOT / "data" / "Penelope3.png"))
        )
    )
    db_path: Path = field(
        default_factory=lambda: Path(os.environ.get("DB_PATH", str(ROOT / "data" / "sessions.db")))
    )
    # Cross-bot shared state file (relay/duet coordination).
    # All bot processes that should coordinate MUST point at the same path.
    shared_db_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "SHARED_DB_PATH",
                str(ROOT.parent / "shared" / "conversation_state.db"),
            )
        )
    )
    # Use the full system prompt including {{char}}/{{user}} placeholders verbatim.
    # We replace them at runtime. Tokens inside character description are kept as-is.
    history_limit_messages: int = int(os.environ.get("HISTORY_LIMIT_MESSAGES", "40"))
    # Max generation tokens per reply
    max_tokens: int = int(os.environ.get("MAX_TOKENS", "1024"))
    # Reply draft edit interval (seconds). Telegram has rate limits ~30 msg/min globally,
    # but per-chat editMessageText is much higher. Keep this short for snappy UX.
    edit_interval: float = float(os.environ.get("EDIT_INTERVAL", "0.6"))
    # User name label substituted for {{user}}
    user_label: str = os.environ.get("USER_LABEL", "Friend")
    # Char label substituted for {{char}}
    char_label: str = os.environ.get("CHAR_LABEL", "Penelope")
    # Force the model to reply in a specific language (overrides character card language).
    # Set to empty string to use whatever the model picks. Examples: "Chinese", "Japanese",
    # "English", "Korean", "French".
    reply_language: str = os.environ.get("REPLY_LANGUAGE", "Chinese")
    # Whether to enable NSFW content. The character card itself is adult-only.
    nsfw_enabled: bool = os.environ.get("NSFW_ENABLED", "true").lower() in ("1", "true", "yes")
    # Optional admin user IDs (comma separated). Only admins can use /admin commands if set.
    admin_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
        )
    )


config = Config()