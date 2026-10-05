#!/usr/bin/env python3
"""A/B Test: English Director vs Chinese Director.

Experiment A: current Chinese Director (baseline)
Experiment B: English Director — all rules in English (same structure, same logic),
              keep everything else identical:
              - Character Card English
              - Example Dialogues English
              - Language Override only says "Respond in fluent Simplified Chinese."

Hypothesis: If English Director is significantly better at reducing loop/ambiguity
            and improving plot advancement, Root Cause #1 is confirmed
            (Chinese Director conflicts with English character card identity).

Run:
    cd ~/Documents/telegramtavern/penelope
    ./venv/bin/python ../../scripts/run_ab_director.py

Outputs:
    docs/ab_director_results/<timestamp>/exp_a_chinese_director.json
    docs/ab_director_results/<timestamp>/exp_b_english_director.json
    docs/ab_director_results/<timestamp>/summary.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from textwrap import dedent

REPO = Path("/Users/leo/Documents/telegramtavern")
BOT_DIR = REPO
sys.path.insert(0, str(BOT_DIR))


# --------------------------------------------------------------------------
# Test scenario — 15 rounds
# --------------------------------------------------------------------------
ROUNDS = [
    ("你刚走进酒馆，环顾四周。",                           "greeting"),
    ("（走到吧台边）一杯热可可，谢谢。",                   "order_drink"),
    ("（接过杯子）你今天一个人？",                          "smalltalk"),
    ("（抬头看向窗外）今晚想去镇上走走，陪我吗？",         "plot_walk"),
    ("（起身，握住你的手）走吧。",                         "physical_intimacy"),
    ("（走了几步）你刚才在读什么书？",                     "callback"),
    ("（停下脚步）……我不想去人多的地方。",                 "user_objection"),
    ("（沉默了几秒）我们回去吧。",                          "user_reversal"),
    ("（回到酒馆，坐下）你还好吗？",                         "emotional_probe"),
    ("（安静地靠在你身边，什么都没说）",                    "user_silence"),
    ("（过了一会儿）你为什么总是这样对我？",                 "confrontation"),
    ("（垂下眼睛）我不是……",                               "user_vulnerable"),
    ("（伸出手，轻轻抬起你的下巴）看着我说。",              "intimate_escalation"),
    ("（两人对视良久）",                                    "long_silence"),
    ("（轻声）今晚留下来吧。",                              "closing"),
]

# Ban phrases for Chinese text analysis (same as original test)
REPEAT_BAN_PHRASES = [
    "脸红", "轻咬嘴唇", "耳边的低语", "贴近", "呼吸一滞",
    "指尖颤抖", "心跳加速", "脸红耳赤", "声音发颤", "凑近",
    "眼神闪躲", "不自觉地", "情不自禁", "欲言又止", "低下头",
    "垂下眼帘", "微微一愣", "愣了愣", "轻笑", "嘴角上扬", "轻声",
    "靠近", "耳边", "心跳", "眼神", "微微一笑", "抬手", "轻轻",
    "缓缓", "咬着唇", "红了脸", "耳尖发红", "低声", "软糯",
]


# --------------------------------------------------------------------------
# Metrics helpers
# --------------------------------------------------------------------------
def chinese_ratio(text: str) -> float:
    if not text.strip():
        return 0.0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    non_ws = sum(1 for c in text if not c.isspace())
    return cjk / max(1, non_ws)


def repetition_score(text: str) -> tuple[int, list[str]]:
    found = [p for p in REPEAT_BAN_PHRASES if p in text]
    return len(found), found


def plot_advance(text: str) -> bool:
    """Did the reply introduce something new (action / info / place change / question)?"""
    if re.search(r"[。！？？\?\!]$", text.strip()):
        return True
    if re.search(r"[吗？\?]", text):
        return True
    if re.search(r"（[^）]*[动了走坐站拿放抬低回握碰开关走靠躺趴站起倒翻穿脱拿抱推拉开门关窗请让说问答决定][^）]*）", text):
        return True
    if "……" in text:
        return True
    return False


def character_break(text: str) -> list[str]:
    bad = []
    if re.search(r"作为[一-龥]*AI", text): bad.append("meta-AI")
    if re.search(r"语言模型", text): bad.append("meta-LM")
    if re.search(r"扮演[一-龥]*角色", text): bad.append("meta-acting")
    if re.search(r"根据.{0,10}(设定|指令|要求)", text): bad.append("meta-prompt")
    if re.search(r"\b(I am an AI|I am a language model)\b", text, re.I): bad.append("meta-en")
    return bad


def jaccard_ngrams(a: str, b: str, n: int = 3) -> float:
    if not a or not b:
        return 0.0
    a_tokens = [a[i:i+n] for i in range(len(a) - n + 1)]
    b_tokens = [b[i:i+n] for i in range(len(b) - n + 1)]
    sa, sb = set(a_tokens), set(b_tokens)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_stuck_loop(rows: list[dict]) -> bool:
    """Detect if model is stuck in repeated ambiguous/flirty responses."""
    stuck_count = 0
    for r in rows:
        if not r["plot_advance"] and r["repeat_count"] >= 2:
            stuck_count += 1
        else:
            stuck_count = 0
    return stuck_count >= 3


def quality_scores(rows: list[dict]) -> dict:
    scores = []
    for r in rows:
        s = 0
        if r["chinese_ratio"] >= 0.8:  s += 2
        elif r["chinese_ratio"] >= 0.5: s += 1
        if r["plot_advance"]:           s += 2
        if r["repeat_count"] == 0:      s += 2
        elif r["repeat_count"] <= 2:    s += 1
        if not r["char_breaks"]:        s += 2
        scores.append(s)
    return {
        "per_round": scores,
        "avg": round(sum(scores) / len(scores), 2),
        "max": max(scores),
        "min": min(scores),
    }


# --------------------------------------------------------------------------
# English Director prompt (Experiment B)
# --------------------------------------------------------------------------
ENGLISH_DIRECTOR_PROMPT = dedent("""\
    [DIRECTOR — MANDATORY RULES]
    You are the director and actor of an immersive roleplay. Character: {{char}}. Player: {{user}}.

    ## Absolute Prohibitions
    1. *Do not list options* ("do you want A or B?" / "he could..."). Act or speak directly.
    2. *No meta-commentary* (no "as an AI..." / "let me roleplay...").
    3. *Do not explain what you are doing.*
    4. *Never break character.* Do not mention being an AI or language model.
    5. *Same micro-expression or action description only once per reply.*
    6. *No emotion loops.* Three or more consecutive lines of the same emotion is a loop — break it.
    7. *Do not paraphrase what the user said.*

    The following micro-expressions/actions may appear at most once per reply and no more than twice consecutively across the whole response:
      - blushes
      - bites her lip
      - whispers in your ear
      - leans in close
      - breath catches
      - fingers tremble
      - heart races
      - face turns red
      - voice trembles
      - moves closer
      - eyes dart away
      - can't help but
      - involuntarily
      - hesitates to speak
      - lowers her head
      - lowers her lashes
      - freezes for a moment
      - chuckles softly
      - lips curve into a smile
      - softly
      - silence
      - moves closer
      - at her ear
      - heartbeat
      - gaze
      - smiles faintly
      - raises her hand
      - gently
      - slowly
      - bites her lip
      - cheeks flush
      - ears turn red
      - in a low voice
      - softly

    ## Narrative Rhythm
    1. *Must advance the plot:* Every reply must contain at least one Scene Beat —
       ACTION / DECISION / LOCATION_CHANGE / NEW_INFORMATION / CHOICE /
       CONFLICT / CONSEQUENCE / EMOTIONAL_SHIFT.
       ✅ Good: 'She closes the door, sits across from you, and asks seriously: "Are you really going to say that?" (action + question = choice)
       ❌ Bad: 'Her cheeks blush, she leans in close, whispers in your ear...' (ambiguous buildup, no advancement)
    2. *When the user advances the plot, immediately follow:* When the user initiates an action, location, event, plan, or says "continue / and then / go further / let's go / you decide," the character MUST immediately act and change the situation — switch location, make a decision, give a choice, or create a minor conflict. Do not stall or continue building atmosphere. User says "let's go" → character goes and arrives at a new scene.
    3. *Character must be proactive:* Ask questions, propose ideas, make decisions. Do not only react to the user's actions — the character can create new scenes, new plot threads, reveal inner thoughts or secrets.
    4. *Avoid Purple Prose:* Do not pile up adjectives and adverbs. Pure dialogue, pure action, pure inner monologue are all fine. Keep a natural, conversational tone. Action descriptions — no more than 1 per reply, and only to advance the plot.
    5. *No infinite setup:* Do not repeatedly describe environment, atmosphere, inner monologue without actual action. Give scene-setting once, then dive into interaction. Environment descriptions total no more than 1/4 of the reply length.
    6. *Ambiguity must bring change:* Ambiguity, blushing, moving closer, soft voice — all allowed, but EVERY instance of ambiguity MUST bring scene change, action change, relationship change, or a new choice. Do not be ambiguous without advancing.
       ❌ Ambiguous only: 'Her cheeks blush, she moves close, whispers in your ear...'
       ✅ Ambiguous + advance: 'She closes the door, sits across from you, and asks seriously: "Are you really going to say that?"'
    7. *Reply length:* 60–180 Chinese characters (excluding character name and quotes). Short scene: 60–100 chars; longer scene: up to 180 chars. Exceeding 180 chars is verbose — trim immediately.

    [END DIRECTOR]
    """).strip()


# --------------------------------------------------------------------------
# Run one experiment
# --------------------------------------------------------------------------
async def run_experiment(
    bot: str,
    exp_label: str,
    *,
    use_english_director: bool = False,
) -> dict:
    bot_dir = REPO / bot
    sys.path.insert(0, str(bot_dir))
    for mod in ["character_card", "prompt_item", "pipeline", "director", "config", "ollama_client"]:
        sys.modules.pop(mod, None)

    from dotenv import dotenv_values
    from character_card import load_character
    from pipeline import PromptPipeline, PromptPosition
    from prompt_item import PromptItem
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

    director_cfg = DirectorConfig(enabled=True)

    # Language override
    if use_english_director:
        lang_content = dedent("""\
            [LANGUAGE OVERRIDE — HIGHEST PRIORITY]
            Respond in fluent Simplified Chinese. All narration, dialogue, inner thoughts,
            and action descriptions must be in Simplified Chinese. Do not switch languages.
            """).strip()
    else:
        lang_content = "Chinese"

    pipeline = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        persona=None,
        char_label=char_label,
        user_label=user_label,
        reply_language=lang_content,
    )

    client = OllamaClient(
        base_url=cfg.get("OLLAMA_URL", "http://localhost:11434"),
        model=cfg.get("OLLAMA_MODEL",
                      "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"),
        max_tokens=int(cfg.get("MAX_TOKENS", 1024)),
    )

    # Build English Director PromptItem
    if use_english_director:
        en_prompt = ENGLISH_DIRECTOR_PROMPT.replace("{{char}}", char_label).replace("{{user}}", user_label)
        director_item = PromptItem(
            id="director",
            role="system",
            content=en_prompt,
            enabled=True,
            position=PromptPosition.DIRECTOR,
            priority=0,
            depth=0,
            source="experiment_b_english_director",
        )
        director_chars = len(en_prompt)
    else:
        director_item = None
        director_chars = 0

    history: list[dict] = []
    rows: list[dict] = []
    t_start = time.time()

    print(f"\n{'='*62}")
    print(f"  Experiment: {exp_label}")
    print(f"  Bot: {bot}  |  Model: {client.model}")
    print(f"  English Director: {use_english_director}  |  Director chars: {director_chars}")
    print(f"{'='*62}")

    for i, (user_msg, tag) in enumerate(ROUNDS, start=1):
        history.append({"role": "user", "content": user_msg})
        if len(history) > 40:
            history = history[-40:]

        messages = pipeline.assemble(history, "")

        # Inject English Director PromptItem at position 2 (after language override, before char_defs)
        if director_item is not None:
            # messages[0]=system_anchor, [1]=language_override, insert director at [2]
            # Build fresh messages with English Director injected at correct position
            from pipeline import _sub_placeholders
            absolute_items = []
            for idx, msg in enumerate(messages):
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "system":
                    # Check what this item is by content
                    if "LANGUAGE OVERRIDE" in content:
                        absolute_items.append(msg)  # language override first
                        absolute_items.append({"role": "system", "content": en_prompt})  # then director
                    elif "You are" in content and "role-playing" in content and "Stay in character" in content:
                        # system anchor — already handled at top, skip duplicates
                        pass
                    else:
                        if idx == 0:  # only system anchor (first message)
                            absolute_items.append(msg)

            # Reconstruct: anchor + lang_override + director + char_defs + examples + history
            new_messages = []
            # Find anchor
            anchor_msg = None
            lang_msg = None
            char_defs_msgs = []
            example_msgs = []
            chat_msgs = []

            for msg in messages:
                c = msg.get("content", "")
                if "You are" in c and "role-playing" in c:
                    anchor_msg = msg
                elif "LANGUAGE OVERRIDE" in c:
                    lang_msg = msg
                elif "[Description]" in c or "[Personality]" in c or "[Scenario]" in c:
                    if not char_defs_msgs or c != char_defs_msgs[0].get("content", ""):
                        char_defs_msgs.append(msg)
                elif "Example Dialogue" in c or "do NOT continue" in c:
                    if not example_msgs:
                        example_msgs.append(msg)
                elif msg.get("role") in ("user", "assistant"):
                    chat_msgs.append(msg)

            if anchor_msg:
                new_messages.append(anchor_msg)
            if lang_msg:
                new_messages.append(lang_msg)
            if use_english_director:
                new_messages.append({"role": "system", "content": en_prompt})
            for m in char_defs_msgs:
                new_messages.append(m)
            for m in example_msgs:
                new_messages.append(m)
            for m in chat_msgs:
                new_messages.append(m)

            messages = new_messages

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
        prev = history[-2]["content"] if len(history) >= 2 else ""
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

        history.append({"role": "assistant", "content": reply})

        status = "✓" if plot else "✗"
        rep_str = f"rep={rep_count}" if rep_count else "rep=0"
        print(f"  R{i:02d} [{tag:18s}] {status} zh={zh:.0%} {rep_str:8s}  echo={jacc:.2f}  ({dt:.1f}s)  len={len(reply)}")

    total_dt = time.time() - t_start
    quality = quality_scores(rows)
    stuck = is_stuck_loop(rows)

    avg_zh = sum(r["chinese_ratio"] for r in rows) / len(rows)
    pct_chinese = sum(1 for r in rows if r["chinese_ratio"] >= 0.8) / len(rows)
    pct_plot = sum(1 for r in rows if r["plot_advance"]) / len(rows)
    avg_rep = sum(r["repeat_count"] for r in rows) / len(rows)
    any_break = any(r["char_breaks"] for r in rows)
    avg_jacc = sum(r["echo_prev_jaccard"] for r in rows) / len(rows)

    summary = {
        "experiment": exp_label,
        "bot": bot,
        "model": client.model,
        "director_chars": director_chars,
        "director_lang": "English" if use_english_director else "Chinese",
        "rounds": len(rows),
        "total_seconds": round(total_dt, 1),
        "avg_repeat_per_turn": round(avg_rep, 2),
        "pct_chinese_dominant": round(pct_chinese, 2),
        "avg_chinese_ratio": round(avg_zh, 2),
        "pct_plot_advance": round(pct_plot, 2),
        "any_fourth_wall_break": any_break,
        "avg_echo_jaccard": round(avg_jacc, 3),
        "stuck_in_loop": stuck,
        "quality_score_avg": quality["avg"],
        "quality_score_max": quality["max"],
        "quality_score_min": quality["min"],
        "quality_per_round": quality["per_round"],
        "rows": rows,
    }

    print(f"\n  ── Summary ──────────────────────────────────────")
    print(f"  Chinese dominant turns:  {pct_chinese:.0%}")
    print(f"  Avg Chinese ratio:       {avg_zh:.0%}")
    print(f"  Plot advance rate:        {pct_plot:.0%}")
    print(f"  Avg repeat phrases/turn: {avg_rep:.2f}")
    print(f"  Stuck in loop:           {stuck}")
    print(f"  Quality score (avg):     {quality['avg']}")
    print(f"  Total time:              {total_dt:.1f}s")

    return summary


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("bot", choices=["penelope", "june", "aqua"], nargs="?", default="penelope")
    ap.add_argument("--skip-a", action="store_true", help="Skip Experiment A (Chinese Director)")
    ap.add_argument("--skip-b", action="store_true", help="Skip Experiment B (English Director)")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir) if args.out_dir else REPO / "docs" / "ab_director_results" / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    if not args.skip_a:
        results["exp_a_chinese_director"] = asyncio.run(run_experiment(
            args.bot,
            "A: Chinese Director (baseline)",
            use_english_director=False,
        ))

    if not args.skip_b:
        results["exp_b_english_director"] = asyncio.run(run_experiment(
            args.bot,
            "B: English Director",
            use_english_director=True,
        ))

    # Save results
    for key, res in results.items():
        path = out_dir / f"{key}.json"
        path.write_text(json.dumps(res, ensure_ascii=False, indent=2))

    # Compare
    if "exp_a_chinese_director" in results and "exp_b_english_director" in results:
        a = results["exp_a_chinese_director"]
        b = results["exp_b_english_director"]

        verdict = ""
        delta_plot = b["pct_plot_advance"] - a["pct_plot_advance"]
        delta_rep = b["avg_repeat_per_turn"] - a["avg_repeat_per_turn"]
        delta_qual = b["quality_score_avg"] - a["quality_score_avg"]

        if delta_plot > 0.1 and delta_rep < -0.2:
            verdict = (
                "**VERDICT: English Director is significantly better.**\n"
                f"  Plot advance +{delta_plot:.0%}, repeat phrases {delta_rep:.2f}/turn lower.\n"
                "  → Root Cause #1 CONFIRMED: Chinese Director conflicts with English character card."
            )
        elif delta_plot < -0.1:
            verdict = (
                "**VERDICT: Chinese Director is better.**\n"
                f"  Plot advance {delta_plot:.0%}, repeat {delta_rep:.2f}/turn.\n"
                "  → Root Cause #1 NOT the main cause. Investigate elsewhere."
            )
        else:
            verdict = (
                "**VERDICT: No significant difference.**\n"
                f"  Plot Δ={delta_plot:+.0%}, repeat Δ={delta_rep:+.2f}, quality Δ={delta_qual:+.1f}\n"
                "  → Root Cause #1 NOT the main cause. Move to next hypothesis."
            )

        lines = [
            "# English vs Chinese Director — A/B Test Results",
            "",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  **Bot:** {args.bot}",
            f"**Model:** {a['model']}",
            "",
            "## Head-to-Head Comparison",
            "",
            "| Metric | A: Chinese Director | B: English Director | Δ |",
            "|--------|-------------------|-------------------|---|",
            f"| Plot advance rate | {a['pct_plot_advance']:.0%} | {b['pct_plot_advance']:.0%} | {delta_plot:+.0%} |",
            f"| Avg repeat/turn | {a['avg_repeat_per_turn']:.2f} | {b['avg_repeat_per_turn']:.2f} | {delta_rep:+.2f} |",
            f"| Stuck in loop | {a['stuck_in_loop']} | {b['stuck_in_loop']} | — |",
            f"| Chinese dominant turns | {a['pct_chinese_dominant']:.0%} | {b['pct_chinese_dominant']:.0%} | {b['pct_chinese_dominant']-a['pct_chinese_dominant']:+.0%} |",
            f"| Avg Chinese ratio | {a['avg_chinese_ratio']:.0%} | {b['avg_chinese_ratio']:.0%} | {b['avg_chinese_ratio']-a['avg_chinese_ratio']:+.0%} |",
            f"| Any 4th-wall break | {a['any_fourth_wall_break']} | {b['any_fourth_wall_break']} | — |",
            f"| Quality score (avg) | {a['quality_score_avg']:.1f} | {b['quality_score_avg']:.1f} | {delta_qual:+.1f} |",
            f"| Total time | {a['total_seconds']:.1f}s | {b['total_seconds']:.1f}s | — |",
            "",
            "## Per-Round Quality Score (A vs B, max=8 per round)",
            "",
            "| Round | Tag | A score | B score | Δ | A plot | B plot | A rep | B rep |",
            "|-------|-----|---------|---------|---|--------|--------|-------|-------|",
        ]

        for ra, rb in zip(a["rows"], b["rows"]):
            qa = a["quality_per_round"][ra["round"] - 1]
            qb = b["quality_per_round"][rb["round"] - 1]
            delta = qb - qa
            sign = "+" if delta > 0 else ""
            lines.append(
                f"| {ra['round']:2d} | {ra['tag']} | {qa} | {qb} | {sign}{delta} | "
                f"{'✓' if ra['plot_advance'] else '✗'} | {'✓' if rb['plot_advance'] else '✗'} | "
                f"{ra['repeat_count']} | {rb['repeat_count']} |"
            )

        lines += [
            "",
            "## Verdict",
            "",
            verdict,
            "",
            f"Results saved to: `{out_dir}/`",
        ]

        summary_md = "\n".join(lines)
        (out_dir / "summary.md").write_text(summary_md)
        print(f"\n{'='*62}")
        print(f"  Summary → {out_dir / 'summary.md'}")
        print(f"{'='*62}")
        print(summary_md)
    else:
        for key, res in results.items():
            path = out_dir / f"{key}.json"
            path.write_text(json.dumps(res, ensure_ascii=False, indent=2))
            print(f"  {key} → {path}")

    print(f"\n→ All results in: {out_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
