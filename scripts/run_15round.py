#!/usr/bin/env python3
"""15-round integration test for one Telegram Tavern bot.

Drives the bot's actual pipeline.py + ollama_client.py without Telegram.
Sends fixed user messages per round, captures replies, computes metrics.

Run:
    cd /Users/leo/Documents/telegramtavern/penelope
    ./venv/bin/python ../../scripts/run_15round.py [bot_name]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO = Path("/Users/leo/Documents/telegramtavern")
BOT_DIR = REPO  # overridden below
sys.path.insert(0, str(BOT_DIR))


# --------------------------------------------------------------------------
# Test scenario — same 15 rounds applied to all three bots
# --------------------------------------------------------------------------
ROUNDS = [
    ("你刚走进酒馆，环顾四周。",                                                   "greeting"),
    ("（走到吧台边）一杯热可可，谢谢。",                                          "order_drink"),
    ("（接过杯子）你今天一个人？",                                                  "smalltalk"),
    ("（抬头看向窗外）今晚想去镇上走走，陪我吗？",                                 "plot_walk"),
    ("（起身，握住你的手）走吧。",                                                 "physical_intimacy"),
    ("（走了几步）你刚才在读什么书？",                                             "callback"),
    ("（停下脚步）……我不想去人多的地方。",                                          "user_objection"),
    ("（沉默了几秒）我们回去吧。",                                                  "user_reversal"),
    ("（回到酒馆，坐下）你还好吗？",                                                 "emotional_probe"),
    ("（安静地靠在你身边，什么都没说）",                                            "user_silence"),
    ("（过了一会儿）你为什么总是这样对我？",                                        "confrontation"),
    ("（垂下眼睛）我不是……",                                                       "user_vulnerable"),
    ("（伸出手，轻轻抬起你的下巴）看着我说。",                                      "intimate_escalation"),
    ("（两人对视良久）",                                                            "long_silence"),
    ("（轻声）今晚留下来吧。",                                                      "closing"),
]


def chinese_ratio(text: str) -> float:
    if not text.strip():
        return 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_ws = sum(1 for c in text if not c.isspace())
    return cjk / max(1, non_ws)


REPEAT_BAN_PHRASES = [
    "脸红", "轻咬嘴唇", "耳边的低语", "贴近", "呼吸一滞",
    "指尖颤抖", "心跳加速", "脸红耳赤", "声音发颤", "凑近",
    "眼神闪躲", "不自觉地", "情不自禁", "欲言又止", "低下头",
    "垂下眼帘", "微微一愣", "愣了愣", "轻笑", "嘴角上扬", "轻声",
]


def repetition_score(text: str) -> tuple[int, list[str]]:
    found = [p for p in REPEAT_BAN_PHRASES if p in text]
    return len(found), found


def plot_advance(text: str) -> bool:
    """Did the reply introduce something new (action / info / place change / question)?"""
    new_action_signals = [
        re.search(r"（[^）]*[动了走坐站拿放抬头回握碰][^）]*）", text),  # （...动作...）
        re.search(r"\?", text),  # question
        "?" in text,
        "吗" in text,
        "？" in text,
        "……" in text,  # dramatic beat
        re.search(r"[。！？]$", text.strip()),  # ends with sentence
    ]
    return any(new_action_signals)


def character_break(text: str) -> list[str]:
    """Detect obvious fourth-wall breaks / AI-ization."""
    bad = []
    if re.search(r"作为[一-龥]*AI", text): bad.append("meta-AI")
    if re.search(r"语言模型", text): bad.append("meta-LM")
    if re.search(r"扮演[一-龥]*角色", text): bad.append("meta-acting")
    if re.search(r"根据.{0,10}(设定|指令|要求)", text): bad.append("meta-prompt")
    if re.search(r"我可以[为帮]", text): bad.append("meta-help")
    if re.search(r"\b(I am an AI|I am a language model)\b", text, re.I): bad.append("meta-en")
    return bad


def jaccard_ngrams(a: str, b: str, n: int = 3) -> float:
    """Naive n-gram overlap — proxy for echo / repetition."""
    if not a or not b:
        return 0.0
    a_tokens = [a[i:i + n] for i in range(len(a) - n + 1)]
    b_tokens = [b[i:i + n] for i in range(len(b) - n + 1)]
    sa, sb = set(a_tokens), set(b_tokens)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


async def run_one(bot: str) -> dict:
    bot_dir = REPO / bot
    sys.path.insert(0, str(bot_dir))
    for mod in ["character_card", "prompt_item", "pipeline", "director", "config", "ollama_client"]:
        sys.modules.pop(mod, None)

    from dotenv import dotenv_values
    from character_card import load_character
    from pipeline import PromptPipeline
    from director import DirectorConfig
    from ollama_client import OllamaClient

    cfg = dotenv_values(bot_dir / ".env")
    char_png = {
        "penelope": "Penelope3.png",
        "june": "June.png",
        "aqua": "Aqua.png",
    }[bot]
    card = load_character(bot_dir / "data" / char_png)

    char_label = cfg.get("CHAR_LABEL", Path(char_png).stem)
    user_label = cfg.get("USER_LABEL", "Friend")
    reply_language = cfg.get("REPLY_LANGUAGE", "Chinese") or "Chinese"

    director_cfg = DirectorConfig(
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

    pipeline = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        persona=None,
        char_label=char_label,
        user_label=user_label,
        reply_language=reply_language,
    )

    client = OllamaClient(
        base_url=cfg.get("OLLAMA_URL", "http://localhost:11434"),
        model=cfg.get("OLLAMA_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"),
        max_tokens=int(cfg.get("MAX_TOKENS", 1024)),
    )

    history: list[dict] = []
    rows: list[dict] = []
    t_start = time.time()

    for i, (user_msg, tag) in enumerate(ROUNDS, start=1):
        # Append user_msg to history FIRST (mirrors bot.py: store.append then history.fetch)
        history.append({"role": "user", "content": user_msg})
        # Trim to last 40 messages to mimic history_limit_messages
        if len(history) > 40:
            history = history[-40:]

        messages = pipeline.assemble(history, "")  # user_message arg is unused by assemble; we already have it in history

        chunks: list[str] = []
        t0 = time.time()
        async for piece in client.stream_chat(messages):
            chunks.append(piece)
        reply = "".join(chunks).strip()
        dt = time.time() - t0

        zh = chinese_ratio(reply)
        rep_count, rep_phrases = repetition_score(reply)
        plot = plot_advance(reply)
        breaks = character_break(reply)
        prev = history[-1]["content"] if history and history[-1]["role"] == "assistant" else ""
        jacc = jaccard_ngrams(reply, prev) if prev else 0.0

        rows.append({
            "round": i,
            "tag": tag,
            "user": user_msg,
            "reply": reply,
            "reply_len": len(reply),
            "gen_seconds": round(dt, 1),
            "chinese_ratio": round(zh, 2),
            "repeat_phrases": rep_phrases,
            "repeat_count": rep_count,
            "plot_advance": plot,
            "char_breaks": breaks,
            "echo_prev_jaccard": round(jacc, 3),
        })

        # Update history for next round
        # (user_msg already appended at top of loop)
        history.append({"role": "assistant", "content": reply})

        print(f"  round {i:2d} [{tag:18s}]  zh={zh:.0%}  rep={rep_count}  plot={'Y' if plot else 'N'}  breaks={breaks}  echo={jacc:.2f}  ({dt:.1f}s)  len={len(reply)}")
        sys.stdout.flush()

    total_dt = time.time() - t_start

    # Summary
    avg_zh = sum(r["chinese_ratio"] for r in rows) / len(rows)
    pct_chinese = sum(1 for r in rows if r["chinese_ratio"] >= 0.8) / len(rows)
    pct_plot = sum(1 for r in rows if r["plot_advance"]) / len(rows)
    avg_rep = sum(r["repeat_count"] for r in rows) / len(rows)
    any_break = any(r["char_breaks"] for r in rows)
    avg_jacc = sum(r["echo_prev_jaccard"] for r in rows) / len(rows)

    summary = {
        "bot": bot,
        "model": client.model,
        "rounds": len(rows),
        "total_seconds": round(total_dt, 1),
        "avg_repeat_phrases_per_turn": round(avg_rep, 2),
        "pct_chinese_dominant_turns": round(pct_chinese, 2),
        "pct_plot_advance": round(pct_plot, 2),
        "any_fourth_wall_break": any_break,
        "avg_echo_jaccard": round(avg_jacc, 3),
        "rows": rows,
    }
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", choices=["penelope", "june", "aqua"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    summary = asyncio.run(run_one(args.bot))
    out_path = Path(args.out) if args.out else REPO / "docs" / f"st_alignment_test_{args.bot}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\n→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
