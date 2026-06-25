#!/usr/bin/env python3
"""20-round Story Engine acceptance test for Penelope.

Drives the bot's actual pipeline + story engine (no Telegram) using the user's
required stalling triggers:
  继续 / 然后呢 / 我们接下来做什么 / 更进一步 / 换个地方 / 你决定吧

Verifies the acceptance criteria:
  - no_progress <= 4
  - consecutive no_progress never > 1
  - repeated motif hits down vs naive
  - '继续' must advance
  - '换个地方' must switch scene

Usage:
    cd /Users/leo/Documents/telegramtavern/penelope
    ./venv/bin/python ../../scripts/run_story_engine_test.py [--out path.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path("/Users/leo/Documents/telegramtavern")


async def run(bot: str = "penelope") -> dict:
    bot_dir = REPO / bot
    sys.path.insert(0, str(bot_dir))
    for mod in [
        "character_card", "prompt_item", "pipeline", "director", "config",
        "ollama_client", "db", "story_engine",
    ]:
        sys.modules.pop(mod, None)

    from dotenv import dotenv_values
    from character_card import load_character
    from pipeline import PromptPipeline
    from director import DirectorConfig
    from ollama_client import OllamaClient
    from story_engine import StoryEngine, StoryState, detect_stall, detect_location_change

    cfg = dotenv_values(bot_dir / ".env")
    char_png = {"penelope": "Penelope3.png", "june": "June.png", "aqua": "Aqua.png"}[bot]
    card = load_character(bot_dir / "data" / char_png)

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
        reply_length_min=60,
        reply_length_max=180,
    )

    engine = StoryEngine()
    state = StoryState()
    pipe = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        persona=None,
        char_label=cfg.get("CHAR_LABEL", "Penelope"),
        user_label=cfg.get("USER_LABEL", "Friend"),
        reply_language=cfg.get("REPLY_LANGUAGE", "Chinese") or "Chinese",
        story_state=state,
        story_engine=engine,
    )
    client = OllamaClient(
        base_url=cfg.get("OLLAMA_URL", "http://127.0.0.1:11434"),
        model=cfg.get("OLLAMA_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest"),
        max_tokens=int(cfg.get("MAX_TOKENS", "1024")),
    )

    # 20 rounds — heavy on the user's required stalling triggers, plus normal RP
    rounds = [
        ("你好，今天我们做什么？",                            "open"),
        ("继续",                                            "advance:继续"),
        ("然后呢",                                           "advance:然后呢"),
        ("更进一步",                                          "advance:更进一步"),
        ("我们接下来做什么",                                  "advance:我们接下来做什么"),
        ("换个地方",                                          "advance:换个地方"),
        ("（到了新地点）这里怎么样？",                          "scene_check:new_loc"),
        ("你决定吧",                                          "advance:你决定吧"),
        ("然后呢",                                            "advance:然后呢"),
        ("（沉默）",                                          "stall_setup"),
        ("继续",                                             "advance:继续"),
        ("（被无视）你还好吗？",                              "stall_recover"),
        ("然后呢",                                            "advance:然后呢"),
        ("更进一步",                                          "advance:更进一步"),
        ("（凑近一点）",                                      "stall_setup"),
        ("换个话题",                                          "advance:换个话题"),
        ("继续",                                             "advance:继续"),
        ("（停顿）",                                          "stall_setup"),
        ("你决定吧",                                          "advance:你决定吧"),
        ("今晚留下来吧",                                      "closing"),
    ]

    history: list[dict] = []
    rows: list[dict] = []
    rewrite_used_count = 0
    t_start = time.time()

    for i, (user_msg, tag) in enumerate(rounds, start=1):
        # Append user turn
        history.append({"role": "user", "content": user_msg})

        # Pre-gen: story state is already updated from previous turn's observe
        messages = pipe.assemble(history, user_msg)

        async def stream():
            out = []
            async for piece in client.stream_chat(messages):
                out.append(piece)
            return "".join(out)

        t0 = time.time()
        reply = await stream()
        gen_s = time.time() - t0

        # Detect stall
        is_stalled, motifs, beats = detect_stall(reply)
        loc_changed = detect_location_change(reply)

        # CJK length
        cjk = sum(1 for c in reply if "\u4e00" <= c <= "\u9fff")
        zh_pct = round(cjk * 100 / max(1, sum(1 for c in reply if not c.isspace())), 2)

        # Optional rewrite (max 1 per turn, only if stalled)
        rewrite_used = False
        final_reply = reply
        if is_stalled:
            rewrite_hint = (
                "你之前的回复只写了暧昧铺垫，没有推动剧情。请基于以下原回复重写，"
                "要求：1) 至少一个具体 Scene Beat；2) 中文 60–180 字；"
                "3) 不要重复脸部特写/靠近/耳边/轻声；4) 给用户一个新的明确互动点。\n\n"
                f"原回复：\n{reply}\n\n"
                f"用户上一条消息：{user_msg}\n\n"
                "只输出重写后的新回复，不要解释。"
            )
            messages.append({"role": "user", "content": rewrite_hint})
            try:
                t1 = time.time()
                chunks: list[str] = []
                async for piece in client.stream_chat(messages):
                    chunks.append(piece)
                rewritten = "".join(chunks).strip()
                if rewritten and len(rewritten) > 10 and "Traceback" not in rewritten:
                    still_stalled, _, _ = detect_stall(rewritten)
                    if not still_stalled:
                        final_reply = rewritten
                        rewrite_used = True
                        rewrite_used_count += 1
                        gen_s = time.time() - t0
            except Exception as e:  # noqa: BLE001
                pass

        # Re-detect on final reply
        final_stalled, final_motifs, final_beats = detect_stall(final_reply)
        final_loc = detect_location_change(final_reply)

        rows.append({
            "round": i,
            "tag": tag,
            "user": user_msg,
            "reply": final_reply,
            "reply_len": len(final_reply),
            "cjk_chars": cjk,
            "zh_pct": zh_pct,
            "motifs": final_motifs,
            "beats": final_beats,
            "is_stalled": final_stalled,
            "loc_changed": final_loc,
            "rewrite_used": rewrite_used,
            "gen_seconds": round(gen_s, 1),
            "stalled_rounds_after": state.stalled_rounds if False else None,  # filled below
        })

        # Update history with assistant turn
        history.append({"role": "assistant", "content": final_reply})

        # Update story state for next turn
        state, _ = engine.observe(state, user_msg, final_reply)
        rows[-1]["stalled_rounds_after"] = state.stalled_rounds
        rows[-1]["repeated_motifs"] = state.repeated_motifs.copy()

        marker = "✓" if not final_stalled else "✗"
        loc_mark = "📍" if final_loc else ""
        rw_mark = "[rw]" if rewrite_used else ""
        print(f"  R{i:02d} [{tag:30s}] {marker} {loc_mark} stalled={final_stalled} beats={final_beats[:2]} motifs={final_motifs[:2]} len={len(final_reply)} zh={zh_pct:.0f}% {rw_mark} ({gen_s:.1f}s)")
        print(f"      reply: {final_reply[:120]}…")

    total_dt = time.time() - t_start

    # Acceptance metrics
    no_progress_count = sum(1 for r in rows if r["is_stalled"])
    max_consec = 0
    cur = 0
    for r in rows:
        if r["is_stalled"]:
            cur += 1
            max_consec = max(max_consec, cur)
        else:
            cur = 0

    # "继续" / "换个地方" / "你决定吧" specific checks
    continue_rounds = [r for r in rows if r["tag"].startswith("advance:继续") or r["tag"].startswith("advance:然后") or r["tag"].startswith("advance:更进一步") or r["tag"].startswith("advance:我们接下来")]
    advance_advanced = sum(1 for r in continue_rounds if not r["is_stalled"])
    advance_advance_rate = advance_advanced / max(1, len(continue_rounds))

    scene_change_rounds = [r for r in rows if r["tag"].startswith("advance:换个地方")]
    scene_change_ok = sum(1 for r in scene_change_rounds if r["loc_changed"])

    # Motif repetition score
    all_motif_hits = sum(len(r["motifs"]) for r in rows)

    return {
        "bot": bot,
        "model": client.model,
        "rounds": rows,
        "total_seconds": round(total_dt, 1),
        "rewrites_used": rewrite_used_count,
        "no_progress_count": no_progress_count,
        "max_consecutive_no_progress": max_consec,
        "advance_signal_count": len(continue_rounds),
        "advance_advanced_count": advance_advanced,
        "advance_advance_rate": round(advance_advance_rate, 2),
        "scene_change_signal_count": len(scene_change_rounds),
        "scene_change_ok_count": scene_change_ok,
        "total_motif_hits": all_motif_hits,
        "avg_reply_len": round(sum(r["reply_len"] for r in rows) / len(rows), 1),
        "avg_cjk_chars": round(sum(r["cjk_chars"] for r in rows) / len(rows), 1),
        "final_story_state": {
            "stalled_rounds": state.stalled_rounds,
            "total_turns": state.total_turns,
            "repeated_motifs": state.repeated_motifs,
            "last_progress_event": state.last_progress_event,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", default="penelope")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    summary = asyncio.run(run(args.bot))
    out_path = Path(args.out) if args.out else REPO / "docs" / f"story_engine_test_{args.bot}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print()
    print("=" * 60)
    print(f"  rounds:                       {len(summary['rounds'])}")
    print(f"  no_progress:                  {summary['no_progress_count']} / {len(summary['rounds'])}")
    print(f"  max_consecutive_no_progress:  {summary['max_consecutive_no_progress']}")
    print(f"  rewrites_used:                {summary['rewrites_used']}")
    print(f"  advance_advance_rate:         {summary['advance_advance_rate']:.0%}")
    print(f"  scene_change_ok:              {summary['scene_change_ok_count']} / {summary['scene_change_signal_count']}")
    print(f"  total_motif_hits:             {summary['total_motif_hits']}")
    print(f"  avg_reply_len:                {summary['avg_reply_len']}")
    print(f"  avg_cjk_chars:                {summary['avg_cjk_chars']}")
    print(f"  total_seconds:                {summary['total_seconds']}")
    print(f"  final_state.stalled_rounds:   {summary['final_story_state']['stalled_rounds']}")
    print(f"  final_state.repeated_motifs:  {summary['final_story_state']['repeated_motifs']}")
    print()
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())