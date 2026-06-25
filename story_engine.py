"""Story Progress Engine — lightweight scene-beat tracker.

Detects RP stalling ("暧昧 only, no progress") and feeds the model a high-priority
hint so the next reply introduces a real scene beat (action / decision / new info
/ choice / location change / conflict / consequence / emotional shift).

Public surface:
    StoryState             — per-thread progress tracker
    detect_stall(reply)    — returns (no_progress, motifs_found, beats_found)
    update_state(state, user_msg, reply, scene_change_hint=None)
    make_progress_hint(state, user_msg) -> str
    update_state_and_hint(state, user_msg, reply) -> (state, hint_for_next_turn)

Storage: in-memory only for v1 (per-bot AppState.story_state[thread_id]).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# ---------------------------------------------------------------------------
# Scene-beat and stall-signal keywords
# ---------------------------------------------------------------------------

# Stall signals (暧昧-only micro-expressions). If the reply is dominated by these
# and contains no real beat, it counts as "no progress".
STALL_MOTIFS = [
    "脸红", "红着脸", "脸颊泛红", "耳根发烫", "耳尖发烫",
    "靠近", "凑近", "贴近", "挨近", "靠过来",
    "轻声", "低低", "低声说", "小声",
    "耳边", "耳畔", "耳语",
    "心跳", "心怦", "心脏漏跳", "心跳加速",
    "沉默", "静静", "无声",
    "眼神", "目光", "眼波", "眼眸",
    "呼吸", "气息", "呼吸一滞",
    "手指", "指尖", "手心",
    "嘴唇", "唇边", "薄唇", "唇角",
    "低语", "喃喃", "轻声细语",
]

# Scene beats — concrete narrative progress.
BEAT_PATTERNS: dict[str, list[str]] = {
    "ACTION":           ["（", "走向", "拿起", "放下", "推", "拉", "打开", "关", "转身", "抬", "握", "递"],
    "DECISION":         ["决定", "我答应", "我拒绝", "我同意", "我愿意", "我选择", "我要"],
    "LOCATION_CHANGE":  [
        "走出", "走进", "去到", "到达", "离开", "踏入", "进入", "回到",
        "换了个", "我们走", "换个地方",
        # scene-setting nouns (a reply mentioning one of these means the bot is
        # placing us in a new room/scene, which counts as a location change)
        "浴室", "厨房", "卧室", "客厅", "书房", "阳台", "院子",
        "酒馆", "咖啡馆", "咖啡厅", "餐厅", "食堂",
        "公园", "街道", "路上", "街道上", "巷子", "小镇", "城市",
        "便利店", "超市", "商店", "书店",
        "楼顶", "天台", "河边", "湖边", "海边", "山上", "山下",
    ],
    "NEW_INFORMATION":  ["原来", "其实", "真相", "秘密", "告诉你", "其实我", "我从来没", "实际上"],
    "CHOICE":           ["你想", "要不要", "还是", "还是说", "你想让我", "我该", "我们该", "?"],
    "CONFLICT":         ["不行", "不能", "拒绝", "反对", "我不要", "你不能", "别这样", "不可以", "我不想"],
    "CONSEQUENCE":      ["因为你", "所以你", "结果", "于是", "导致", "造成"],
    "EMOTIONAL_SHIFT":  ["突然想", "我对你", "我们的关系", "我对你感", "我觉得你", "开始重新", "态度", "不再"],
}

# User intent signals — if user uses these, the next reply MUST advance.
USER_ADVANCE_SIGNALS = [
    "继续", "然后呢", "接下来", "更进一步", "然后", "再然后",
    "换个地方", "换个话题", "去别处", "去镇上", "去城里", "去酒馆外",
    "你决定", "你来", "你选", "你来选", "随你", "你来定",
    "再试", "重新", "快点", "快点啊", "别铺垫", "别废话",
]

# Stall-warning thresholds — if stalled this many rounds in a row, escalate hint.
STALL_WARN_AT = 1
STALL_FORCE_AT = 2
STALL_HEAVY_AT = 3


# ---------------------------------------------------------------------------
# StoryState
# ---------------------------------------------------------------------------

@dataclass
class StoryState:
    current_scene: str = ""                # short description of current scene
    current_goal: str = ""                 # what we're trying to advance
    relationship_stage: str = "陌生"        # 陌生 / 初识 / 熟识 / 暧昧 / 亲密
    tension_level: int = 0                 # 0..5
    last_progress_event: str = ""          # human-readable last beat
    repeated_motifs: list[str] = field(default_factory=list)   # stall motifs seen recently
    stalled_rounds: int = 0                # consecutive no_progress rounds
    user_intent: str = ""                  # last detected user intent
    next_progress_hint: str = ""           # hint to inject on next turn

    # Internal (not in spec but useful)
    beat_history: list[str] = field(default_factory=list)      # last 5 beat types
    last_replies_tail: list[str] = field(default_factory=list) # last 3 reply tails for motif dedup
    total_turns: int = 0


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _find_motifs(text: str) -> list[str]:
    found: list[str] = []
    for m in STALL_MOTIFS:
        if m in text:
            found.append(m)
    return found


def _find_beats(text: str) -> list[str]:
    beats: list[str] = []
    for beat_name, kws in BEAT_PATTERNS.items():
        if any(kw in text for kw in kws):
            beats.append(beat_name)
    return beats


def detect_stall(reply: str) -> tuple[bool, list[str], list[str]]:
    """Return (no_progress, motifs_found, beats_found).

    no_progress is True when:
      - reply is short (<40 zh chars) and motif-heavy (>=3 motifs), OR
      - reply has motifs and zero beats.
    """
    motifs = _find_motifs(reply)
    beats = _find_beats(reply)
    # Count Chinese characters (rough length proxy)
    cjk = sum(1 for c in reply if "\u4e00" <= c <= "\u9fff")
    if not motifs:
        return False, motifs, beats
    if not beats:
        return True, motifs, beats
    # Motifs present but beats also present — borderline; only fail if reply is very
    # short AND motif-heavy (suggests motif is the *whole* reply).
    if cjk < 60 and len(motifs) >= 3 and len(beats) <= 1:
        return True, motifs, beats
    return False, motifs, beats


def detect_user_intent(user_msg: str) -> str:
    """Detect explicit user advance signals. Returns short tag or ''."""
    msg = user_msg.strip()
    for sig in USER_ADVANCE_SIGNALS:
        if sig in msg:
            return f"advance:{sig}"
    if "?" in msg or "？" in msg:
        return "question"
    if len(msg) < 8:
        return "short"
    return ""


def detect_location_change(reply: str) -> bool:
    return any(kw in reply for kw in BEAT_PATTERNS["LOCATION_CHANGE"])


# ---------------------------------------------------------------------------
# Hint generation
# ---------------------------------------------------------------------------

HINT_TEMPLATE_BASE = (
    "[STORY PROGRESS — 必须在本次回复中推进剧情]\n"
    "{context}\n"
    "强制要求：\n"
    "  1. 至少包含一个 Scene Beat（具体动作 / 决定 / 新信息 / 选择 / 场景切换 / 小冲突 / 后果 / 情绪转折）。\n"
    "  2. 中文 60–180 字，简洁、具体、有进展感。\n"
    "  3. 禁止只用 {banned} 等暧昧铺垫撑回复。\n"
)


def _banned_examples(motifs: list[str]) -> str:
    # Pick up to 3 most-common motifs seen recently to ban explicitly.
    if not motifs:
        return "脸红/靠近/轻声/耳边/心跳"
    # Count occurrences in the last few replies — motifs is already a list of hits,
    # so we just keep the top 3 by listing order with priority to earlier ones.
    seen: list[str] = []
    for m in motifs:
        if m not in seen and len(seen) < 4:
            seen.append(m)
    return "/".join(seen) if seen else "暧昧铺拆"


def make_progress_hint(state: StoryState, user_msg: str) -> str:
    """Build the high-priority hint to inject before PHI.

    Returns a short Chinese instruction block. Empty string only if state is fresh
    AND user_msg has no advance signal AND stalled_rounds == 0.
    """
    user_intent = detect_user_intent(user_msg)
    state.user_intent = user_intent

    parts: list[str] = []

    # Stalled too long — strong push
    if state.stalled_rounds >= STALL_HEAVY_AT:
        parts.append(
            f"剧情已经连续 {state.stalled_rounds} 轮没有推进（最近只重复 {','.join(state.repeated_motifs[:3] or ['暧昧铺拆'])}）。"
            f"本次回复必须：换场景或换互动方式，给用户一个新的明确选择或事件，不能再继续当前循环。"
        )
    elif state.stalled_rounds >= STALL_FORCE_AT:
        parts.append(
            f"剧情已经连续 {state.stalled_rounds} 轮原地打转。"
            f"本次回复必须引入明确的新动作、新地点、新决定或新选择中的一个，"
            f"不要继续只写 {','.join(state.repeated_motifs[:3] or ['暧昧铺拆'])} 等铺垫。"
        )
    elif state.stalled_rounds >= STALL_WARN_AT:
        parts.append(
            f"上一轮没有实质推进（仅 {_banned_examples(state.repeated_motifs)}）。"
            f"本次回复请加入至少一个具体动作、决定、信息或场景变化。"
        )

    # User asked to advance
    if user_intent.startswith("advance:"):
        sig = user_intent.split(":", 1)[1]
        if sig in ("继续", "然后呢", "接下来", "更进一步", "然后", "再然后"):
            parts.append(
                "用户正在要求推进剧情。本次回复必须实际推进当前场景，"
                "让角色做出明确行动或决定，给出新的互动点，不要继续铺拆。"
            )
        elif sig in ("换个地方", "换个话题", "去镇上", "去城里", "去酒馆外"):
            parts.append(
                "用户要求换场景。本次回复必须把场景切换到一个新地点，"
                "并在新地点开始一段新互动（不是同一个房间里的微表情循环）。"
            )
        elif sig in ("你决定", "你来", "你选", "你来选", "随你", "你来定"):
            parts.append(
                "用户让角色做主。本次回复让角色主动做决定、提出新计划或新选择，"
                "而不是等用户行动。"
            )

    if not parts:
        # No stall, no advance signal — return a gentle always-on beat reminder
        # so the model never goes more than one turn without a beat.
        return (
            "[STORY PROGRESS — 提醒]\n"
            "本次回复请至少包含一个具体的 Scene Beat（动作 / 决定 / 新信息 / 选择 / 场景切换 / 小冲突 / 后果 / 情绪转折）。\n"
            "60–180 字中文，简洁有进展感。"
        )

    body = " ".join(parts)
    return HINT_TEMPLATE_BASE.format(
        context=body,
        banned=_banned_examples(state.repeated_motifs),
    )


# ---------------------------------------------------------------------------
# State update
# ---------------------------------------------------------------------------

def update_state_and_hint(
    state: StoryState,
    user_msg: str,
    reply: str,
    *,
    rewrite_used: bool = False,
) -> tuple[StoryState, str]:
    """Update the StoryState with the just-generated reply, return (state, hint_for_next_turn)."""
    state.total_turns += 1

    motifs = _find_motifs(reply)
    beats = _find_beats(reply)
    no_progress, _, _ = detect_stall(reply)

    if no_progress:
        state.stalled_rounds += 1
    else:
        state.stalled_rounds = 0

    # Track repeated motifs — rolling window of the last 3 replies.
    state.last_replies_tail.append(reply[-300:] if len(reply) > 300 else reply)
    if len(state.last_replies_tail) > 3:
        state.last_replies_tail.pop(0)

    # Merge motifs from this reply into the repeated list.
    for m in motifs:
        if m not in state.repeated_motifs:
            state.repeated_motifs.append(m)
    # Cap and decay: drop motifs not seen in last 3 replies.
    recent_motif_blob = "".join(state.last_replies_tail)
    state.repeated_motifs = [m for m in state.repeated_motifs if m in recent_motif_blob]
    if len(state.repeated_motifs) > 6:
        state.repeated_motifs = state.repeated_motifs[-6:]

    # Beat history
    if beats:
        state.beat_history.extend(beats)
        state.last_progress_event = f"{beats[0]} @ turn {state.total_turns}"
    if len(state.beat_history) > 8:
        state.beat_history = state.beat_history[-8:]

    # Location change → reset tension baseline
    if detect_location_change(reply):
        state.tension_level = max(0, state.tension_level - 1)

    # Advance signals from user
    intent = detect_user_intent(user_msg)
    state.user_intent = intent

    # Update progress hint for next turn
    state.next_progress_hint = make_progress_hint(state, "")

    return state, state.next_progress_hint


# ---------------------------------------------------------------------------
# Tiny debug helper
# ---------------------------------------------------------------------------

def debug_summary(state: StoryState) -> str:
    beats = "/".join(state.beat_history[-5:]) if state.beat_history else "-"
    motifs = "/".join(state.repeated_motifs[:6]) if state.repeated_motifs else "-"
    return (
        f"scene:           {state.current_scene or '(unset)'}\n"
        f"goal:            {state.current_goal or '(unset)'}\n"
        f"relationship:    {state.relationship_stage}\n"
        f"tension:         {state.tension_level}/5\n"
        f"stalled_rounds:  {state.stalled_rounds}\n"
        f"last_progress:   {state.last_progress_event or '(none yet)'}\n"
        f"repeated_motifs: {motifs}\n"
        f"recent_beats:    {beats}\n"
        f"user_intent:     {state.user_intent or '-'}\n"
        f"next_hint:       {(state.next_progress_hint or '').splitlines()[0] if state.next_progress_hint else '(none)'}"
    )


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick sanity check — does detect_stall behave correctly?
    bad = "她脸颊微红，轻轻靠近你，在你耳边低声说：'我……'，眼神闪躲，手指颤抖。"
    good = "她把门关上，拉开椅子坐到你对面，认真地问：'你刚才那句话，是认真的吗？'"

    s = StoryState()
    for label, reply in [("bad", bad), ("good", good)]:
        no_p, motifs, beats = detect_stall(reply)
        print(f"{label}: no_progress={no_p} motifs={motifs} beats={beats}")


# ---------------------------------------------------------------------------
# StoryEngine wrapper — exposed to PromptPipeline
# ---------------------------------------------------------------------------

class StoryEngine:
    """Thin wrapper that exposes `hint_for(state, user_msg)` to the pipeline.

    Holds no per-thread state itself; the caller (AppState.story_state[thread_id])
    owns the StoryState instance.
    """

    def hint_for(self, state: StoryState, user_msg: str) -> str:
        return make_progress_hint(state, user_msg or "")

    def observe(self, state: StoryState, user_msg: str, reply: str) -> tuple[StoryState, str]:
        """Update state with the just-generated reply, return updated state + hint for next turn."""
        return update_state_and_hint(state, user_msg, reply)

    def is_stalled(self, reply: str) -> bool:
        no_progress, _, _ = detect_stall(reply)
        return no_progress

    def debug(self, state: StoryState) -> str:
        return debug_summary(state)