"""Data directory layout and per-bot YAML configuration.

Layout (``TAVERN_DATA_DIR`` or ~/Library/Application Support/TelegramTavern):
    .env            TG_TOKEN_* secrets (never in yaml / git)
    bots/*.yaml     one file = one bot
    characters/     SillyTavern V2/V3 PNG cards (embedded books auto-detected)
    worlds/         SillyTavern World Info JSON (stack any number onto a bot)
    data/<bot>/     SQLite + session exports
    logs/
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml
from dotenv import dotenv_values

log = logging.getLogger("tavern.config")

APP_NAME = "TelegramTavern"
DEFAULT_MODEL = "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"
DEFAULT_EXTRACT_MODEL = "qwen3:14b"
SUBDIRS = ("bots", "characters", "worlds", "data", "logs")

ENV_TEMPLATE = """# TelegramTavern secrets — one token per bot, referenced by `token_env` in bots/*.yaml
# Get tokens from @BotFather. This file is never committed.
TG_TOKEN_EXAMPLE=
"""

EXAMPLE_BOT_YAML = """# Example bot — copy, rename, set enabled: true.
name: example
enabled: false
mode: dialogue            # dialogue | rpg
token_env: TG_TOKEN_EXAMPLE
characters:               # files in characters/ ; dialogue mode lets users /character-switch
  - MyCharacter.png
worlds: []                # files in worlds/ stacked on top of the card's embedded book
model: {model}
reply_language: zh-CN
user_label: 你
""".format(model=DEFAULT_MODEL)


def default_data_dir() -> Path:
    env = os.environ.get("TAVERN_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def ensure_data_dir(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (data_dir / sub).mkdir(exist_ok=True)
    env = data_dir / ".env"
    if not env.exists():
        env.write_text(ENV_TEMPLATE, encoding="utf-8")
        os.chmod(env, 0o600)
    if not any((data_dir / "bots").glob("*.yaml")):
        (data_dir / "bots" / "example.yaml").write_text(EXAMPLE_BOT_YAML, encoding="utf-8")
    return data_dir


@dataclass
class BotConfig:
    name: str
    mode: str = "dialogue"                 # dialogue | rpg
    kind: str = "telegram"                 # telegram | local (local = in-app chat only, no token)
    enabled: bool = True
    token_env: str = ""
    characters: list[str] = field(default_factory=list)
    worlds: list[str] = field(default_factory=list)
    model: str = DEFAULT_MODEL
    extract_model: str = DEFAULT_EXTRACT_MODEL
    ollama_url: str = "http://127.0.0.1:11434"
    reply_language: str = "zh-CN"
    language_override: str = ""            # full text; replaces the pipeline's generic override
    translation_table: list = field(default_factory=list)   # [[src, dst], ...] applied to output
    director_characters: list[str] = field(default_factory=list)  # dialogue: cards with Director block
    story_engine: bool = True              # dialogue: anti-stall story hints
    rules: bool = False                    # rpg: D&D-style RuleEngine (DM only)
    scenes: bool = False                   # rpg: DirectorEngine scene/beat control (DM only)
    extract: bool = True                   # rpg: LLM world-state extraction
    user_label: str = "你"
    max_context: int = 16384
    max_tokens: int = 1024
    history_limit: int = 30                # was 60: a long verbatim history teaches the model its own loops
    temperature: float = 0.95
    # anti-loop sampling (see ollama_client.py); per-bot overridable
    repeat_penalty: float = 1.1
    repeat_last_n: int = 512
    presence_penalty: float = 0.4
    frequency_penalty: float = 0.25
    top_k: int = 40
    min_p: float = 0.05
    newgame_prompt: str = ""               # rpg: user-side instruction that opens a new game
    continue_prompt: str = "（请继续推进剧情。）"
    first_mes_translate: bool = True       # rpg: run translation_table over the card greeting
    admin_ids: list[int] = field(default_factory=list)
    # runtime-only
    token: str = field(default="", repr=False)
    path: Path | None = field(default=None, repr=False)

    @property
    def character_files(self) -> list[str]:
        return [c for c in self.characters if c]


_KNOWN = {f.name for f in fields(BotConfig)}


def load_env(data_dir: Path) -> dict[str, str]:
    values = {k: v for k, v in (dotenv_values(data_dir / ".env") or {}).items() if v}
    values.update({k: v for k, v in os.environ.items() if k.startswith("TG_TOKEN_")})
    return values


def load_bot_file(path: Path, env: dict[str, str]) -> BotConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    unknown = sorted(set(raw) - _KNOWN)
    if unknown:
        log.warning("%s: ignoring unknown keys %s", path.name, unknown)
    data = {k: v for k, v in raw.items() if k in _KNOWN and k not in ("token", "path")}
    data.setdefault("name", path.stem)
    if isinstance(data.get("characters"), str):
        data["characters"] = [data["characters"]]
    if isinstance(data.get("worlds"), str):
        data["worlds"] = [data["worlds"]]
    cfg = BotConfig(**data)
    cfg.path = path
    cfg.token = env.get(cfg.token_env, "") if cfg.token_env else ""
    return cfg


def load_bots(data_dir: Path) -> list[BotConfig]:
    env = load_env(data_dir)
    bots: list[BotConfig] = []
    for p in sorted((data_dir / "bots").glob("*.yaml")):
        try:
            bots.append(load_bot_file(p, env))
        except Exception as exc:  # noqa: BLE001
            log.error("skip %s: %s", p.name, exc)
    return bots


def validate_bot(cfg: BotConfig, data_dir: Path) -> list[str]:
    """Return human-readable problems (empty = ok)."""
    problems = []
    if cfg.mode not in ("dialogue", "rpg"):
        problems.append(f"mode 必须是 dialogue 或 rpg（当前 {cfg.mode}）")
    if cfg.kind not in ("telegram", "local"):
        problems.append(f"kind 必须是 telegram 或 local（当前 {cfg.kind}）")
    if cfg.kind == "telegram":
        if not cfg.token_env:
            problems.append("缺少 token_env")
        elif not cfg.token:
            problems.append(f".env 里没有 {cfg.token_env}")
    if not cfg.character_files:
        problems.append("characters 为空")
    for c in cfg.character_files:
        if not (data_dir / "characters" / c).exists():
            problems.append(f"角色卡不存在: characters/{c}")
    for w in cfg.worlds:
        if not (data_dir / "worlds" / w).exists():
            problems.append(f"世界书不存在: worlds/{w}")
    if cfg.mode == "rpg" and len(cfg.character_files) > 1:
        problems.append("rpg 模式只使用第一张角色卡，其余将被忽略")
    return problems
