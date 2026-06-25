#!/usr/bin/env python3
"""Generate /debug-level prompt reports for Penelope / June / Aqua.

Run from one of the bot venvs (must have python-telegram-bot deps — or just
basic stdlib + python-dotenv + Pillow since pipeline.py only needs those).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Add the bot dir to sys.path so we import that bot's modules
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # repo root (telegramtavern/)


def find_bot_dir(bot_dir_name: str) -> Path:
    return ROOT / bot_dir_name


def load_character_card(path: Path):
    sys.path.insert(0, str(path))
    from character_card import ExtendedCharacterCard

    card = ExtendedCharacterCard.load(path / "data" / "character_card.png")
    return card


def load_config(bot_dir: Path) -> dict[str, Any]:
    from dotenv import dotenv_values

    cfg = dotenv_values(bot_dir / ".env")
    return cfg


def make_director_config() -> Any:
    from director import DirectorConfig

    return DirectorConfig(
        enabled=True,
        no_option_listing=True,
        no_meta_commentary=True,
        no_prompt_explanation=True,
        no_fourth_wall=True,
        no_repeat_micro_expressions=True,
        no_emotion_loops=True,
        no_user_repeat=True,
        advance_narrative=True,
        proactive_role=True,
        avoid_purple_prose=True,
        reply_length_min=80,
        reply_length_max=280,
    )


def sample_history(char_label: str, user_label: str) -> list[dict]:
    """A representative 4-turn history so the report shows real interleaving."""
    return [
        {"role": "user", "content": f"（{user_label} 推开酒馆的木门，深吸一口气。）"},
        {"role": "assistant", "content": f"（{char_label} 抬头看向门口，微微一笑）这么晚才来，我以为你不来了。"},
        {"role": "user", "content": "路上堵车了。"},
        {"role": "assistant", "content": f"（{char_label} 放下手里的书）没关系，我已经把暖炉点上了。坐吧，要喝点什么？"},
    ]


def generate_one(
    bot: str,
    char_png_rel: str,
    out_path: Path,
) -> dict[str, Any]:
    bot_dir = ROOT / bot
    sys.path.insert(0, str(bot_dir))
    # Reload modules fresh for each bot (different file content + sys.path)
    for mod in ["character_card", "prompt_item", "pipeline", "director", "config"]:
        if mod in sys.modules:
            del sys.modules[mod]

    from character_card import ExtendedCharacterCard, load_character
    from pipeline import PromptPipeline, DEBUG_PROMPT

    char_png_path = ROOT / bot / char_png_rel
    card = load_character(char_png_path)

    cfg = load_config(bot_dir)
    director_cfg = make_director_config()
    char_label = cfg.get("CHAR_LABEL", char_png_path.stem)
    user_label = cfg.get("USER_LABEL", "Friend")
    reply_language = cfg.get("REPLY_LANGUAGE", "zh-CN")

    pipeline = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        persona=None,
        char_label=char_label,
        user_label=user_label,
        reply_language=reply_language or None,
    )

    history = sample_history(char_label, user_label)
    user_message = f"（{user_label} 走到吧台边坐下）一杯热可可，谢谢。"

    DEBUG_PROMPT = True
    report = pipeline.debug_report(history, user_message)

    # Add token estimates, sampling, context length summary
    messages = pipeline.assemble(history, user_message)
    total_chars = sum(len(m.get("content", "")) for m in messages)
    total_tokens_est = total_chars // 2

    # Sampling (defaults from config.py — since REPLY_LANGUAGE / model are .env,
    # we read ollama params from config defaults)
    summary_block = f"""

── D. Sampling + context ──────────────────────────────────
  model:                {cfg.get("OLLAMA_MODEL", "qwen3:32b (default)")}
  ollama_url:           {cfg.get("OLLAMA_URL", "http://localhost:11434 (default)")}
  REPLY_LANGUAGE:       {reply_language}
  max_tokens (reply):   {cfg.get("MAX_TOKENS", "1024 (default)")}
  history_limit:        {cfg.get("HISTORY_LIMIT_MESSAGES", "40 (default)")}
  edit_interval:        {cfg.get("EDIT_INTERVAL", "0.6 (default)")}
  nsfw_enabled:         {cfg.get("NSFW_ENABLED", "true (default)")}
  total messages:       {len(messages)}
  total chars (input):  {total_chars}
  estimated input tok:  ~{total_tokens_est}  (chars/2 — rough for CJK)

  Sampler params used by Ollama (set by user via UI or API; not in .env):
    temp:      (default — see ollama_client.py / per-bot run)
    top_p:     -
    top_k:     -
    repeat_pen: -
"""

    full = report + summary_block

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(full, encoding="utf-8")

    return {
        "bot": bot,
        "report_path": str(out_path),
        "items_total": report.count("PROMPT_ITEM"),
        "messages_count": len(messages),
        "total_chars": total_chars,
    }


def main() -> int:
    # Use Penelope's venv python interpreter (any bot venv works — same modules)
    bots = [
        ("penelope", "data/Penelope3.png", ROOT / "docs" / "prompt_compare_penelope.md"),
        ("june",     "data/June.png",       ROOT / "docs" / "prompt_compare_june.md"),
        ("aqua",     "data/Aqua.png",       ROOT / "docs" / "prompt_compare_aqua.md"),
    ]
    results = []
    for bot, char_png, out in bots:
        try:
            r = generate_one(bot, char_png, out)
            results.append(r)
            print(f"  {bot}: -> {out}")
        except Exception as e:
            print(f"  {bot}: FAILED — {e!r}")
            raise
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
