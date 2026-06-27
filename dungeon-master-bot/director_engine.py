#!/usr/bin/env python3
"""
director_engine.py — Director Engine (Phase 4): the narrative CONTROL layer.

Layered ABOVE Phase 2 (world facts: where/who-alive) and Phase 3 (game rules:
XP/gold/items), the Director OWNS the narrative control:
  - which Scene is active (one and only one),
  - the current Story Beat,
  - the Current Objective,
  - whether a Scene Transition (location change) is APPROVED.

The LLM is demoted to "writer of the current Scene". The Director controls the
whole story. After this phase:
  - the PROGRAM owns world facts (Phase 2) + game rules (Phase 3) + story
    control (Phase 4);
  - the LLM only DESCRIBES the current Scene it is told to write.

Integration with bot.py `_generate_and_reply`:
  1. sys_content injects DIRECTOR STATE (right after CURRENT WORLD STATE) +
     DIRECTOR_GUARD (last, highest priority).
  2. director().intake() runs AFTER rule().intake(): it consumes the narrative
     changes (from StateUpdater) + rule changes (from RuleEngine) to advance the
     Story Beat / Current Objective and to APPROVE or DENY Scene Transitions.
     A DENIED location change is REVERTED on the world state — turning Phase 2's
     PASSIVE location_jump detection into ACTIVE control (no LLM teleporting).

Tables ADDED (legacy + Phase 2/3 tables are KEPT, never dropped):
  director_state (session_id PK, arc, chapter, scene_id, scene_name,
                  scene_location, beat, objective, state, progress_json,
                  turn, updated_at)
  scene_timeline (id PK, session_id, turn, scene_id, scene_name, beat,
                  objective, transition, note, created_at)
"""
import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import closing

import game_state as G   # GameStateManager — to read world facts + persist location reverts


# --------------------------------------------------------------------------- #
# DIRECTOR GUARD — appended to the system prompt every turn (Stage 7).
# Placed LAST so it reads at the highest priority.
# --------------------------------------------------------------------------- #
DIRECTOR_GUARD = """[DIRECTOR GUARD — 导演控制 / DIRECTOR-OWNED]
- 永远不要跳过未完成的场景（Never skip unfinished scenes）。若 DIRECTOR STATE 的 Scene Exit Conditions 未全部满足，不得推进到下一个场景。
- 永远不要传送玩家（Never teleport）。Location 的改变必须由 Director 批准。若玩家在当前场景的目标未完成就想离开，必须让其留在原地。
- 永远不要引入与当前场景无关的 NPC 或敌人（Never introduce unrelated NPCs）。
- 永远不要在 Director 批准前结束一场战斗（Never end a battle without Director approval）。
- 永远不要让「Current Objective」未完成就推进到下一个目标（Never leave Current Objective unfinished）。
- 你的职责：只描写「DIRECTOR STATE」中指定的当前场景（Current Scene）。场景切换、剧情节拍（Beat）、目标（Objective）、地点变更（Transition）一律由 Director 控制，以「DIRECTOR STATE」为准。"""

DIRECTOR_HEADER_TOP = "==================\nDIRECTOR STATE（导演控制 / DIRECTOR-OWNED）"
DIRECTOR_HEADER_BOT = "=================="


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# StoryBeat — the narrative beat the Director tracks at all times (Stage 4).
# String constants (not Enum) so they JSON-serialize and render directly.
# --------------------------------------------------------------------------- #
class StoryBeat:
    INTRODUCTION = "Introduction"
    EXPLORATION = "Exploration"
    BATTLE = "Battle"
    LOOT = "Loot"
    DIALOGUE = "Dialogue"
    PUZZLE = "Puzzle"
    TRAVEL = "Travel"
    BOSS = "Boss"
    REWARD = "Reward"
    REST = "Rest"
    SHOPPING = "Shopping"
    QUEST = "Quest"
    ENDING = "Ending"

    ALL = (INTRODUCTION, EXPLORATION, BATTLE, LOOT, DIALOGUE, PUZZLE, TRAVEL,
           BOSS, REWARD, REST, SHOPPING, QUEST, ENDING)


# --------------------------------------------------------------------------- #
# Arc / Chapter — the story structure above Scenes (Stage 1).
# --------------------------------------------------------------------------- #
class Arc:
    def __init__(self, arc_id, name, summary=""):
        self.id = arc_id
        self.name = name
        self.summary = summary


class Chapter:
    def __init__(self, chapter_id, name, arc_id, summary=""):
        self.id = chapter_id
        self.name = name
        self.arc_id = arc_id
        self.summary = summary


# --------------------------------------------------------------------------- #
# Scene model (Stage 2): Entry / Goal / Conflict / Exit / State.
# One and only one Scene is active at any time.
# --------------------------------------------------------------------------- #
def make_scene(scene_id, name, location, entry, goal, conflict, exit_conditions,
               beat, objective, next_scene=None, exit_mode="all"):
    """exit_conditions: list of dicts.
       {"type":"enemies_dead"} | {"type":"loot_taken"} |
       {"type":"flag","key":..,"value":"true"} | {"type":"objective_done"}
    exit_mode: "all" (default, every cond must hold) | "any".
    """
    return {
        "id": scene_id, "name": name, "location": location,
        "entry": entry, "goal": goal, "conflict": conflict,
        "exit_conditions": exit_conditions or [], "exit_mode": exit_mode,
        "beat": beat, "objective": objective, "next": next_scene,
        "state": "active",
    }


# --------------------------------------------------------------------------- #
# Default Story Arc (Stage 9 timeline: Arrival -> Basement -> Boss Battle ->
# Search Room -> Return Tavern). The Boss Room Scene covers the boss fight +
# the post-battle search via Beat progression (Stage 10: after the boss dies the
# player still searches/loots the SAME scene; only an explicit leave transitions).
# --------------------------------------------------------------------------- #
DEFAULT_ARC = Arc("arc_shadow_dungeon", "序章：暗影地牢", "黑石领主的地下据点。")

DEFAULT_CHAPTERS = [
    Chapter("ch1", "第一章：地底探险", "arc_shadow_dungeon",
            "玩家深入暗影地牢，击败首领并回收失窃神器。"),
]

DEFAULT_SCENES = [
    make_scene(
        "scene_arrival", "抵达地牢入口", "地牢入口",
        entry="玩家站在阴森的地牢入口前。",
        goal="进入地牢，开启冒险。",
        conflict="入口被藤蔓与阴影笼罩，气息不祥。",
        exit_conditions=[{"type": "flag", "key": "entered_dungeon", "value": "true"}],
        beat=StoryBeat.INTRODUCTION, objective="进入地牢",
        next_scene="scene_basement"),
    make_scene(
        "scene_basement", "地下室", "地下室",
        entry="玩家走入潮湿阴暗的地下室。",
        goal="探索地下室，找到通往深处的路。",
        conflict="暗影生物在角落低语，岔路众多。",
        exit_conditions=[{"type": "flag", "key": "found_boss_room", "value": "true"}],
        beat=StoryBeat.EXPLORATION, objective="探索地下室，找到首领所在",
        next_scene="scene_boss"),
    make_scene(
        "scene_boss", "首领之间", "首领之间",
        entry="玩家推开沉重的石门，进入首领盘踞的房间。",
        goal="击败首领，搜索房间，回收战利品与神器。",
        conflict="首领察觉入侵者，发出震怒的咆哮。",
        # Boss fight + post-battle search are the SAME scene (Stage 10).
        # The room may only be left once the boss is dead, loot is taken,
        # and the room has been searched.
        exit_conditions=[
            {"type": "enemies_dead"},
            {"type": "loot_taken"},
            {"type": "flag", "key": "room_searched", "value": "true"},
        ],
        beat=StoryBeat.BOSS, objective="击败首领",
        next_scene="scene_return"),
    make_scene(
        "scene_hidden", "首领之间·密室", "首领之间",
        entry="搜索时，玩家在墙后发现一间隐藏的密室。",
        goal="搜查密室中的机关与隐藏宝物。",
        conflict="机关暗藏，触之即发。",
        exit_conditions=[{"type": "flag", "key": "hidden_searched", "value": "true"}],
        beat=StoryBeat.LOOT, objective="搜查密室（机关/隐藏宝物）",
        next_scene="scene_return"),
    make_scene(
        "scene_return", "返回灰石酒馆", "灰石酒馆",
        entry="玩家带着战利品离开地牢，返回酒馆。",
        goal="向委托人汇报，领取报酬。",
        conflict="酒馆的喧嚣与地牢的死寂形成对比。",
        exit_conditions=[],
        beat=StoryBeat.TRAVEL, objective="返回灰石酒馆，结束本章",
        next_scene=None),
]


class DirectorSnapshot:
    """The full director state for one session."""

    def __init__(self, session_id, arc, chapter, scene_id, scene_name,
                 scene_location, beat, objective, state, progress, turn):
        self.session_id = session_id
        self.arc = arc
        self.chapter = chapter
        self.scene_id = scene_id
        self.scene_name = scene_name
        self.scene_location = scene_location
        self.beat = beat
        self.objective = objective
        self.state = state          # active | completed | abandoned | ending
        self.progress = progress    # dict of free-form per-scene progress flags
        self.turn = turn


# --------------------------------------------------------------------------- #
# SceneManager — owns the Scene list, the active scene, exit evaluation, and
# transition approval (pure logic; reads world facts from `st` + flags via mgr).
# --------------------------------------------------------------------------- #
class SceneManager:
    def __init__(self, scenes=None):
        self.scenes = scenes or DEFAULT_SCENES
        self.by_id = {s["id"]: s for s in self.scenes}

    def first(self):
        return self.scenes[0]

    def get(self, scene_id):
        return self.by_id.get(scene_id)

    def next_of(self, scene):
        nid = scene.get("next")
        return self.by_id.get(nid) if nid else None

    # ---- exit-condition evaluation ---------------------------------------- #
    def exit_satisfied(self, st, scene, mgr):
        conds = scene.get("exit_conditions") or []
        if not conds:
            return True                 # no condition -> free to leave
        results = [self._cond_satisfied(st, scene, c, mgr) for c in conds]
        return all(results) if scene.get("exit_mode", "all") == "all" else any(results)

    def exit_checklist(self, st, scene, mgr):
        """Return [(label, bool), ...] for rendering."""
        conds = scene.get("exit_conditions") or []
        out = []
        for c in conds:
            out.append((self._cond_label(c), self._cond_satisfied(st, scene, c, mgr)))
        return out

    def _cond_label(self, cond):
        t = cond.get("type")
        if t == "enemies_dead":
            return "敌人全部死亡"
        if t == "loot_taken":
            return "战利品全部拾取"
        if t == "flag":
            return "条件：%s" % cond.get("key")
        if t == "objective_done":
            return "当前目标完成"
        return "条件"

    def _cond_satisfied(self, st, scene, cond, mgr):
        t = cond.get("type")
        if t == "enemies_dead":
            ens = st.persistent.enemies
            return (all(e.get("status") == "dead" for e in ens)) if ens else True
        if t == "loot_taken":
            loot = st.persistent.loot
            return (all(lo.get("taken") for lo in loot)) if loot else True
        if t == "flag":
            return mgr.get_flag(st.session_id, cond.get("key")) == cond.get("value", "true")
        if t == "objective_done":
            return self._objective_core_done(st, scene)
        return False

    def _objective_core_done(self, st, scene):
        """The scene's primary goal: boss scene -> all enemies dead."""
        ens = st.persistent.enemies
        if scene["id"] == "scene_boss":
            return (all(e.get("status") == "dead" for e in ens)) if ens else False
        return True

    # ---- transition approval (Stage 3) ------------------------------------ #
    def evaluate_transition(self, st, scene, mgr, dm_reply):
        """Decide whether a detected location change may take effect.

        Returns (approved: bool, reason: str). A transition is approved only if:
          (a) the scene's exit conditions are satisfied, AND
          (b) the player EXPLICITLY intends to leave (sticky scenes).
        Otherwise the location change is denied and will be reverted.
        """
        satisfied = self.exit_satisfied(st, scene, mgr)
        leaving = bool(re.search(
            r"离开|走出去|出去|离开这里|离开房间|离开地牢|返回|回去|走出地牢|踏上归途",
            dm_reply or ""))
        if not satisfied:
            return False, "scene_not_complete（场景目标/战利品未完成，禁止传送）"
        if not leaving:
            return False, "must_explicitly_leave（须玩家明确离开才切换场景）"
        return True, "exit_conditions_met"


# --------------------------------------------------------------------------- #
# StoryManager — owns the Arc/Chapter structure and Beat progression (Stage 4).
# --------------------------------------------------------------------------- #
class StoryManager:
    def __init__(self, arc=None, chapters=None):
        self.arc = arc or DEFAULT_ARC
        self.chapters = chapters or DEFAULT_CHAPTERS

    def progress_beat(self, snap, scene, narrative_changes, rule_changes, dm_reply):
        """Return the next StoryBeat for the current scene, or None to keep it.

        Beat progression is RULE-DRIVEN (never LLM-free): it reacts to program
        facts (enemy death via rule_changes, loot taken, dialogue cues).
        """
        rc = rule_changes or []
        nc = narrative_changes or []
        cur = snap.beat

        def has(cat, sub=""):
            for c in rc:
                if c.get("category") == cat and (not sub or sub in str(c.get("change", ""))):
                    return True
            for c in nc:
                if c.get("category") == cat and (not sub or sub in str(c.get("change", ""))):
                    return True
            return False

        # Boss/Battle -> Loot when the boss dies (program owns the kill).
        if has("enemy", "dead") and cur in (StoryBeat.BOSS, StoryBeat.BATTLE):
            return StoryBeat.LOOT
        # Loot -> Reward once loot is taken.
        if (has("loot") or has("rule_loot")) and cur == StoryBeat.LOOT:
            return StoryBeat.REWARD
        # Dialogue beat when an NPC speaks (quoted line).
        if re.search(r"[「『“\"].{2,}[」』”\"]", dm_reply or "") and cur == StoryBeat.EXPLORATION:
            return StoryBeat.DIALOGUE
        return None


# --------------------------------------------------------------------------- #
# DirectorEngine — DB + the single intake entry point + rendering.
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS director_state(
  session_id TEXT PRIMARY KEY,
  arc TEXT,
  chapter TEXT,
  scene_id TEXT,
  scene_name TEXT,
  scene_location TEXT,
  beat TEXT,
  objective TEXT,
  state TEXT,
  progress_json TEXT,
  turn INTEGER DEFAULT 0,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS scene_timeline(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn INTEGER,
  scene_id TEXT,
  scene_name TEXT,
  beat TEXT,
  objective TEXT,
  transition TEXT,
  note TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scene_timeline_session
  ON scene_timeline(session_id, turn);
"""


class DirectorEngine:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.mgr = G.GameStateManager(db_path)   # read world facts + persist reverts
        self.scenes = SceneManager()
        self.story = StoryManager()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def ensure_schema(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with closing(self._conn()) as db:
            db.executescript(SCHEMA)

    # ---- core load / save -------------------------------------------------- #
    def get_or_init(self, session_id: str) -> DirectorSnapshot:
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM director_state WHERE session_id=?",
                             (session_id,)).fetchone()
        if row:
            try:
                progress = json.loads(row["progress_json"] or "{}")
            except json.JSONDecodeError:
                progress = {}
            return DirectorSnapshot(
                session_id=session_id,
                arc=row["arc"], chapter=row["chapter"],
                scene_id=row["scene_id"], scene_name=row["scene_name"],
                scene_location=row["scene_location"], beat=row["beat"],
                objective=row["objective"], state=row["state"],
                progress=progress, turn=int(row["turn"] or 0))
        return self.init_state(session_id)

    def init_state(self, session_id: str) -> DirectorSnapshot:
        s = self.scenes.first()
        snap = DirectorSnapshot(
            session_id=session_id,
            arc=self.story.arc.name,
            chapter=self.story.chapters[0].name,
            scene_id=s["id"], scene_name=s["name"], scene_location=s["location"],
            beat=s["beat"], objective=s["objective"], state="active",
            progress={}, turn=0)
        self._save_state(session_id, snap)
        self._timeline_append(session_id, 0, snap, transition="scene_start",
                              note="冒险开始：%s" % s["name"])
        return snap

    def _save_state(self, session_id: str, snap: DirectorSnapshot) -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO director_state(session_id, arc, chapter, scene_id, "
                "scene_name, scene_location, beat, objective, state, progress_json, "
                "turn, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET arc=excluded.arc, "
                "chapter=excluded.chapter, scene_id=excluded.scene_id, "
                "scene_name=excluded.scene_name, scene_location=excluded.scene_location, "
                "beat=excluded.beat, objective=excluded.objective, state=excluded.state, "
                "progress_json=excluded.progress_json, turn=excluded.turn, "
                "updated_at=excluded.updated_at",
                (session_id, snap.arc, snap.chapter, snap.scene_id, snap.scene_name,
                 snap.scene_location, snap.beat, snap.objective, snap.state,
                 json.dumps(snap.progress, ensure_ascii=False), snap.turn, _now_iso()))
            db.commit()

    def _timeline_append(self, session_id: str, turn: int, snap: DirectorSnapshot,
                         transition: str = "", note: str = "") -> None:
        with closing(self._conn()) as db:
            db.execute(
                "INSERT INTO scene_timeline(session_id, turn, scene_id, scene_name, "
                "beat, objective, transition, note, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (session_id, turn, snap.scene_id, snap.scene_name, snap.beat,
                 snap.objective, transition, (note or "")[:1000], _now_iso()))
            db.commit()

    def get_timeline(self, session_id: str):
        with closing(self._conn()) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT turn, scene_id, scene_name, beat, objective, transition, note "
                "FROM scene_timeline WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
            return [dict(r) for r in rows]

    # ---- INTAKE: the control bridge (runs AFTER rule().intake) ------------ #
    def intake(self, session_id: str, turn: int, narrative_changes: list,
               rule_changes: list, dm_reply: str, st) -> list:
        """Consume narrative + rule changes to control the story.

        Returns a list of decision dicts (beat / objective / transition_*).
        Side effects: advances Beat + Objective, syncs them into the world state
        (so render_block reflects them), tracks scene progress, and APPROVES or
        REVERTS Scene Transitions (location changes).
        """
        decisions = []
        snap = self.get_or_init(session_id)
        scene = self.scenes.get(snap.scene_id) or self.scenes.first()

        # 1) track scene-progress flags from the reply (search / puzzle / hidden).
        self._track_progress(session_id, snap, dm_reply)

        # 2) BEAT progression (rule-driven).
        new_beat = self.story.progress_beat(snap, scene, narrative_changes,
                                            rule_changes, dm_reply)
        if new_beat and new_beat != snap.beat:
            decisions.append({"category": "beat", "entity": snap.scene_name,
                              "change": "%s→%s" % (snap.beat, new_beat)})
            snap.beat = new_beat
            self._timeline_append(session_id, turn, snap,
                                  transition="beat", note="节拍→%s" % new_beat)

        # 3) OBJECTIVE progression — follows the beat within a scene.
        new_obj = self._current_objective(snap, scene, st)
        if new_obj != snap.objective:
            decisions.append({"category": "objective", "entity": new_obj,
                              "change": "%s→%s" % (snap.objective, new_obj)})
            snap.objective = new_obj
        # sync director decisions into the world state so render_block shows them.
        st.persistent.data["current_scene"] = snap.scene_name
        st.persistent.data["current_objective"] = snap.objective
        st.persistent.data["current_quest"] = snap.chapter

        # 4) SCENE TRANSITION approval (Stage 3 core: active control of location).
        loc_change = next((c for c in (narrative_changes or [])
                           if c.get("category") == "location"), None)
        if loc_change:
            approved, reason = self.scenes.evaluate_transition(st, scene,
                                                               self.mgr, dm_reply)
            if approved:
                self._advance(session_id, turn, snap, scene, st, loc_change)
                decisions.append({"category": "transition_approved",
                                  "entity": loc_change.get("entity", "?"),
                                  "change": "%s→%s" % (snap.scene_name,
                                                       self.scenes.get(snap.scene_id)
                                                       and self.scenes.get(snap.scene_id)["name"]
                                                       or loc_change.get("entity", "?")),
                                  "reason": reason})
            else:
                # REVERT the unauthorized teleport — LLM may not move the player.
                st.persistent.data["location"] = snap.scene_location or scene["location"]
                self.mgr.save(st)
                decisions.append({"category": "transition_denied",
                                  "entity": loc_change.get("entity", "?"),
                                  "change": "revert→%s" % (snap.scene_location or scene["location"]),
                                  "reason": reason})

        snap.turn = turn
        self._save_state(session_id, snap)
        self.mgr.save(st)   # persist world-state syncs (current_scene/objective/reverts)
        return decisions

    def _track_progress(self, session_id: str, snap: DirectorSnapshot, dm_reply: str) -> None:
        """Detect search / puzzle / hidden-room cues and set scene flags."""
        text = dm_reply or ""
        if re.search(r"搜索|搜查|查看|检查|翻找|仔细|打量|环顾|搜身", text):
            self.mgr.set_flag(session_id, "room_searched", "true", snap.turn)
        if re.search(r"密室|隐藏门|暗门|暗格|隐藏的|墙后|机关", text):
            self.mgr.set_flag(session_id, "found_hidden", "true", snap.turn)

    def _current_objective(self, snap: DirectorSnapshot, scene, st) -> str:
        """Derive the Current Objective from the beat + world facts (Stage 5)."""
        ens = st.persistent.enemies
        loot = st.persistent.loot
        boss_alive = any(e.get("status") != "dead" for e in ens)
        loot_pending = any(not lo.get("taken") for lo in loot)

        if scene["id"] == "scene_boss":
            if snap.beat in (StoryBeat.BOSS, StoryBeat.BATTLE) and boss_alive:
                return "击败首领（Defeat the boss）"
            if loot_pending or snap.beat == StoryBeat.LOOT:
                return "搜索房间，拾取战利品（Search the room / Loot）"
            return "离开房间，返回酒馆（Leave the room）"
        if scene["id"] == "scene_return":
            return "返回灰石酒馆，结束本章（Return to the tavern）"
        return scene.get("objective") or snap.objective

    def _advance(self, session_id: str, turn: int, snap: DirectorSnapshot,
                 scene, st, loc_change) -> None:
        """Move to the next scene (transition approved)."""
        self._timeline_append(session_id, turn, snap,
                              transition="scene_complete", note="场景完成")
        nxt = self.scenes.next_of(scene)
        if nxt:
            snap.scene_id = nxt["id"]
            snap.scene_name = nxt["name"]
            snap.beat = nxt["beat"]
            snap.objective = nxt["objective"]
            # location follows the player's actual destination (already set by
            # StateUpdater and approved); keep scene_location in sync.
            snap.scene_location = st.persistent.data.get("location") or nxt["location"]
            snap.state = "active"
            self._timeline_append(session_id, turn, snap,
                                  transition="scene_start", note="进入：%s" % nxt["name"])
        else:
            snap.state = "ending"
            snap.beat = StoryBeat.ENDING
            snap.objective = "故事结束（The story concludes）"
            self._timeline_append(session_id, turn, snap,
                                  transition="story_end", note="故事收尾")
        # sync the new scene into the world state immediately.
        st.persistent.data["current_scene"] = snap.scene_name
        st.persistent.data["current_objective"] = snap.objective

    # ---- RENDERING: DIRECTOR STATE (Stage 8) ------------------------------- #
    def render_director_state(self, session_id: str) -> str:
        snap = self.get_or_init(session_id)
        scene = self.scenes.get(snap.scene_id) or self.scenes.first()
        st = self.mgr.get_or_init(session_id)
        checklist = self.scenes.exit_checklist(st, scene, self.mgr)
        if checklist:
            cond_str = "; ".join("%s %s" % (label, "✓" if ok else "✗")
                                 for label, ok in checklist)
        else:
            cond_str = "（无，可自由离开）"
        lines = [
            DIRECTOR_HEADER_TOP,
            "Current Arc: %s" % snap.arc,
            "Current Chapter: %s" % snap.chapter,
            "Current Scene: %s（state: %s）" % (snap.scene_name, snap.state),
            "Current Beat: %s" % snap.beat,
            "Current Objective: %s" % snap.objective,
            "Scene Exit Conditions: %s" % cond_str,
            "Next Expected Event: %s" % self._next_expected(snap, scene, st),
            DIRECTOR_HEADER_BOT,
        ]
        return "\n".join(lines)

    def _next_expected(self, snap: DirectorSnapshot, scene, st) -> str:
        ens = st.persistent.enemies
        loot = st.persistent.loot
        boss_alive = any(e.get("status") != "dead" for e in ens)
        loot_pending = any(not lo.get("taken") for lo in loot)
        searched = self.mgr.get_flag(st.session_id, "room_searched") == "true"
        if boss_alive:
            return "继续战斗，击败首领"
        if loot_pending:
            return "拾取剩余战利品"
        if scene["id"] == "scene_boss" and not searched:
            return "搜索房间（可能有机关/隐藏房间）"
        if scene["id"] in ("scene_arrival", "scene_basement"):
            return "探索并触发下一个场景"
        if snap.state == "ending":
            return "为故事收尾"
        return "离开场景，前往下一地点"
