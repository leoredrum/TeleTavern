#!/usr/bin/env python3
"""Offline RPG-mode checks: engines wire up from the real dungeon-master.yaml,
sessions/turn log/exports work, the anchor block renders, and a scripted
state round-trip (register → death → revive flagged → correction rendered)
runs through the ported engines without Telegram or Ollama.

Run:  cd ~/Documents/telegramtavern && ./venv/bin/python tests/test_rpg_v3.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SRC = Path(os.environ.get("TAVERN_DATA_DIR", ROOT / "data-v3"))

from tavern.config import load_bots  # noqa: E402
from tavern.modes.rpg import RPGBot, detect_character  # noqa: E402
from tavern.rpg import game_state as G  # noqa: E402
from tavern.rpg import state_extractor as SX  # noqa: E402

fails: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tavern-rpg-"))
    for sub in ("characters", "worlds", "bots"):
        shutil.copytree(SRC / sub, tmp / sub)
    (tmp / "data").mkdir()
    (tmp / "logs").mkdir()
    (tmp / ".env").write_text("TG_TOKEN_DM=0:test\n")
    cfg = next(b for b in load_bots(tmp) if b.name == "dungeon-master")
    bot = RPGBot(cfg, tmp)
    check(bot.rule is not None and bot.director is not None, "DM yaml enables RuleEngine + DirectorEngine")

    s = bot.sessions.create(chat_id=1)
    sid = s["session_id"]
    bot.gsm.init_state(sid)
    bot.rule.init_state(sid)
    bot.director.init_state(sid)
    st = bot.gsm.get_or_init(sid)
    anchor = bot.anchor_text(st, sid)
    check("CURRENT WORLD STATE" in anchor and "STATE GUARD" in anchor and "RPG RULE GUARD" in anchor
          and "DIRECTOR GUARD" in anchor, "anchor block contains world state + all three guards")

    check(detect_character("我是一个叫阿尔的精灵游侠") == ("精灵", "游侠"), "race/class detection")
    bot.rule.create_character(sid, "阿尔", "精灵", "游侠", turn=0)
    check("阿尔" in bot.rule.render_rpg_snapshot(sid), "RPG snapshot shows created character")

    # scripted state round-trip through the ported engines
    data1 = {"new_enemies": [{"name": "骸骨法师莫尔甘", "aliases": ["莫尔甘"], "status": "alive"}],
             "deaths": [], "location": None, "battle_active": True, "scene_summary": "BOSS 现身"}
    SX.apply_extraction(bot.gsm, st, 1, data1)
    SX.apply_extraction(bot.gsm, st, 2, {"deaths": ["莫尔甘"], "battle_active": False})
    boss = next(e for e in st.persistent.enemies if "莫尔甘" in e["name"])
    check(boss["status"] == "dead" and "莫尔甘" in boss.get("aliases", []), "alias death resolved to full-name entity")
    conflicts = G.StateValidator(bot.gsm).validate(st, 3, "莫尔甘站起身咆哮。")
    st.narrative.data["last_conflicts"] = [f"{c['category']}:{c['entity']}" for c in conflicts]
    bot.gsm.save(st)
    check(any(c["category"] == "enemy_revive" for c in conflicts), "revive flagged via alias")
    check("上一轮叙述错误" in bot.anchor_text(bot.gsm.get_or_init(sid), sid), "correction line in next anchor")

    bot.sessions.record(s, "player", 1, "我推开石门")
    bot.sessions.record(s, "gm", 1, "门后是莫尔甘。")
    bot.sessions.append_state_md(s, 1, "before", "after", [{"entity": "莫尔甘", "change": "+registered"}], conflicts)
    raw = bot.sessions.raw_log_path(sid).read_text(encoding="utf-8")
    check("第 1 回合" in raw and "世界状态" in raw, "raw_log.md has turn + state block")
    check("第一幕" in bot.sessions.build_script(s) and "小说素材" in bot.sessions.build_notes(s), "script/notes exports")
    check(bot.sessions.list_recent(1)[0]["turn_count"] == 1, "sessions list counts player turns")

    history = [{"role": "user", "content": "我推开石门"}]
    msgs = bot.rt.build_messages(history, "我推开石门",
                                 extra_items=[bot.rt.anchor_item(bot.anchor_text(st, sid), depth=1, iid="ws")])
    check(any("CURRENT WORLD STATE" in m["content"] for m in msgs), "anchor present in assembled messages")
    check(any(cfg.language_override[:30] in m["content"] for m in msgs), "DM language override present")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + ("PASS" if not fails else f"FAIL ({len(fails)}):\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
