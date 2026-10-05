#!/usr/bin/env python3
"""
rpg_engine.py — Structured RPG Rule Engine for Dungeon Master (Phase 3).

The PROGRAM owns all GAME RULES; the LLM only DESCRIBES. The LLM can never
mint XP / gold / loot / levels / stats / inventory by itself. Every numeric
and structural change must be APPROVED and APPLIED by the RuleEngine.

Architecture (sits beside game_state.py, which stays untouched):
  - game_state.py = narrative WORLD FACTS (location / scene / traps / doors /
    NPC disposition). LLM describes, rule-based updater/validator anchor it.
  - rpg_engine.py = GAME RULES (player stats, XP, levels, gold, items, loot,
    quests, enemies, economy). Deterministic. LLM's numbers are IGNORED.

Per-turn pipeline (driven by bot.py):
  1. StateUpdater.update(...) extracts NARRATIVE changes from the DM reply
     (e.g. enemy "status->dead").
  2. RuleEngine.intake(session, turn, changes, dm_reply) turns those into
     RULE changes: enemy death -> grant its fixed xp_reward -> roll its
     loot_table -> add items to available loot / inventory. The XP value
     comes from the ENEMY definition, NOT the LLM text.
  3. RuleEngine.validate_reply(...) flags any LLM-claimed XP/gold/level that
     disagrees with the authoritative program value (conflict, no auto-fix).
  4. Every rule change is logged to rpg_rule_log (Timeline: XP/Gold/...).

Tables ADDED (all legacy + game_state + game_sessions/turns KEPT, never dropped):
  rpg_state    (session_id PK, player_json, enemies_json, quests_json,
                npcs_json, economy_json, turn_number, updated_at)
  rpg_items    (item_id PK, name, name_en, rarity, weight, value, slot,
                effect, desc, stack, unique, quest_item, data_json)
  rpg_inventory(session_id, item_id, qty, equipped, notes) PK(session_id,item_id)
  rpg_rule_log (id PK, session_id, turn_number, category, entity, before_json,
                after_json, delta_json, detail, created_at)

Rules are deterministic and table-driven (D&D 5e-flavored, simplified).
"""
import os
import re
import json
import random
import sqlite3
from datetime import datetime, timezone
from contextlib import closing


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# D&D 5e cumulative XP thresholds (level -> total XP required to reach it).
# --------------------------------------------------------------------------- #
XP_TABLE = {
    1: 0, 2: 300, 3: 900, 4: 2700, 5: 6500, 6: 14000, 7: 23000,
    8: 34000, 9: 48000, 10: 64000, 11: 85000, 12: 100000, 13: 120000,
    14: 140000, 15: 165000, 16: 195000, 17: 225000, 18: 265000,
    19: 305000, 20: 355000,
}


def level_from_xp(xp: int) -> int:
    """Return the level for a given total XP (1..20)."""
    lvl = 1
    for lv in sorted(XP_TABLE):
        if xp >= XP_TABLE[lv]:
            lvl = lv
    return min(lvl, 20)


def xp_to_next(level: int) -> int:
    """XP threshold for level+1, or the cap if at 20."""
    return XP_TABLE.get(min(level + 1, 20), XP_TABLE[20])


# Economy: store everything internally in COPPER. 1 gold = 10 silver = 100 copper.
COPPER_PER_GOLD = 100
COPPER_PER_SILVER = 10


def split_coins(copper: int) -> dict:
    """Copper total -> {gold, silver, copper} for display."""
    gold, rem = divmod(int(copper), COPPER_PER_GOLD)
    silver, copper = divmod(rem, COPPER_PER_SILVER)
    return {"gold": gold, "silver": silver, "copper": copper}


def coins_str(copper: int) -> str:
    c = split_coins(copper)
    parts = []
    if c["gold"]:
        parts.append("%d金币" % c["gold"])
    if c["silver"]:
        parts.append("%d银币" % c["silver"])
    if c["copper"] or not parts:
        parts.append("%d铜币" % c["copper"])
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Class templates — base stats at level 1. (stats use standard 4..18 range.)
#   base_hp, hit_die (avg gain per level), base_ac, prime_stat, spell_slots{level:slots}
# --------------------------------------------------------------------------- #
CLASS_TEMPLATES = {
    "战士": {"en": "Fighter", "base_hp": 12, "hit_die_avg": 7, "base_ac": 16,
            "prime": "力量", "stats": {"力量": 16, "体质": 14, "敏捷": 12, "智力": 10, "感知": 10, "魅力": 10},
            "spell_slots": {}, "abilities": ["回血(第二春)", "战斗风格"]},
    "法师": {"en": "Wizard", "base_hp": 6, "hit_die_avg": 4, "base_ac": 12,
            "prime": "智力", "stats": {"力量": 8, "体质": 10, "敏捷": 12, "智力": 16, "感知": 12, "魅力": 10},
            "spell_slots": {1: 2}, "abilities": ["奥术恢复", "戏法"]},
    "牧师": {"en": "Cleric", "base_hp": 8, "hit_die_avg": 5, "base_ac": 15,
            "prime": "感知", "stats": {"力量": 12, "体质": 12, "敏捷": 10, "智力": 10, "感知": 16, "魅力": 12},
            "spell_slots": {1: 2}, "abilities": ["治疗之言", "神圣法术"]},
    "游荡者": {"en": "Rogue", "base_hp": 8, "hit_die_avg": 5, "base_ac": 14,
             "prime": "敏捷", "stats": {"力量": 10, "体质": 12, "敏捷": 16, "智力": 12, "感知": 10, "魅力": 12},
             "spell_slots": {}, "abilities": ["偷袭", "巧手"]},
    "游侠": {"en": "Ranger", "base_hp": 10, "hit_die_avg": 6, "base_ac": 14,
            "prime": "敏捷", "stats": {"力量": 12, "体质": 12, "敏捷": 16, "智力": 10, "感知": 14, "魅力": 10},
            "spell_slots": {}, "abilities": ["宿敌", "自然探索"]},
    "圣武士": {"en": "Paladin", "base_hp": 10, "hit_die_avg": 6, "base_ac": 16,
             "prime": "魅力", "stats": {"力量": 16, "体质": 14, "敏捷": 10, "智力": 10, "感知": 10, "魅力": 14},
             "spell_slots": {1: 2}, "abilities": ["神圣感知", "圣疗"]},
}

RACE_BONUS = {
    "人类": {"任意属性": 1}, "精灵": {"敏捷": 2}, "矮人": {"体质": 2},
    "半身人": {"敏捷": 2}, "半精灵": {"魅力": 2}, "提夫林": {"魅力": 2, "智力": 1},
    "龙裔": {"力量": 2, "魅力": 1}, "侏儒": {"智力": 2}, "半兽人": {"力量": 2, "体质": 1},
}


# --------------------------------------------------------------------------- #
# Item Registry — global, seed catalogue. value is in COPPER.
# rarity: 普通/非普通/稀有/珍奇/传说/神器. slot: weapon/armor/shield/ring/amulet/consumable/misc/none
# --------------------------------------------------------------------------- #
SEED_ITEMS = [
    # consumables
    {"item_id": "potion_healing", "name": "治疗药水", "name_en": "Potion of Healing", "rarity": "普通",
     "weight": 0.5, "value": 5000, "slot": "consumable", "effect": "恢复2d4+2生命值", "desc": "深红色的治疗药水。", "stack": True},
    {"item_id": "potion_greater_healing", "name": "高级治疗药水", "rarity": "非普通", "weight": 0.5,
     "value": 15000, "slot": "consumable", "effect": "恢复4d4+4生命值", "desc": "蕴含更强治愈之力的药水。", "stack": True},
    {"item_id": "potion_mana", "name": "法力药水", "rarity": "非普通", "weight": 0.5,
     "value": 10000, "slot": "consumable", "effect": "恢复1环法术位", "desc": "幽蓝的法力药水。", "stack": True},
    {"item_id": "antidote", "name": "解毒剂", "rarity": "普通", "weight": 0.2,
     "value": 3000, "slot": "consumable", "effect": "解除中毒", "desc": "草本的解毒剂。", "stack": True},
    # weapons
    {"item_id": "sword_long", "name": "长剑", "name_en": "Longsword", "rarity": "普通",
     "weight": 3.0, "value": 1500, "slot": "weapon", "effect": "1d8挥砍（可单/双手）", "desc": "制式长剑。", "stack": False},
    {"item_id": "dagger", "name": "匕首", "rarity": "普通", "weight": 1.0,
     "value": 200, "slot": "weapon", "effect": "1d4穿刺，灵巧", "desc": "短小的匕首。", "stack": False},
    {"item_id": "bow_short", "name": "短弓", "rarity": "普通", "weight": 2.0,
     "value": 2500, "slot": "weapon", "effect": "1d6穿刺，远程", "desc": "灵活的短弓。", "stack": False},
    {"item_id": "staff_magic", "name": "法师之杖", "name_en": "Magic Staff", "rarity": "稀有",
     "weight": 2.0, "value": 50000, "slot": "weapon", "effect": "法术攻击+1，奥术聚焦", "desc": "刻满符文的法师之杖。", "stack": False},
    {"item_id": "sword_flame", "name": "烈焰之刃", "rarity": "珍奇", "weight": 3.0,
     "value": 200000, "slot": "weapon", "effect": "1d8挥砍+1d6火焰，攻击+1", "desc": "刀身永远燃烧的魔法长剑。", "stack": False},
    # armor / shield
    {"item_id": "armor_leather", "name": "皮甲", "rarity": "普通", "weight": 10.0,
     "value": 1000, "slot": "armor", "effect": "AC 11+敏捷", "desc": "轻便的皮甲。", "stack": False},
    {"item_id": "armor_chain", "name": "链甲", "rarity": "普通", "weight": 25.0,
     "value": 7500, "slot": "armor", "effect": "AC 16", "desc": "沉重的锁子甲。", "stack": False},
    {"item_id": "shield_kite", "name": "鸢盾", "rarity": "普通", "weight": 6.0,
     "value": 1000, "slot": "shield", "effect": "AC +2", "desc": "制式鸢形盾。", "stack": False},
    # rings / amulets
    {"item_id": "ring_rune", "name": "符文戒指", "name_en": "Rune Ring", "rarity": "稀有",
     "weight": 0.1, "value": 40000, "slot": "ring", "effect": "豁免+1", "desc": "镌刻古老符文的戒指。", "stack": False},
    {"item_id": "ring_power", "name": "力量之戒", "rarity": "珍奇", "weight": 0.1,
     "value": 120000, "slot": "ring", "effect": "力量+2", "desc": "佩戴者力大无穷。", "stack": False},
    {"item_id": "amulet_ward", "name": "守护护符", "rarity": "稀有", "weight": 0.2,
     "value": 45000, "slot": "amulet", "effect": "AC +1", "desc": "抵御伤害的护身符。", "stack": False},
    # scrolls
    {"item_id": "scroll_fireball", "name": "火球术卷轴", "rarity": "非普通", "weight": 0.1,
     "value": 20000, "slot": "consumable", "effect": "施展火球术（8d6火焰）", "desc": "可一次性施展的卷轴。", "stack": True},
    {"item_id": "scroll_heal", "name": "治疗术卷轴", "rarity": "非普通", "weight": 0.1,
     "value": 15000, "slot": "consumable", "effect": "施展治疗术", "desc": "蕴含治疗之力的卷轴。", "stack": True},
    # misc / valuables
    {"item_id": "gem_ruby", "name": "红宝石", "rarity": "稀有", "weight": 0.1,
     "value": 50000, "slot": "misc", "effect": "高价值宝石", "desc": "璀璨的红宝石。", "stack": True},
    {"item_id": "gem_sapphire", "name": "蓝宝石", "rarity": "稀有", "weight": 0.1,
     "value": 40000, "slot": "misc", "effect": "高价值宝石", "desc": "深邃的蓝宝石。", "stack": True},
    {"item_id": "lockpick", "name": "撬锁工具", "rarity": "普通", "weight": 0.5,
     "value": 2500, "slot": "misc", "effect": "用于开锁检定", "desc": "一套精巧的撬锁工具。", "stack": True},
    {"item_id": "torch", "name": "火把", "rarity": "普通", "weight": 1.0,
     "value": 10, "slot": "misc", "effect": "照明", "desc": "一根火把。", "stack": True},
    {"item_id": "rations", "name": "口粮", "rarity": "普通", "weight": 1.0,
     "value": 50, "slot": "misc", "effect": "一天的补给", "desc": "干粮与水袋。", "stack": True},
    # quest items / uniques (Boss drops)
    {"item_id": "key_rusty", "name": "古锈钥匙", "rarity": "非普通", "weight": 0.1,
     "value": 0, "slot": "none", "effect": "开启某扇门", "desc": "锈迹斑斑的古钥匙。", "stack": False, "unique": True, "quest_item": True},
    {"item_id": "boss_relic_shadow", "name": "暗影之心", "rarity": "传说", "weight": 0.5,
     "value": 0, "slot": "none", "effect": "任务关键道具", "desc": "暗影领主体内凝结的黑色核心。", "stack": False, "unique": True, "quest_item": True},
]


# --------------------------------------------------------------------------- #
# Schema (CREATE IF NOT EXISTS; legacy + game_state tables untouched).
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS rpg_state(
  session_id TEXT PRIMARY KEY,
  player_json TEXT NOT NULL DEFAULT '{}',
  enemies_json TEXT NOT NULL DEFAULT '[]',
  quests_json TEXT NOT NULL DEFAULT '[]',
  npcs_json TEXT NOT NULL DEFAULT '[]',
  economy_json TEXT NOT NULL DEFAULT '{}',
  turn_number INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rpg_items(
  item_id TEXT PRIMARY KEY,
  name TEXT, name_en TEXT, rarity TEXT, weight REAL, value INTEGER,
  slot TEXT, effect TEXT, descr TEXT, stack INTEGER DEFAULT 0,
  uniq INTEGER DEFAULT 0, quest_item INTEGER DEFAULT 0, data_json TEXT
);
CREATE TABLE IF NOT EXISTS rpg_inventory(
  session_id TEXT NOT NULL,
  item_id TEXT NOT NULL,
  qty INTEGER DEFAULT 1,
  equipped INTEGER DEFAULT 0,
  notes TEXT,
  PRIMARY KEY (session_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_rpg_inv_session ON rpg_inventory(session_id);
CREATE TABLE IF NOT EXISTS rpg_rule_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_number INTEGER,
  category TEXT,
  entity TEXT,
  before_json TEXT,
  after_json TEXT,
  delta_json TEXT,
  detail TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rpg_rulelog_session ON rpg_rule_log(session_id, turn_number);
"""

RPG_GUARD = """[RPG RULE GUARD — 游戏规则由程序裁定 / RULES ARE PROGRAM-OWNED]
- 经验（XP）、金币（Gold）、等级（Level）、属性（Stats）、物品（Items）、装备（Equipment）、掉落（Loot）、任务进度（Quest Progress）一律以「RPG SNAPSHOT」中的数值为准。
- 你不得自行增减 XP、金币、等级或属性数值；不得凭空创造不在「AVAILABLE LOOT / INVENTORY」中的装备。
- 你只能描述战斗与冒险的过程与结果（叙事）；数字与物品的授予/扣除由规则引擎完成。
- 若叙事所需的数值与「RPG SNAPSHOT」不一致，以「RPG SNAPSHOT」为准并据此描述。"""


# --------------------------------------------------------------------------- #
# Default snapshots
# --------------------------------------------------------------------------- #
def default_player(name="未知英雄", race="人类", cls="战士"):
    tpl = CLASS_TEMPLATES.get(cls, CLASS_TEMPLATES["战士"])
    stats = dict(tpl["stats"])
    bonus = RACE_BONUS.get(race, {})
    for k, v in bonus.items():
        if k in stats:
            stats[k] = stats[k] + v
    return {
        "name": name, "race": race, "class": cls, "class_en": tpl.get("en", ""),
        "level": 1, "xp": 0, "xp_to_next": XP_TABLE[2],
        "hp": tpl["base_hp"], "max_hp": tpl["base_hp"], "ac": tpl["base_ac"],
        "stats": stats, "prime_stat": tpl["prime"],
        "skills": [], "equipment": {},        # {slot: item_id}
        "status": "正常", "conditions": [],   # e.g. ["中毒","潜行"]
        "spell_slots": dict(tpl["spell_slots"]),
        "abilities": list(tpl["abilities"]),
    }


def default_economy():
    return {"copper": 0}   # internal total copper


# --------------------------------------------------------------------------- #
# State containers
# --------------------------------------------------------------------------- #
class RPGSnapshot:
    """The full rule-state for one session (player / enemies / quests / npcs / economy)."""

    def __init__(self, session_id, player, enemies, quests, npcs, economy, turn):
        self.session_id = session_id
        self.player = player
        self.enemies = enemies
        self.quests = quests
        self.npcs = npcs
        self.economy = economy
        self.turn = turn


# --------------------------------------------------------------------------- #
# RuleEngine — the single authority on all game rules.
# --------------------------------------------------------------------------- #
class RuleEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with closing(self._conn()) as db:
            db.executescript(SCHEMA)
            self._seed_items(db)
            db.commit()

    def _seed_items(self, db) -> None:
        n = db.execute("SELECT COUNT(*) FROM rpg_items").fetchone()[0]
        if n > 0:
            return
        for it in SEED_ITEMS:
            db.execute(
                "INSERT INTO rpg_items(item_id, name, name_en, rarity, weight, value, "
                "slot, effect, descr, stack, uniq, quest_item, data_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["item_id"], it.get("name", it["item_id"]), it.get("name_en", ""),
                 it.get("rarity", "普通"), float(it.get("weight", 0)), int(it.get("value", 0)),
                 it.get("slot", "misc"), it.get("effect", ""), it.get("desc", ""),
                 1 if it.get("stack") else 0, 1 if it.get("unique") else 0,
                 1 if it.get("quest_item") else 0, json.dumps({}, ensure_ascii=False)))

    # ---- item registry -------------------------------------------------------
    def get_item(self, item_id: str) -> dict | None:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM rpg_items WHERE item_id=?", (item_id,)).fetchone()
            return dict(r) if r else None

    def all_items(self) -> list:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM rpg_items ORDER BY value").fetchall()]

    def find_item_by_name(self, name: str) -> dict | None:
        name = (name or "").strip()
        if not name:
            return None
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            r = db.execute("SELECT * FROM rpg_items WHERE name=? OR name_en=? COLLATE NOCASE",
                           (name, name)).fetchone()
            return dict(r) if r else None

    # ---- core load / save ----------------------------------------------------
    def load(self, session_id: str) -> RPGSnapshot | None:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM rpg_state WHERE session_id=?", (session_id,)).fetchone()
        if not row:
            return None
        return RPGSnapshot(
            session_id=session_id,
            player=json.loads(row["player_json"] or "{}"),
            enemies=json.loads(row["enemies_json"] or "[]"),
            quests=json.loads(row["quests_json"] or "[]"),
            npcs=json.loads(row["npcs_json"] or "[]"),
            economy=json.loads(row["economy_json"] or "{}"),
            turn=int(row["turn_number"] or 0),
        )

    def get_or_init(self, session_id: str) -> RPGSnapshot:
        st = self.load(session_id)
        return st if st else self.init_state(session_id)

    def init_state(self, session_id: str, name="未知英雄", race="人类", cls="战士") -> RPGSnapshot:
        snap = RPGSnapshot(
            session_id=session_id, player=default_player(name, race, cls),
            enemies=[], quests=[], npcs=[], economy=default_economy(), turn=0)
        self._save(snap)
        return snap

    def _save(self, snap: RPGSnapshot) -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO rpg_state(session_id, player_json, enemies_json, quests_json, "
                "npcs_json, economy_json, turn_number, updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET player_json=excluded.player_json, "
                "enemies_json=excluded.enemies_json, quests_json=excluded.quests_json, "
                "npcs_json=excluded.npcs_json, economy_json=excluded.economy_json, "
                "turn_number=excluded.turn_number, updated_at=excluded.updated_at",
                (snap.session_id, json.dumps(snap.player, ensure_ascii=False),
                 json.dumps(snap.enemies, ensure_ascii=False),
                 json.dumps(snap.quests, ensure_ascii=False),
                 json.dumps(snap.npcs, ensure_ascii=False),
                 json.dumps(snap.economy, ensure_ascii=False), snap.turn, _now_iso()))
            db.commit()

    # ---- rule log ------------------------------------------------------------
    def _log(self, session_id: str, turn: int, category: str, entity: str,
             before=None, after=None, delta=None, detail: str = "") -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO rpg_rule_log(session_id, turn_number, category, entity, "
                "before_json, after_json, delta_json, detail, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, turn, category, entity,
                 json.dumps(before, ensure_ascii=False) if before is not None else "",
                 json.dumps(after, ensure_ascii=False) if after is not None else "",
                 json.dumps(delta, ensure_ascii=False) if delta is not None else "",
                 (detail or "")[:2000], _now_iso()))
            db.commit()

    def get_rule_log(self, session_id: str) -> list:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT * FROM rpg_rule_log WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---- PLAYER --------------------------------------------------------------
    def create_character(self, session_id: str, name: str, race: str, cls: str,
                         turn: int = 0) -> list:
        """(Re)initialize the player from a class/race choice."""
        snap = self.get_or_init(session_id)
        before = dict(snap.player)
        cls = cls if cls in CLASS_TEMPLATES else "战士"
        snap.player = default_player(name or "未知英雄", race or "人类", cls)
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "character", snap.player["name"],
                  before=before, after=dict(snap.player),
                  delta={"name": name, "race": race, "class": cls},
                  detail="角色创建：%s（%s %s）" % (name, race, cls))
        return [self._change("character", snap.player["name"],
                             "→ %s/%s Lv.1" % (race, cls))]

    def grant_xp(self, session_id: str, amount: int, turn: int = 0,
                 reason: str = "") -> list:
        snap = self.get_or_init(session_id)
        if amount <= 0:
            return []
        changes = []
        before = {"xp": snap.player["xp"], "level": snap.player["level"]}
        snap.player["xp"] = int(snap.player["xp"]) + int(amount)
        snap.player["xp_to_next"] = xp_to_next(snap.player["level"])
        new_level = level_from_xp(snap.player["xp"])
        # level ups
        while snap.player["level"] < new_level:
            changes.extend(self._level_up_one(snap))
        snap.player["xp_to_next"] = xp_to_next(snap.player["level"])
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "xp", snap.player["name"], before=before,
                  after={"xp": snap.player["xp"], "level": snap.player["level"]},
                  delta={"xp": "+%d" % amount, "reason": reason or "战斗"},
                  detail="XP +%d（%s）→ 累计 %d / Lv.%d" %
                         (amount, reason or "战斗", snap.player["xp"], snap.player["level"]))
        changes.insert(0, self._change("xp", snap.player["name"],
                                       "+%d→%d" % (amount, snap.player["xp"])))
        return changes

    def _level_up_one(self, snap: RPGSnapshot) -> list:
        old = dict(snap.player)
        snap.player["level"] = int(snap.player["level"]) + 1
        cls = snap.player.get("class", "战士")
        gain = CLASS_TEMPLATES.get(cls, CLASS_TEMPLATES["战士"])["hit_die_avg"]
        snap.player["max_hp"] = int(snap.player["max_hp"]) + gain
        snap.player["hp"] = int(snap.player["hp"]) + gain        # full-ish heal on level
        # grant an ability point every even level (flavor)
        lv = snap.player["level"]
        if lv % 2 == 0 and "属性提升" not in snap.player["abilities"]:
            snap.player["abilities"].append("属性提升")
        snap.player["xp_to_next"] = xp_to_next(snap.player["level"])
        self._log(snap.session_id, snap.turn, "levelup", snap.player["name"],
                  before={"level": old["level"], "max_hp": old["max_hp"]},
                  after={"level": snap.player["level"], "max_hp": snap.player["max_hp"]},
                  delta={"max_hp": "+%d" % gain},
                  detail="升级！Lv.%d（max_hp %d→%d）" %
                         (snap.player["level"], old["max_hp"], snap.player["max_hp"]))
        return [self._change("levelup", snap.player["name"],
                             "Lv.%d (max_hp+%d)" % (snap.player["level"], gain))]

    def damage_player(self, session_id: str, amount: int, turn: int = 0,
                      reason: str = "") -> list:
        if amount <= 0:
            return []
        snap = self.get_or_init(session_id)
        before = snap.player["hp"]
        snap.player["hp"] = max(0, int(snap.player["hp"]) - int(amount))
        if snap.player["hp"] == 0 and "倒下" not in snap.player["conditions"]:
            snap.player["conditions"].append("倒下")
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "hp", snap.player["name"], before=before,
                  after=snap.player["hp"], delta={"hp": "-%d" % amount},
                  detail="HP -%d（%s）→ %d/%d" %
                         (amount, reason or "受伤", snap.player["hp"], snap.player["max_hp"]))
        return [self._change("hp", snap.player["name"],
                             "-%d→%d" % (amount, snap.player["hp"]))]

    def heal_player(self, session_id: str, amount: int, turn: int = 0,
                    reason: str = "") -> list:
        if amount <= 0:
            return []
        snap = self.get_or_init(session_id)
        before = snap.player["hp"]
        snap.player["hp"] = min(int(snap.player["max_hp"]), int(snap.player["hp"]) + int(amount))
        if "倒下" in snap.player["conditions"] and snap.player["hp"] > 0:
            snap.player["conditions"].remove("倒下")
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "hp", snap.player["name"], before=before,
                  after=snap.player["hp"], delta={"hp": "+%d" % amount},
                  detail="HP +%d（%s）→ %d/%d" %
                         (amount, reason or "治疗", snap.player["hp"], snap.player["max_hp"]))
        return [self._change("hp", snap.player["name"],
                             "+%d→%d" % (amount, snap.player["hp"]))]

    # ---- ECONOMY -------------------------------------------------------------
    def grant_gold(self, session_id: str, gold: int = 0, silver: int = 0,
                   copper: int = 0, turn: int = 0, reason: str = "") -> list:
        total = gold * COPPER_PER_GOLD + silver * COPPER_PER_SILVER + copper
        if total <= 0:
            return []
        snap = self.get_or_init(session_id)
        before = snap.economy.get("copper", 0)
        snap.economy["copper"] = int(before) + total
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "gold", snap.player["name"], before=before,
                  after=snap.economy["copper"], delta={"copper": "+%d" % total},
                  detail="金币 +%s（%s）→ 累计 %s" %
                         (coins_str(total), reason or "拾取", coins_str(snap.economy["copper"])))
        return [self._change("gold", snap.player["name"],
                             "+%s→%s" % (coins_str(total), coins_str(snap.economy["copper"])))]

    def spend(self, session_id: str, gold: int = 0, silver: int = 0,
              copper: int = 0, turn: int = 0, reason: str = "") -> tuple:
        """Return (ok, changes). Refuses if insufficient balance."""
        total = gold * COPPER_PER_GOLD + silver * COPPER_PER_SILVER + copper
        snap = self.get_or_init(session_id)
        if int(snap.economy.get("copper", 0)) < total:
            return False, [self._change("gold", snap.player["name"], "余额不足（拒绝）")]
        before = snap.economy["copper"]
        snap.economy["copper"] = before - total
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "gold", snap.player["name"], before=before,
                  after=snap.economy["copper"], delta={"copper": "-%d" % total},
                  detail="花费 %s（%s）→ 余额 %s" %
                         (coins_str(total), reason or "购买", coins_str(snap.economy["copper"])))
        return True, [self._change("gold", snap.player["name"],
                                   "-%s→%s" % (coins_str(total), coins_str(snap.economy["copper"])))]

    def buy(self, session_id: str, item_id: str, turn: int = 0) -> tuple:
        """Buy an item at its registry value. (ok, changes)."""
        item = self.get_item(item_id)
        if not item:
            return False, [self._change("buy", item_id, "物品不在注册表")]
        price = int(item["value"] or 0)
        ok, gold_changes = self.spend(session_id, copper=price, turn=turn, reason="购买%s" % item["name"])
        if not ok:
            return False, gold_changes
        inv_changes = self.add_item(session_id, item_id, qty=1, turn=turn, reason="购买")
        return True, gold_changes + inv_changes

    def sell(self, session_id: str, item_id: str, turn: int = 0) -> list:
        item = self.get_item(item_id)
        if not item:
            return [self._change("sell", item_id, "物品不在注册表")]
        inv = self.inventory(session_id)
        row = next((r for r in inv if r["item_id"] == item_id), None)
        if not row or int(row["qty"]) <= 0:
            return [self._change("sell", item_id, "背包无此物品")]
        proceeds = int(item["value"] or 0) // 2     # sell at half value
        self.remove_item(session_id, item_id, qty=1, turn=turn, reason="出售")
        gold_changes = self.grant_gold(session_id, copper=proceeds, turn=turn, reason="出售%s" % item["name"])
        return gold_changes

    # ---- INVENTORY -----------------------------------------------------------
    def add_item(self, session_id: str, item_id: str, qty: int = 1,
                 turn: int = 0, reason: str = "") -> list:
        item = self.get_item(item_id)
        if not item:
            return [self._change("inventory", item_id, "拒绝：未在注册表（防作弊）")]
        with closing(self._conn()) as db:
            row = db.execute(
                "SELECT qty FROM rpg_inventory WHERE session_id=? AND item_id=?",
                (session_id, item_id)).fetchone()
            before = row[0] if row else 0
            db.execute(
                "INSERT INTO rpg_inventory(session_id, item_id, qty, equipped, notes) "
                "VALUES(?,?,?,?,?) ON CONFLICT(session_id, item_id) DO UPDATE SET "
                "qty=excluded.qty",
                (session_id, item_id, before + qty, 0, ""))
            db.commit()
        self._log(session_id, turn, "inventory", item_id, before=before,
                  after=before + qty, delta={"qty": "+%d" % qty, "reason": reason or "获得"},
                  detail="物品 +%s×%d（%s）→ 持有 %d" %
                         (item["name"], qty, reason or "获得", before + qty))
        return [self._change("inventory", item["name"],
                             "+%d（持有%d）" % (qty, before + qty))]

    def remove_item(self, session_id: str, item_id: str, qty: int = 1,
                    turn: int = 0, reason: str = "") -> list:
        with closing(self._conn()) as db:
            row = db.execute(
                "SELECT qty FROM rpg_inventory WHERE session_id=? AND item_id=?",
                (session_id, item_id)).fetchone()
            if not row:
                return [self._change("inventory", item_id, "背包无此物品")]
            before = row[0]
            new = max(0, before - qty)
            if new == 0:
                db.execute("DELETE FROM rpg_inventory WHERE session_id=? AND item_id=?",
                           (session_id, item_id))
            else:
                db.execute("UPDATE rpg_inventory SET qty=? WHERE session_id=? AND item_id=?",
                           (new, session_id, item_id))
            db.commit()
        item = self.get_item(item_id) or {"name": item_id}
        self._log(session_id, turn, "inventory", item_id, before=before, after=new,
                  delta={"qty": "-%d" % qty, "reason": reason or "使用"},
                  detail="物品 -%s×%d（%s）→ 持有 %d" %
                         (item["name"], qty, reason or "使用", new))
        return [self._change("inventory", item["name"],
                             "-%d（持有%d）" % (qty, new))]

    def inventory(self, session_id: str) -> list:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT i.*, m.name, m.rarity, m.slot, m.value "
                "FROM rpg_inventory i JOIN rpg_items m ON i.item_id=m.item_id "
                "WHERE i.session_id=? ORDER BY m.value DESC", (session_id,)).fetchall()
            return [dict(r) for r in rows]

    def equip(self, session_id: str, item_id: str, turn: int = 0) -> list:
        item = self.get_item(item_id)
        if not item:
            return [self._change("equip", item_id, "物品不在注册表")]
        slot = item.get("slot", "misc")
        if slot in ("misc", "consumable", "none"):
            return [self._change("equip", item_id, "该物品不可装备")]
        snap = self.get_or_init(session_id)
        inv = self.inventory(session_id)
        if not any(r["item_id"] == item_id and int(r["qty"]) > 0 for r in inv):
            return [self._change("equip", item_id, "背包无此物品")]
        prev = snap.player["equipment"].get(slot)
        snap.player["equipment"][slot] = item_id
        # apply AC delta for armor/shield
        self._apply_equipment_ac(snap)
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "equip", item_id,
                  before={"slot": slot, "prev": prev},
                  after={"slot": slot, "item": item_id},
                  delta={"equipped": item["name"]},
                  detail="装备 %s → %s槽（AC=%d）" % (item["name"], slot, snap.player["ac"]))
        return [self._change("equip", item["name"], "→%s槽" % slot)]

    def unequip(self, session_id: str, slot: str, turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        prev = snap.player["equipment"].pop(slot, None)
        if not prev:
            return [self._change("unequip", slot, "该槽位无装备")]
        self._apply_equipment_ac(snap)
        snap.turn = turn
        self._save(snap)
        item = self.get_item(prev) or {"name": prev}
        self._log(session_id, turn, "unequip", prev,
                  before={"slot": slot, "item": prev}, after={"slot": slot},
                  delta={"unequipped": item["name"]},
                  detail="卸下 %s（%s槽，AC=%d）" % (item["name"], slot, snap.player["ac"]))
        return [self._change("unequip", item["name"], "←%s槽" % slot)]

    def _apply_equipment_ac(self, snap: RPGSnapshot) -> None:
        """Recompute AC: armor SETS a base (takes max), "+N" bonuses STACK."""
        cls = snap.player.get("class", "战士")
        base = CLASS_TEMPLATES.get(cls, CLASS_TEMPLATES["战士"])["base_ac"]
        eq = snap.player.get("equipment", {})
        armor = eq.get("armor")
        if armor:
            item = self.get_item(armor)
            if item:
                m = re.search(r"AC\s*(\d+)", item.get("effect", "") or "")
                if m:
                    base = max(base, int(m.group(1)))
        for slot, item_id in eq.items():
            item = self.get_item(item_id)
            if not item:
                continue
            for m in re.finditer(r"AC\s*\+(\d+)", item.get("effect", "") or ""):
                base += int(m.group(1))
        snap.player["ac"] = base

    # ---- ENEMIES -------------------------------------------------------------
    def spawn_enemy(self, session_id: str, enemy_id: str, name: str, hp: int,
                    xp_reward: int, loot_table: list = None, faction: str = "敌对",
                    hostile: bool = True, turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        existing = next((e for e in snap.enemies if e["enemy_id"] == enemy_id), None)
        if existing:
            return [self._change("enemy", enemy_id, "已存在（拒绝重复生成）")]
        snap.enemies.append({
            "enemy_id": enemy_id, "name": name, "hp": hp, "max_hp": hp,
            "alive": True, "hostile": hostile, "faction": faction,
            "xp_reward": int(xp_reward), "loot_table": loot_table or [],
            "flags": {},
        })
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "enemy_spawn", enemy_id, before=None,
                  after=snap.enemies[-1], delta={"name": name},
                  detail="敌人出现：%s（HP %d，XP %d）" % (name, hp, xp_reward))
        return [self._change("enemy_spawn", name, "HP%d/XP%d" % (hp, xp_reward))]

    def get_enemy(self, session_id: str, enemy_id: str) -> dict | None:
        snap = self.get_or_init(session_id)
        return next((e for e in snap.enemies if e["enemy_id"] == enemy_id), None)

    def find_enemy_by_name(self, session_id: str, name: str) -> dict | None:
        snap = self.get_or_init(session_id)
        name = (name or "").strip()
        for e in snap.enemies:
            if e.get("name") == name or name in (e.get("name") or ""):
                return e
        return None

    def kill_enemy(self, session_id: str, enemy_id: str, turn: int = 0) -> list:
        """PROGRAM decides enemy death consequences: mark dead + grant XP + roll loot."""
        snap = self.get_or_init(session_id)
        e = next((x for x in snap.enemies if x["enemy_id"] == enemy_id), None)
        if not e:
            return [self._change("enemy", enemy_id, "无此敌人")]
        if not e.get("alive", True):
            return [self._change("enemy", enemy_id, "已死亡（忽略）")]
        e["alive"] = False
        e["hp"] = 0
        e["flags"]["dead_at_turn"] = turn
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "enemy_death", enemy_id,
                  before={"alive": True, "hp": e["max_hp"]},
                  after={"alive": False, "hp": 0},
                  delta={"xp_reward": e["xp_reward"]},
                  detail="%s 被击败（XP 奖励 %d）" % (e["name"], e["xp_reward"]))
        changes = [self._change("enemy_death", e["name"], "死亡")]
        # XP comes from the ENEMY definition, never the LLM text.
        if e["xp_reward"]:
            changes.extend(self.grant_xp(session_id, e["xp_reward"], turn=turn,
                                         reason="击败%s" % e["name"]))
        # Loot comes from the ENEMY loot_table, rolled by the program.
        drops = self.roll_loot(e.get("loot_table") or [])
        for item_id, qty in drops:
            changes.extend(self.add_item(session_id, item_id, qty=qty, turn=turn,
                                         reason="击败%s掉落" % e["name"]))
        # gold drop if present in loot_table as a coin entry
        for entry in (e.get("loot_table") or []):
            if entry.get("kind") == "gold":
                changes.extend(self.grant_gold(session_id, gold=int(entry.get("amount", 0)),
                                               turn=turn, reason="击败%s" % e["name"]))
        return changes

    def damage_enemy(self, session_id: str, enemy_id: str, amount: int,
                     turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        e = next((x for x in snap.enemies if x["enemy_id"] == enemy_id), None)
        if not e or not e.get("alive", True):
            return [self._change("enemy", enemy_id, "无/已死")]
        before = e["hp"]
        e["hp"] = max(0, int(e["hp"]) - int(amount))
        snap.turn = turn
        changes = [self._change("enemy_hp", e["name"], "-%d→%d" % (amount, e["hp"]))]
        if e["hp"] == 0:
            self._save(snap)
            changes.extend(self.kill_enemy(session_id, enemy_id, turn=turn))
        else:
            self._save(snap)
        self._log(session_id, turn, "enemy_hp", enemy_id, before=before,
                  after=e["hp"], delta={"hp": "-%d" % amount},
                  detail="%s HP -%d → %d/%d" % (e["name"], amount, e["hp"], e["max_hp"]))
        return changes

    # ---- LOOT ENGINE ---------------------------------------------------------
    def roll_loot(self, loot_table: list) -> list:
        """Return [(item_id, qty)] from a loot table.
        Each entry: {item_id, chance (0..1), qty} or {kind:'gold', amount}.
        Uses random at runtime; tests pass chance=1.0 for determinism."""
        drops = []
        for entry in (loot_table or []):
            if entry.get("kind") == "gold":
                continue   # gold handled separately in kill_enemy
            chance = float(entry.get("chance", 1.0))
            if random.random() <= chance:
                drops.append((entry["item_id"], int(entry.get("qty", 1))))
        return drops

    # ---- QUESTS --------------------------------------------------------------
    def add_quest(self, session_id: str, quest_id: str, title: str,
                  objective: str, reward: dict = None, turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        if any(q["quest_id"] == quest_id for q in snap.quests):
            return [self._change("quest", quest_id, "已存在")]
        snap.quests.append({
            "quest_id": quest_id, "title": title, "objective": objective,
            "status": "available", "progress": 0, "flags": {},
            "reward": reward or {}, "accepted_at_turn": None,
            "completed_at_turn": None,
        })
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "quest_add", quest_id, after=snap.quests[-1],
                  delta={"title": title}, detail="任务出现：%s" % title)
        return [self._change("quest", title, "出现")]

    def accept_quest(self, session_id: str, quest_id: str, turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        q = next((x for x in snap.quests if x["quest_id"] == quest_id), None)
        if not q:
            return [self._change("quest", quest_id, "无此任务")]
        if q["status"] != "available":
            return [self._change("quest", quest_id, "状态=%s（不可接受）" % q["status"])]
        q["status"] = "active"
        q["accepted_at_turn"] = turn
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "quest_accept", quest_id,
                  before="available", after="active", detail="接受任务：%s" % q["title"])
        return [self._change("quest", q["title"], "接受")]

    def advance_quest(self, session_id: str, quest_id: str, step: int = 1,
                      turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        q = next((x for x in snap.quests if x["quest_id"] == quest_id), None)
        if not q or q["status"] != "active":
            return [self._change("quest", quest_id, "无/非进行中")]
        before = q["progress"]
        q["progress"] = before + step
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "quest_progress", quest_id,
                  before=before, after=q["progress"], detail="任务推进：%s +%d" % (q["title"], step))
        return [self._change("quest", q["title"], "进度 %d→%d" % (before, q["progress"]))]

    def complete_quest(self, session_id: str, quest_id: str, turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        q = next((x for x in snap.quests if x["quest_id"] == quest_id), None)
        if not q:
            return [self._change("quest", quest_id, "无此任务")]
        if q["status"] == "completed":
            return [self._change("quest", quest_id, "已完成")]
        q["status"] = "completed"
        q["completed_at_turn"] = turn
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "quest_complete", quest_id,
                  before="active", after="completed", detail="任务完成：%s" % q["title"])
        changes = [self._change("quest", q["title"], "完成")]
        # grant rewards (xp/gold/items) from quest definition
        rw = q.get("reward") or {}
        if rw.get("xp"):
            changes.extend(self.grant_xp(session_id, int(rw["xp"]), turn=turn, reason="任务奖励"))
        if rw.get("gold"):
            changes.extend(self.grant_gold(session_id, gold=int(rw["gold"]), turn=turn, reason="任务奖励"))
        for iid in (rw.get("items") or []):
            changes.extend(self.add_item(session_id, iid, turn=turn, reason="任务奖励"))
        return changes

    def active_quests(self, session_id: str) -> list:
        snap = self.get_or_init(session_id)
        return [q for q in snap.quests if q["status"] in ("active", "available")]

    # ---- NPC REGISTRY --------------------------------------------------------
    def register_npc(self, session_id: str, npc_id: str, name: str, role: str = "",
                     faction: str = "中立", location: str = "", turn: int = 0) -> list:
        snap = self.get_or_init(session_id)
        if any(n["npc_id"] == npc_id for n in snap.npcs):
            return [self._change("npc", npc_id, "已注册")]
        snap.npcs.append({
            "npc_id": npc_id, "name": name, "role": role, "faction": faction,
            "location": location, "status": "存活", "disposition": "中立",
            "first_met_turn": turn,
        })
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "npc_register", npc_id, after=snap.npcs[-1],
                  delta={"name": name, "location": location},
                  detail="NPC 注册：%s（@%s）" % (name, location or "?"))
        return [self._change("npc", name, "注册@%s" % (location or "?"))]

    def move_npc(self, session_id: str, npc_id: str, location: str,
                 turn: int = 0) -> list:
        """Enforce a SINGLE location per NPC (no duplication across places)."""
        snap = self.get_or_init(session_id)
        n = next((x for x in snap.npcs if x["npc_id"] == npc_id), None)
        if not n:
            return [self._change("npc", npc_id, "无此NPC")]
        before = n["location"]
        n["location"] = location
        snap.turn = turn
        self._save(snap)
        self._log(session_id, turn, "npc_move", npc_id, before=before, after=location,
                  detail="%s 移动 %s → %s" % (n["name"], before or "?", location))
        return [self._change("npc_move", n["name"], "%s→%s" % (before or "?", location))]

    def npcs_at(self, session_id: str, location: str) -> list:
        snap = self.get_or_init(session_id)
        return [n for n in snap.npcs if n.get("location") == location and n.get("status") == "存活"]

    # ---- INTAKE: narrative changes -> rule changes (the anti-cheat bridge) ----
    def intake(self, session_id: str, turn: int, narrative_changes: list,
               dm_reply: str = "") -> list:
        """Map StateUpdater's narrative changes into authoritative rule changes.

        The LLM may NARRATE numbers, but they are IGNORED here — XP/loot come
        from enemy/quest definitions owned by the program.
        """
        rule_changes = []
        for c in (narrative_changes or []):
            cat = c.get("category")
            ent = c.get("entity", "")
            change = c.get("change", "")
            if cat == "enemy" and "dead" in change:
                # enemy death -> program grants xp + loot (LLM's stated number ignored)
                e = self.find_enemy_by_name(session_id, ent)
                if e:
                    rule_changes.extend(self.kill_enemy(session_id, e["enemy_id"], turn=turn))
                else:
                    rule_changes.append(self._change("enemy", ent, "无匹配敌人（XP/掉落未触发）"))
            # player hp changes detected by StateUpdater are re-applied authoritatively
            elif cat == "player" and change.startswith("-") and ent == "hp":
                m = re.match(r"-(\d+)", change)
                if m:
                    rule_changes.extend(self.damage_player(session_id, int(m.group(1)), turn=turn,
                                                           reason="战斗"))
            elif cat == "player" and change.startswith("+") and ent == "hp":
                m = re.match(r"\+(\d+)", change)
                if m:
                    rule_changes.extend(self.heal_player(session_id, int(m.group(1)), turn=turn,
                                                         reason="恢复"))
        # detect explicit numeric claims in the reply that the program must OWN
        rule_changes.extend(self._extract_program_authoritative(session_id, turn, dm_reply))
        return rule_changes

    def _extract_program_authoritative(self, session_id: str, turn: int,
                                       dm_reply: str) -> list:
        """If the DM reply explicitly narrates item pickups of KNOWN registry
        items (by exact name), the program adds them — but ONLY registry items
        (anti-cheat: LLM cannot mint non-existent gear). Gold/XP numbers are
        NEVER taken from the text."""
        out = []
        text = dm_reply or ""
        for item in self.all_items():
            nm = item["name"]
            if nm and nm in text and re.search(
                    r"(拾起|捡起|拿起|收起|获得|入手|掉落|出现).{0,8}" + re.escape(nm), text):
                inv = self.inventory(session_id)
                if not any(r["item_id"] == item["item_id"] for r in inv):
                    out.extend(self.add_item(session_id, item["item_id"], turn=turn,
                                             reason="拾取（注册表确认）"))
        return out

    def validate_reply(self, session_id: str, dm_reply: str) -> list:
        """Flag LLM-claimed XP/gold/level numbers that disagree with the program.
        Records conflicts; does NOT auto-fix (the program value always wins)."""
        snap = self.get_or_init(session_id)
        text = dm_reply or ""
        conflicts = []
        # claimed XP grant
        for m in re.finditer(r"获得(?:了)?\s*(\d+)\s*(?:点)?\s*(?:经验|经验值|XP)", text):
            claimed = int(m.group(1))
            # compare to last xp delta from rule log
            if not self._matches_last_delta(session_id, "xp", claimed):
                conflicts.append({"category": "xp_mismatch", "claimed": claimed,
                                  "program": "见 rpg_rule_log",
                                  "detail": "LLM 声称 +%d 经验，但 XP 由规则引擎裁定（敌人/任务定义）" % claimed})
        # claimed level
        for m in re.finditer(r"(?:升到|达到|等级[:：]?)\s*(\d+)\s*级", text):
            claimed = int(m.group(1))
            if claimed != int(snap.player["level"]):
                conflicts.append({"category": "level_mismatch", "claimed": claimed,
                                  "program": snap.player["level"],
                                  "detail": "LLM 声称 Lv.%d，程序记录 Lv.%d" % (claimed, snap.player["level"])})
        # claimed gold
        for m in re.finditer(r"获得(?:了)?\s*(\d+)\s*(?:枚)?\s*金(?:币|钱)", text):
            claimed = int(m.group(1))
            if not self._matches_last_delta(session_id, "gold", claimed * COPPER_PER_GOLD):
                conflicts.append({"category": "gold_mismatch", "claimed": claimed,
                                  "program": "见 rpg_rule_log",
                                  "detail": "LLM 声称 +%d 金币，但金币由规则引擎裁定" % claimed})
        for c in conflicts:
            self._log(session_id, snap.turn, "rule_conflict", c["category"],
                      before=c.get("program"), after=c.get("claimed"),
                      detail=c["detail"])
        return conflicts

    def _matches_last_delta(self, session_id: str, category: str, value: int) -> bool:
        """True if the most recent rule log delta for `category` carries `value`."""
        with closing(self._conn()) as db:
            r = db.execute(
                "SELECT delta_json FROM rpg_rule_log WHERE session_id=? AND category=? "
                "ORDER BY id DESC LIMIT 1", (session_id, category)).fetchone()
        if not r or not r[0]:
            return False
        try:
            d = json.loads(r[0])
        except json.JSONDecodeError:
            return False
        raw = str(d.get("copper") or d.get("xp") or "")
        return str(value) in raw.replace("+", "")

    # ---- helpers -------------------------------------------------------------
    def _change(self, category: str, entity: str, change: str) -> dict:
        return {"category": category, "entity": entity, "change": change}

    # ---- RENDERING: RPG SNAPSHOT (Phase 10) ----------------------------------
    def render_rpg_snapshot(self, session_id: str) -> str:
        snap = self.get_or_init(session_id)
        p = snap.player
        econ = snap.economy
        lines = [
            "==================",
            "RPG SNAPSHOT（规则引擎裁定 / PROGRAM-OWNED）",
            "==================",
            "---- PLAYER STATE ----",
            "Name: %s" % p.get("name", "?"),
            "Race/Class: %s / %s" % (p.get("race", "?"), p.get("class", "?")),
            "Level: %d   XP: %d / %d" % (int(p.get("level", 1)), int(p.get("xp", 0)),
                                         int(p.get("xp_to_next", xp_to_next(int(p.get("level", 1)))))),
            "HP: %d / %d   AC: %d" % (int(p.get("hp", 0)), int(p.get("max_hp", 0)),
                                      int(p.get("ac", 0))),
            "Stats: " + ("; ".join("%s %d" % (k, v) for k, v in (p.get("stats") or {}).items()) or "（无）"),
            "Status: %s   Conditions: %s" % (p.get("status", "正常"),
                                             "、".join(p.get("conditions") or []) or "无"),
            "Abilities: " + ("、".join(p.get("abilities") or []) or "（无）"),
            "Spell Slots: " + ("; ".join("环%d×%d" % (int(lv), int(n))
                                         for lv, n in (p.get("spell_slots") or {}).items()) or "（无）"),
            "Gold: %s" % coins_str(int(econ.get("copper", 0))),
            "Equipment: " + ("; ".join("%s=%s" % (s, self.get_item(iid)["name"])
                                       for s, iid in (p.get("equipment") or {}).items()
                                       if self.get_item(iid)) or "（无）"),
        ]
        inv = self.inventory(session_id)
        lines.append("Inventory: " + ("; ".join("%s×%d%s" % (
            r["name"], int(r["qty"]), "✓装备" if int(r["equipped"]) else "")
            for r in inv) if inv else "（空）"))

        lines.append("---- ENEMIES ----")
        if snap.enemies:
            for e in snap.enemies:
                state = "存活" if e.get("alive") else "已死亡"
                lines.append("- %s[%s]: HP %d/%d, XP %d, 阵营=%s" % (
                    e.get("name"), e.get("enemy_id"), int(e.get("hp", 0)),
                    int(e.get("max_hp", 0)), int(e.get("xp_reward", 0)), e.get("faction")))
        else:
            lines.append("（无）")

        lines.append("---- ACTIVE QUESTS ----")
        aq = [q for q in snap.quests if q["status"] in ("active", "available")]
        if aq:
            for q in aq:
                lines.append("- [%s] %s: %s（进度 %d）" % (
                    q["status"], q["title"], q["objective"], q.get("progress", 0)))
        else:
            lines.append("（无）")

        lines.append("---- NPC ----")
        if snap.npcs:
            for n in snap.npcs:
                lines.append("- %s[%s] @%s（%s，%s）" % (
                    n.get("name"), n.get("npc_id"), n.get("location") or "?",
                    n.get("status"), n.get("disposition")))
        else:
            lines.append("（无）")

        lines.append("---- AVAILABLE LOOT (registry only) ----")
        reg = self.all_items()
        lines.append("（物品库共 %d 项；可掉落/出售的均来自此注册表，LLM 不得凭空创造）" % len(reg))
        lines.append("==================")
        return "\n".join(lines)

    def render_compact(self, session_id: str) -> str:
        snap = self.get_or_init(session_id)
        p = snap.player
        parts = ["Lv%d" % int(p.get("level", 1)),
                 "HP%d/%d" % (int(p.get("hp", 0)), int(p.get("max_hp", 0))),
                 "AC%d" % int(p.get("ac", 0)),
                 "XP%d" % int(p.get("xp", 0)),
                 "Gold=%s" % coins_str(int(snap.economy.get("copper", 0)))]
        ens = [e["name"] + ("(死)" if not e.get("alive") else "") for e in snap.enemies]
        if ens:
            parts.append("Enemy=" + ",".join(ens))
        inv = self.inventory(session_id)
        if inv:
            parts.append("Inv=" + ",".join("%s×%d" % (r["name"], int(r["qty"])) for r in inv))
        return " | ".join(parts)
