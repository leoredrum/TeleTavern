#!/usr/bin/env python3
"""
state_extractor.py — LLM-assisted world-state extraction for Dungeon Master.

Why: the rule-based StateUpdater only tracks entities that were PRE-SEEDED in
the world state and only fires on a narrow set of verbs. In live play nearly
every turn logged `narrative=0`, so new bosses were never registered, deaths
were never recorded, and location drift went unnoticed. This module asks a
small local model (default qwen3:14b, thinking disabled, JSON mode) to read the
DM reply and emit a structured delta, which is then MERGED into the persistent
state through the same `changes` dict format the regex updater uses — so
RuleEngine / Director approval chains are untouched.

The LLM still never writes the DB directly; `apply_extraction` is the only
writer and it refuses to revive dead entities or duplicate names.
"""
from __future__ import annotations

import json
import logging
import os
import re

import aiohttp

log = logging.getLogger("dm-bot")

OLLAMA_URL = os.environ.get("EXTRACT_OLLAMA_URL", "http://127.0.0.1:11434")
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "qwen3:14b")
EXTRACT_TIMEOUT_S = float(os.environ.get("EXTRACT_TIMEOUT_S", "45"))
EXTRACT_ENABLED = os.environ.get("EXTRACT_ENABLED", "1") not in ("0", "false", "no")

_SYSTEM = """你是TRPG世界状态记录员。阅读「当前世界状态」和「DM本轮叙述」，只输出JSON，不要解释。
规则：
- 只记录叙述里明确发生的事实，不要推测。
- name用叙述里的原文全名；aliases列出叙述中可能单独使用的短名/称呼（如全名「骸骨法师莫尔甘」→aliases["莫尔甘"]）。
- 已在状态里标记dead的实体不得出现在alive列表。
- location只在玩家/队伍确实移动到新地点时填写，否则为null。
JSON格式：
{"new_enemies":[{"name":"","aliases":[""],"status":"alive"}],
 "new_npcs":[{"name":"","aliases":[""],"status":"alive"}],
 "deaths":[""],
 "location":null,
 "battle_active":null,
 "scene_summary":""}
battle_active: 本轮结束时是否处于战斗中（true/false），无法判断为null。
scene_summary: 用一句话（≤40字）概括本轮结束时的场面。"""


def _state_digest(p: dict) -> str:
    ens = "; ".join(f"{e.get('name')}({e.get('status')})" for e in (p.get("enemies") or [])) or "无"
    npcs = "; ".join(f"{e.get('name')}({e.get('status')})" for e in (p.get("npcs") or [])) or "无"
    return (f"Location: {p.get('location') or '未设定'}\n"
            f"Enemies: {ens}\nNPCs: {npcs}\n"
            f"Battle: {'进行中' if (p.get('battle') or {}).get('active') else '否'}")


async def extract(persistent: dict, player_text: str, dm_reply: str, *,
                  model: str | None = None, ollama_url: str | None = None,
                  enabled: bool | None = None) -> dict | None:
    """Call the extraction model. Returns parsed dict or None on any failure.

    V3: `model` / `ollama_url` / `enabled` override the env defaults per bot."""
    if not (EXTRACT_ENABLED if enabled is None else enabled) or not dm_reply:
        return None
    user = (f"【当前世界状态】\n{_state_digest(persistent)}\n\n"
            f"【玩家本轮输入】\n{(player_text or '')[:400]}\n\n"
            f"【DM本轮叙述】\n{dm_reply[:3500]}")
    payload = {
        "model": model or EXTRACT_MODEL,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"temperature": 0, "num_ctx": 8192},
        "messages": [{"role": "system", "content": _SYSTEM},
                     {"role": "user", "content": user}],
    }
    try:
        timeout = aiohttp.ClientTimeout(total=EXTRACT_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post((ollama_url or OLLAMA_URL).rstrip("/") + "/api/chat", json=payload) as resp:
                if resp.status != 200:
                    log.warning("extractor http %s", resp.status)
                    return None
                body = await resp.json()
        content = (body.get("message") or {}).get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
        data = json.loads(content)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("extractor failed: %s", exc)
        return None


def _clean_name(n) -> str:
    n = str(n or "").strip().strip("「」『』【】\"'")
    return n if 1 < len(n) <= 20 else ""


def _aliases(e: dict, name: str) -> list:
    out = []
    for a in (e or {}).get("aliases") or []:
        a = _clean_name(a)
        if a and a != name and a not in out:
            out.append(a)
    return out[:4]


def apply_extraction(mgr, st, turn: int, data: dict | None) -> list:
    """Merge extractor output into `st.persistent`; return changes in the
    StateUpdater dict format. Never revives dead entities, never duplicates."""
    changes = []
    if not data:
        return changes
    p = st.persistent
    known_enemies = {e.get("name"): e for e in p.enemies}
    known_npcs = {e.get("name"): e for e in p.npcs}

    for e in data.get("new_enemies") or []:
        name = _clean_name((e or {}).get("name"))
        if not name or name in known_enemies or name in known_npcs:
            continue
        p.enemies.append({"name": name, "aliases": _aliases(e, name), "status": "alive"})
        known_enemies[name] = p.enemies[-1]
        mgr.set_flag(st.session_id, f"enemy:{name}", "alive", turn)
        changes.append({"category": "enemy", "entity": name, "change": "+registered(alive)"})

    for e in data.get("new_npcs") or []:
        name = _clean_name((e or {}).get("name"))
        if not name or name in known_npcs or name in known_enemies:
            continue
        p.npcs.append({"name": name, "aliases": _aliases(e, name), "status": "alive"})
        known_npcs[name] = p.npcs[-1]
        changes.append({"category": "npc", "entity": name, "change": "+registered(alive)"})

    def _resolve(nm: str):
        for table in (known_enemies, known_npcs):
            if nm in table:
                return nm, table[nm]
            for full, ent in table.items():
                if nm in (ent.get("aliases") or []) or (nm and nm in full):
                    return full, ent
        return nm, None

    for raw in data.get("deaths") or []:
        name, ent = _resolve(_clean_name(raw))
        if not ent or ent.get("status") == "dead":
            continue
        ent["status"] = "dead"
        kind = "enemy" if name in known_enemies else "npc"
        mgr.set_flag(st.session_id, f"{kind}:{name}", "dead", turn)
        if kind == "enemy":
            boss = ("boss" in name.lower() or "魔王" in name or "大法师" in name)
            mgr.set_flag(st.session_id, "boss_defeated" if boss else f"defeated:{name}", "true", turn)
        changes.append({"category": kind, "entity": name, "change": "status→dead"})

    loc = _clean_name(data.get("location"))
    if loc and loc != p.data.get("location"):
        old = p.data.get("location")
        p.data["location"] = loc
        mgr.set_flag(st.session_id, "location", loc, turn)
        changes.append({"category": "location", "entity": loc, "change": f"{old or '?'}→{loc}"})

    ba = data.get("battle_active")
    if isinstance(ba, bool):
        battle = p.data.setdefault("battle", {"active": False, "round": 0})
        if battle.get("active") != ba:
            battle["active"] = ba
            battle["round"] = (battle.get("round", 0) + 1) if ba else 0
            changes.append({"category": "battle", "entity": "active", "change": str(ba)})

    summ = str(data.get("scene_summary") or "").strip()
    if summ:
        st.narrative.data["scene_summary"] = summ[:80]

    for c in changes:
        mgr.append_history(st.session_id, turn, c["category"],
                           f"{c['entity']}: {c['change']} (llm)",
                           delta_json=json.dumps(c, ensure_ascii=False))
    if changes or summ:
        mgr.save(st)
    return changes
