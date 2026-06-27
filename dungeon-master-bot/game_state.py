#!/usr/bin/env python3
"""
game_state.py — Game State Engine (Single Source of Truth) for Dungeon Master.

The PROGRAM owns world FACTS; the LLM only DESCRIBES. The LLM can never write
to the state DB. Each turn:
  1. the current world state is injected into the system prompt (anchoring);
  2. the LLM generates a reply;
  3. a rule-based StateUpdater extracts detected changes from the reply;
  4. a rule-based StateValidator flags contradictions vs the state;
  5. every change is recorded to the Timeline (state_history).

Tables ADDED (legacy tables + game_sessions/game_turns are KEPT, never dropped):
  world_state (session_id PK, persistent_json, narrative_json, turn_number, updated_at)
  state_flags (session_id, flag_key, flag_value, turn_number, set_at) PK(session_id, flag_key)
  state_history (id PK, session_id, turn_number, category, event, before_json,
                 after_json, delta_json, conflict_json, dialogue, created_at)

State Updater / Validator are RULE-BASED (no AI). They cover the common,
acceptance-critical cases (enemy death, trap disable, door open, location
change, loot pickup, damage/heal; revival / trap-reactivation / location-jump
detection). They are heuristic and extensible — see STATE_UPDATER.md /
STATE_VALIDATOR.md.
"""
import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

# --------------------------------------------------------------------------- #
# Prompt Guard (Phase 7) — appended to the system prompt every turn.
# --------------------------------------------------------------------------- #
STATE_GUARD = """[STATE GUARD — 最高优先级 / HIGHEST PRIORITY]
- 永远不要凭空发明一个新地点（Never invent a new location）。
- 永远不要传送玩家（Never teleport the player / 不得无故改变 Location）。
- 永远不要复活已死亡的 NPC 或敌人（Never revive dead NPCs/enemies）。
- 永远不要重新激活已解除的陷阱（Never recreate removed/disabled traps）。
- 永远不要忽视「CURRENT WORLD STATE」。
- 「CURRENT WORLD STATE」永远拥有最高优先级；若聊天历史与之冲突，必须以「CURRENT WORLD STATE」为准。"""

WORLD_STATE_HEADER_TOP = "==================\nCURRENT WORLD STATE\n=================="
WORLD_STATE_HEADER_BOT = "=================="


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Default state templates
# --------------------------------------------------------------------------- #
DEFAULT_PERSISTENT = {
    "location": "",
    "area": "",
    "room": "",
    "current_scene": "",
    "current_objective": "",
    "current_quest": "",
    "weather": "",
    "time_of_day": "",
    "environment": "",
    "player": {"name": "Unknown Hero", "hp": 20, "max_hp": 20, "ac": 12, "status": "正常"},
    "party": [],
    "npcs": [],
    "enemies": [],          # [{"name":..., "status":"alive|dead", "hp":int}]
    "loot": [],             # [{"name":..., "taken":bool}]
    "containers": [],
    "doors": [],            # [{"name":..., "status":"open|closed|locked"}]
    "traps": [],            # [{"name":..., "status":"enabled|disabled"}]
    "puzzle": {"status": "unsolved", "note": ""},
    "battle": {"active": False, "round": 0},
    "inventory": [],
    "player_status": "正常",
    "world_vars": {},       # free-form; "locations": {name: [signature keywords]}
}

DEFAULT_NARRATIVE = {
    "scene_summary": "",
    "mood": "",
    "tension": "",
    "recent_events": [],    # list of short strings
    "last_dialogue_summary": "",
    "story_beat": "",
}


def _merge(default: dict, override: dict) -> dict:
    """Deep-merge override into a copy of default (one level for sub-dicts/lists)."""
    out = json.loads(json.dumps(default))  # deep copy
    if not override:
        return out
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- #
# State containers
# --------------------------------------------------------------------------- #
class PersistentState:
    """Wraps the persistent (factual) world state for one session."""

    def __init__(self, data: dict | None = None):
        self.data = _merge(DEFAULT_PERSISTENT, data or {})

    def to_dict(self) -> dict:
        return self.data

    # convenience accessors
    @property
    def enemies(self):
        return self.data.setdefault("enemies", [])

    @property
    def traps(self):
        return self.data.setdefault("traps", [])

    @property
    def doors(self):
        return self.data.setdefault("doors", [])

    @property
    def npcs(self):
        return self.data.setdefault("npcs", [])

    @property
    def loot(self):
        return self.data.setdefault("loot", [])

    @property
    def inventory(self):
        return self.data.setdefault("inventory", [])

    @property
    def player(self):
        return self.data.setdefault("player", {"name": "Unknown Hero", "hp": 20, "max_hp": 20, "ac": 12, "status": "正常"})


class NarrativeState:
    """The narrative/expressive layer the LLM is free to shape."""

    def __init__(self, data: dict | None = None):
        self.data = _merge(DEFAULT_NARRATIVE, data or {})

    def to_dict(self) -> dict:
        return self.data


class WorldSnapshot:
    """A point-in-time copy of persistent + narrative state (for Timeline)."""

    def __init__(self, persistent: dict, narrative: dict, turn: int):
        self.persistent = json.loads(json.dumps(persistent))
        self.narrative = json.loads(json.dumps(narrative))
        self.turn = turn


class SessionState:
    """The full state for one session."""

    def __init__(self, session_id: str, persistent: PersistentState,
                 narrative: NarrativeState, turn: int):
        self.session_id = session_id
        self.persistent = persistent
        self.narrative = narrative
        self.turn = turn


# --------------------------------------------------------------------------- #
# GameStateManager — DB + logic
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS world_state(
  session_id TEXT PRIMARY KEY,
  persistent_json TEXT NOT NULL,
  narrative_json TEXT NOT NULL,
  turn_number INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS state_flags(
  session_id TEXT NOT NULL,
  flag_key TEXT NOT NULL,
  flag_value TEXT,
  turn_number INTEGER,
  set_at TEXT,
  PRIMARY KEY (session_id, flag_key)
);
CREATE TABLE IF NOT EXISTS state_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_number INTEGER,
  category TEXT,
  event TEXT,
  before_json TEXT,
  after_json TEXT,
  delta_json TEXT,
  conflict_json TEXT,
  dialogue TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_history_session
  ON state_history(session_id, turn_number);
"""


class GameStateManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with closing(self._conn()) as db:
            db.executescript(SCHEMA)

    # ---- core load / save ----------------------------------------------------
    def load(self, session_id: str):
        """Return SessionState or None if no state row exists."""
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM world_state WHERE session_id=?",
                             (session_id,)).fetchone()
        if not row:
            return None
        return SessionState(
            session_id=session_id,
            persistent=PersistentState(json.loads(row["persistent_json"])),
            narrative=NarrativeState(json.loads(row["narrative_json"])),
            turn=int(row["turn_number"] or 0),
        )

    def save(self, st: SessionState) -> None:
        pj = json.dumps(st.persistent.to_dict(), ensure_ascii=False)
        nj = json.dumps(st.narrative.to_dict(), ensure_ascii=False)
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO world_state(session_id, persistent_json, narrative_json, "
                "turn_number, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET persistent_json=excluded.persistent_json, "
                "narrative_json=excluded.narrative_json, turn_number=excluded.turn_number, "
                "updated_at=excluded.updated_at",
                (st.session_id, pj, nj, st.turn, _now_iso()))
            db.commit()

    def init_state(self, session_id: str, persistent_override: dict | None = None,
                   narrative_override: dict | None = None, turn: int = 0) -> SessionState:
        """Create a fresh state row for a session (used on /newgame). Idempotent insert."""
        st = SessionState(
            session_id=session_id,
            persistent=PersistentState(persistent_override),
            narrative=NarrativeState(narrative_override),
            turn=turn,
        )
        self.save(st)
        # seed the location flag so flags reflect the authoritative location from turn 0
        loc = st.persistent.to_dict().get("location")
        if loc:
            self.set_flag(session_id, "location", loc, turn)
        return st

    def get_or_init(self, session_id: str) -> SessionState:
        st = self.load(session_id)
        return st if st else self.init_state(session_id)

    # ---- flags ---------------------------------------------------------------
    def set_flag(self, session_id: str, key: str, value: str, turn: int) -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO state_flags(session_id, flag_key, flag_value, turn_number, set_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_id, flag_key) DO UPDATE SET "
                "flag_value=excluded.flag_value, turn_number=excluded.turn_number, set_at=excluded.set_at",
                (session_id, key, str(value), turn, _now_iso()))
            db.commit()

    def get_flag(self, session_id: str, key: str):
        with closing(self._conn()) as db:
            r = db.execute("SELECT flag_value FROM state_flags WHERE session_id=? AND flag_key=?",
                           (session_id, key)).fetchone()
            return r[0] if r else None

    def all_flags(self, session_id: str) -> dict:
        with closing(self._conn()) as db:
            rows = db.execute("SELECT flag_key, flag_value FROM state_flags WHERE session_id=?",
                              (session_id,)).fetchall()
            return {r[0]: r[1] for r in rows}

    # ---- timeline / history --------------------------------------------------
    def append_history(self, session_id: str, turn: int, category: str, event: str,
                       before_json: str = "", after_json: str = "", delta_json: str = "",
                       conflict_json: str = "", dialogue: str = "") -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO state_history(session_id, turn_number, category, event, "
                "before_json, after_json, delta_json, conflict_json, dialogue, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, turn, category, event, before_json, after_json,
                 delta_json, conflict_json, (dialogue or "")[:4000], _now_iso()))
            db.commit()

    def get_timeline(self, session_id: str):
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM state_history WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---- rendering -----------------------------------------------------------
    def render_block(self, st: SessionState) -> str:
        """Render the CURRENT WORLD STATE block for prompt injection (Phase 3)."""
        p = st.persistent.to_dict()
        n = st.narrative.to_dict()
        flags = self.all_flags(st.session_id)

        def _list(items, fmt):
            items = items or []
            if not items:
                return "（无）"
            return "; ".join(fmt(i) for i in items)

        def _empt(v):
            return v if v else "（未设定）"

        def _ent(i):           # name(status) for npc/enemy/door/trap
            return "%s(%s)" % (i.get("name", "?"), i.get("status", "?"))

        def _loot(i):          # name + ✓已取 marker
            return i.get("name", "?") + ("✓已取" if i.get("taken") else "")

        lines = [
            WORLD_STATE_HEADER_TOP,
            "Location: " + _empt(p.get("location")),
            "Current Room: " + _empt(p.get("room") or p.get("area")),
            "Scene: " + _empt(p.get("current_scene")),
            "Current Objective: " + _empt(p.get("current_objective")),
            "Current Quest: " + _empt(p.get("current_quest")),
            "NPCs: " + _list(p.get("npcs"), _ent),
            "Enemies: " + _list(p.get("enemies"), _ent),
            "Loot: " + _list(p.get("loot"), _loot),
            "Doors: " + _list(p.get("doors"), _ent),
            "Traps: " + _list(p.get("traps"), _ent),
            "Environment: " + _empt(p.get("environment")),
            "Weather: " + _empt(p.get("weather")),
            "Time: " + _empt(p.get("time_of_day")),
            "Flags: " + ("; ".join("%s=%s" % (k, v) for k, v in flags.items()) if flags else "（无）"),
            "Recent Events: " + ("; ".join(n.get("recent_events") or []) or "（无）"),
            "Scene Summary: " + (n.get("scene_summary") or "（无）"),
            WORLD_STATE_HEADER_BOT,
        ]
        return "\n".join(lines)

    def render_compact(self, st: SessionState) -> str:
        """Compact one-line state for the raw_log per-turn block (Phase 9)."""
        p = st.persistent.to_dict()
        flags = self.all_flags(st.session_id)
        parts = [f"Location={p.get('location') or '?'}"]
        ens = [f"{e.get('name','?')}({e.get('status','?')})" for e in (p.get('enemies') or [])]
        if ens:
            parts.append("Enemies=" + ",".join(ens))
        trs = [f"{t.get('name','?')}({t.get('status','?')})" for t in (p.get('traps') or [])]
        if trs:
            parts.append("Traps=" + ",".join(trs))
        inv = p.get('inventory') or []
        if inv:
            parts.append("Inv=" + ",".join(str(x) for x in inv))
        if flags:
            parts.append("Flags=" + ";".join(f"{k}={v}" for k, v in flags.items()))
        return " | ".join(parts)


# --------------------------------------------------------------------------- #
# StateUpdater — rule-based (Phase 4). Returns list of change dicts.
# --------------------------------------------------------------------------- #
_PLACE_SUFFIX = ("室", "厅", "馆", "窟", "洞", "塔", "堡", "地", "营", "村", "城",
                 "房", "院", "庙", "渊", "谷", "林", "山", "道", "桥", "街")


class StateUpdater:
    """Parses the LLM reply (and player text) and applies detected state changes.

    All pattern matching is Chinese regex, rule-based, extensible. Each applied
    change mutates the SessionState and records a Timeline delta.
    """

    def __init__(self, mgr: GameStateManager):
        self.mgr = mgr

    def update(self, st: SessionState, turn: int, player_text: str,
               dm_reply: str) -> list:
        """Apply detected changes to `st` in place; return list of change dicts."""
        changes = []
        reply = dm_reply or ""
        ptxt = player_text or ""
        p = st.persistent

        # 1) enemy death  (match known enemies by name; status not already dead)
        for e in p.enemies:
            if e.get("status") == "dead":
                continue
            name = e.get("name", "")
            if name and re.search(rf"{re.escape(name)}.*?(死亡|死去|倒下|倒地身亡|被击杀|击杀|咽下|归零|灰飞烟灭|化为灰烬|碎裂|毙命|殒命|倒在了|已经死了|生命消散|轰然倒地)", reply):
                e["status"] = "dead"
                self.mgr.set_flag(st.session_id, f"enemy:{name}", "dead", turn)
                self.mgr.set_flag(st.session_id, "boss_defeated" if "boss" in name.lower() or "大法师" in name or "魔王" in name else f"defeated:{name}", "true", turn)
                changes.append({"category": "enemy", "entity": name, "change": "status→dead"})

        # 2) trap disabled
        for t in p.traps:
            if t.get("status") == "disabled":
                continue
            name = t.get("name", "")
            if name and re.search(rf"({re.escape(name)}.*?(解除|失效|被破坏|拆除|失去作用|停下|卡住|报废)|(解除|拆除|破坏|拆掉)了?.{{0,6}}{re.escape(name)})", reply):
                t["status"] = "disabled"
                self.mgr.set_flag(st.session_id, f"trap:{name}", "disabled", turn)
                changes.append({"category": "trap", "entity": name, "change": "status→disabled"})

        # 3) door open
        for d in p.doors:
            if d.get("status") == "open":
                continue
            name = d.get("name", "")
            if name and re.search(rf"({re.escape(name)}.*?(打开|推开|开启)|(打开|推开|开启)了?.{{0,6}}{re.escape(name)})", reply):
                d["status"] = "open"
                self.mgr.set_flag(st.session_id, f"door:{name}", "open", turn)
                changes.append({"category": "door", "entity": name, "change": "status→open"})

        # 4) location change (player intent or DM narration)
        loc = self._detect_location(ptxt + "\n" + reply, p)
        if loc and loc != p.data.get("location"):
            old = p.data.get("location")
            p.data["location"] = loc
            self.mgr.set_flag(st.session_id, "location", loc, turn)
            changes.append({"category": "location", "entity": loc, "change": f"{old or '?'}→{loc}"})

        # 5) loot picked up — known loot names take priority; free-extract only if
        #    clearly a real item (strip demonstrative/classifier, drop generic words)
        _PICKUP = r"(?:拾起|捡起|拿起|收起|获得|得到|入手|装起)"
        _LOOT_STOP = {
            "战利品", "物品", "东西", "奖励", "财富", "宝物", "装备", "道具",
            "金币", "银币", "铜币", "金钱", "财宝", "收获", "战果", "宝藏", "宝箱",
        }
        _CL = r"(?:[那这][根把件个条块张本颗枚瓶袋套]?\s*|[一二三四五六七八九十两]?[件根把个条块张本颗枚瓶袋套]\s*)"
        # (a) match KNOWN loot names (allow up to 10 chars between verb and the name)
        for lo in p.loot:
            nm = lo.get("name", "")
            if nm and not lo.get("taken") and re.search(
                    rf"{_PICKUP}[^。！？\n]{{0,10}}{re.escape(nm)}", reply):
                lo["taken"] = True
                if nm not in p.inventory:
                    p.inventory.append(nm)
                self.mgr.set_flag(st.session_id, f"loot:{nm}", "taken", turn)
                changes.append({"category": "loot", "entity": nm, "change": "+inventory/taken"})
        # (b) free extraction — strip leading demonstrative/classifier, drop generic words
        for mm in re.finditer(
                rf"{_PICKUP}\s*(?:了?\s*)?(?:{_CL})?(?:「|【|《)?([一-龥A-Za-z·]{{2,14}})(?:」|】|》)?",
                reply):
            item = mm.group(1).strip()
            if (item in _LOOT_STOP or item in p.inventory
                    or not self._looks_like_item(item)
                    or any(sw and sw in item for sw in _LOOT_STOP)):
                continue
            if any(lo.get("name") == item for lo in p.loot):  # handled by (a)
                continue
            if item.startswith(("那", "这", "一", "些", "个", "根", "把", "件",
                                "块", "条", "张", "本", "颗", "枚", "瓶", "袋", "套",
                                "两", "由", "以", "用", "某")):
                continue
            if item.endswith("的"):   # modifier clause (e.g. 由黑檀木制成的)
                continue
            p.inventory.append(item)
            self.mgr.set_flag(st.session_id, f"loot:{item}", "taken", turn)
            changes.append({"category": "loot", "entity": item, "change": "+inventory"})

        # 6) damage / heal to player
        for mm in re.finditer(r"(?:受到|损失|扣除|承受)(?:了|\s)*(\d+)\s*(?:点)?\s*(?:伤害|生命值)", reply):
            try:
                dmg = int(mm.group(1))
                p.player["hp"] = max(0, int(p.player.get("hp", 0)) - dmg)
                changes.append({"category": "player", "entity": "hp", "change": f"-{dmg}→{p.player['hp']}"})
            except ValueError:
                pass
        for mm in re.finditer(r"(?:恢复|回复|治疗|增加)(?:了|\s)*(\d+)\s*(?:点)?\s*(?:生命值|生命|血)", reply):
            try:
                heal = int(mm.group(1))
                p.player["hp"] = min(int(p.player.get("max_hp", 999)), int(p.player.get("hp", 0)) + heal)
                changes.append({"category": "player", "entity": "hp", "change": f"+{heal}→{p.player['hp']}"})
            except ValueError:
                pass

        # 7) record timeline deltas
        for c in changes:
            self.mgr.append_history(st.session_id, turn, c["category"],
                                    f"{c['entity']}: {c['change']}", delta_json=json.dumps(c, ensure_ascii=False))
        st.turn = turn
        self.mgr.save(st)
        return changes

    def _detect_location(self, text: str, p: PersistentState) -> str | None:
        known = []
        locs = p.data.get("world_vars", {}).get("locations") or {}
        for name in locs:
            known.append(name)
        m = re.search(r"(?:进入|来到|走入|走进|抵达|回到|返回|推门进入|踏[入进])\s*(?:了?\s*)?(?:了?\s*)?([一-龥A-Za-z·]{2,10})", text)
        if not m:
            return None
        cand = m.group(1).strip()
        # accept if known location, or ends with a place suffix
        if cand in known:
            return cand
        if any(cand.endswith(s) for s in _PLACE_SUFFIX):
            return cand
        return None

    def _looks_like_item(self, s: str) -> bool:
        return len(s) >= 2 and not s.startswith(("你", "我", "他", "她", "它"))


# --------------------------------------------------------------------------- #
# StateValidator — rule-based (Phase 5). Returns list of conflict dicts.
# Records conflicts to Timeline but does NOT auto-fix.
# --------------------------------------------------------------------------- #
class StateValidator:
    def __init__(self, mgr: GameStateManager):
        self.mgr = mgr

    def validate(self, st: SessionState, turn: int, dm_reply: str) -> list:
        reply = dm_reply or ""
        p = st.persistent
        conflicts = []

        # 1) dead entity acting again (revival)
        for e in p.enemies:
            if e.get("status") == "dead":
                name = e.get("name", "")
                if name and re.search(rf"{re.escape(name)}.*?(攻击|说[道话]?|喊|站起|站了|冲过来|挥|笑|咆哮|举起|扑|咒骂)", reply):
                    conflicts.append({"category": "enemy_revive", "entity": name,
                                      "detail": "已死亡的敌人在回复中表现出存活行为"})
        for npc in p.npcs:
            if npc.get("status") == "dead":
                name = npc.get("name", "")
                if name and re.search(rf"{re.escape(name)}.*?(说[道话]?|笑|点头|递|招呼|站)", reply):
                    conflicts.append({"category": "npc_revive", "entity": name,
                                      "detail": "已死亡的 NPC 在回复中表现出存活行为"})

        # 2) disabled trap reactivating
        for t in p.traps:
            if t.get("status") == "disabled":
                name = t.get("name", "")
                if name and re.search(rf"({re.escape(name)}.*?(触发|启动|激活|发动)|(触发|踩到|触动|启动).{{0,6}}{re.escape(name)})", reply):
                    conflicts.append({"category": "trap_reactivate", "entity": name,
                                      "detail": "已解除的陷阱在回复中被触发"})

        # 3) location jump
        cur = p.data.get("location", "")
        if cur:
            locs = p.data.get("world_vars", {}).get("locations") or {}
            cur_sigs = locs.get(cur) or [cur]
            if not any(sig and sig in reply for sig in cur_sigs):
                # current location not mentioned — does another known location appear?
                for other, sigs in locs.items():
                    if other == cur:
                        continue
                    if any(sig and sig in reply for sig in (sigs or [other])):
                        conflicts.append({"category": "location_jump", "entity": other,
                                          "detail": f"当前地点={cur}，但回复出现 {other} 的场景标志"})
                        break

        # record conflicts to timeline (no auto-fix)
        for c in conflicts:
            self.mgr.append_history(st.session_id, turn, "conflict",
                                    f"{c['category']}:{c['entity']}",
                                    conflict_json=json.dumps(c, ensure_ascii=False),
                                    dialogue=reply[:600])
        return conflicts
