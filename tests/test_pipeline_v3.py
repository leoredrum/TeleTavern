#!/usr/bin/env python3
"""Offline V3 pipeline checks (no Telegram, no Ollama).

Loads the real DM card through CharacterRuntime and asserts:
  - World Info triggered by the pending user message lands before char defs;
  - the IN_CHAT anchor at depth 1 sits right before the newest user message;
  - PHI comes AFTER chat history (V1 emitted it before — fixed in V3);
  - token budget trims oldest history first;
  - custom language_override replaces the generic one;
  - greeting() substitutes {{user}}/{{char}} and supports alternates.

Run:  cd ~/Documents/telegramtavern && ./venv/bin/python tests/test_pipeline_v3.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TAVERN_DATA_DIR", str(ROOT / "data-v3"))

from tavern.config import BotConfig  # noqa: E402
from tavern.engine import CharacterRuntime  # noqa: E402

DATA = Path(os.environ["TAVERN_DATA_DIR"])
DM_CARD = "aicc-2025-07-24_DungeonMaster12.png"
fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    if not (DATA / "characters" / DM_CARD).exists():
        print("skip: DM card missing in", DATA)
        return 0
    cfg = BotConfig(name="t", mode="rpg", characters=[DM_CARD], worlds=["Eldoria.json"],
                    language_override="[LANGUAGE OVERRIDE TEST] 用中文。", user_label="玩家",
                    max_context=16384, max_tokens=1024)
    rt = CharacterRuntime(cfg, DM_CARD, DATA)
    check(len(rt.worldinfo.entries) == 68 + 4, f"card book + Eldoria stacked ({len(rt.worldinfo.entries)} entries)")

    key = next(e.keys[0] for e in rt.worldinfo.entries if e.keys and not e.constant)
    history = [{"role": "assistant", "content": "开场。"}, {"role": "user", "content": f"我查看 {key}"}]
    anchor = rt.anchor_item("==ANCHOR==", depth=1, iid="world_state")
    msgs = rt.build_messages(history, history[-1]["content"], extra_items=[anchor])
    texts = [m["content"] for m in msgs]

    wi_idx = next((i for i, t in enumerate(texts) if rt.pipeline.last_worldinfo.before_char and
                   rt.pipeline.last_worldinfo.before_char.splitlines()[0] in t), None)
    desc_idx = next((i for i, t in enumerate(texts) if rt.card.description[:40] in t), None)
    check(wi_idx is not None and desc_idx is not None and wi_idx < desc_idx,
          f"World Info before char defs (wi={wi_idx}, defs={desc_idx})")

    last_user = max(i for i, m in enumerate(msgs) if m["role"] == "user")
    check(texts[last_user - 1] == "==ANCHOR==", "anchor depth 1 sits right before newest user message")

    phi_text = (rt.card.post_history_instructions or "").strip()[:30]
    # the DM card repeats its PHI text inside a constant lore entry, so take the LAST occurrence
    phi_idx = max((i for i, t in enumerate(texts) if phi_text and phi_text in t), default=None)
    check(phi_idx is not None and phi_idx > last_user, f"PHI after chat history (phi={phi_idx}, user={last_user})")

    lang_idx = next((i for i, t in enumerate(texts) if "[LANGUAGE OVERRIDE TEST]" in t), None)
    check(lang_idx is not None and not any("Always reply in" in t for t in texts),
          "custom language_override replaces the generic one")

    long_hist = [{"role": "user" if i % 2 else "assistant", "content": "字" * 400 + f"#{i}"} for i in range(80)]
    msgs2 = rt.build_messages(long_hist, long_hist[-1]["content"])
    kept = [t for t in (m["content"] for m in msgs2) if t.startswith("字")]
    check(0 < len(kept) < 80 and kept[-1].endswith("#79") and not kept[0].endswith("#0"),
          f"token budget trims oldest first (kept {len(kept)}/80)")

    g0, g2 = rt.greeting(), rt.greeting(2)
    check("{{user}}" not in g0 and "{{char}}" not in g0, "greeting placeholders substituted")
    check(g2 != g0 or not rt.card.alternate_greetings, "alternate greeting selectable")

    print("\n" + ("PASS" if not fails else f"FAIL ({len(fails)}):\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
