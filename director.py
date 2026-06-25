"""RP Director Prompt — configurable rules, not hardcoded in business logic.

The Director layer tells the model HOW to roleplay, independent of the
character's personality.  This mirrors SillyTavern's Author's Note / Post-History
Instructions separation but applies to ALL turns.

Phase 1 scope
============
- Fully configurable via DirectorConfig dataclass
- Default rules cover: no looping, advance narrative, no purple prose,
  no meta-commentary, proactive role, length guidance, language priority
- build_director_item() returns a ready PromptItem
- DEBUG log every generated item
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field

from prompt_item import PromptItem, PromptPosition


# ---------------------------------------------------------------------------
# Default repeat-ban triggers (common purple-prose / loop patterns)
# ---------------------------------------------------------------------------

_DEFAULT_REPEAT_BAN = [
    "脸红",
    "轻咬嘴唇",
    "耳边的低语",
    "贴近",
    "呼吸一滞",
    "指尖颤抖",
    "心跳加速",
    "脸红耳赤",
    "声音发颤",
    "凑近",
    "眼神闪躲",
    "不自觉地",
    "情不自禁",
    "欲言又止",
    "低下头",
    "垂下眼帘",
    "微微一愣",
    "愣了愣",
    "轻笑",
    "嘴角上扬",
]


@dataclass
class DirectorConfig:
    """All Director rules in one place — edit these, not the code.

    This is the ONLY place where RP rules live.  No business-logic files
    should contain hardcoded rule strings.
    """

    # Absolute prohibitions
    no_option_listing: bool = True          # Don't offer choices — act
    no_meta_commentary: bool = True         # No "作为AI…" / "让我来扮演…"
    no_prompt_explanation: bool = True      # Don't explain what you're doing
    no_fourth_wall: bool = True             # Never mention being an AI model
    no_repeat_micro_expressions: bool = True  # Same micro-expression once per reply
    no_emotion_loops: bool = True           # No 3+ consecutive lines of same emotion
    no_user_repeat: bool = True             # Don't paraphrase what the user said

    # Narrative
    advance_narrative: bool = True          # Every reply must push the story forward
    proactive_role: bool = True             # Character must initiate, not just react
    avoid_purple_prose: bool = True         # Natural, interactive prose style

    # Length guidance (character count, Chinese; 0 = no guidance)
    reply_length_min: int = 80
    reply_length_max: int = 280

    # Repeat-ban phrases
    repeat_ban_phrases: list[str] = field(
        default_factory=lambda: _DEFAULT_REPEAT_BAN.copy()
    )

    # Should the Director block itself be enabled?
    enabled: bool = True

    # Priority override — if set, Director goes to this position instead
    # (higher priority = closer to the end = more weight)
    priority_override: int | None = None


def _build_repeat_block(phrases: list[str]) -> str:
    if not phrases:
        return ""
    lines = "\n".join(f"  - {p}" for p in phrases)
    return (
        "以下微表情/动作描写每个在同一回合内最多出现1次，全文中不得连续出现2次以上：\n"
        f"{lines}\n\n"
    )


def _build_absolute_prohibitions(cfg: DirectorConfig) -> str:
    rules = []
    if cfg.no_option_listing:
        rules.append(
            '1. *不要列出选项*（"你想\u2026还是\u2026？"、"他可以\u2026也可以\u2026\u2026"）。直接行动或说话。'
        )
    if cfg.no_meta_commentary:
        rules.append(
            '2. *不要做元评论*（不说"作为AI\u2026"、"让我来扮演\u2026"、"根据我的设定\u2026"）。'
        )
    if cfg.no_prompt_explanation:
        rules.append(
            '3. *不要解释自己在做什么*。不要写"我现在要推进剧情\u2026"这类旁白。'
        )
    if cfg.no_fourth_wall:
        rules.append("4. *不要跳出角色*。不提及自己是AI语言模型，不评论对话本身。")
    if cfg.no_repeat_micro_expressions:
        rules.append("5. *同一回复中，同一微表情/动作描写只出现1次。*")
    if cfg.no_emotion_loops:
        rules.append("6. *不要陷入情绪循环*。连续3句以上相同的情绪表达视为循环，务必打破。")
    if cfg.no_user_repeat:
        rules.append(
            '7. *不要复述用户说过的话*。不"你说\u2026"、"你问我\u2026"这类转述。'
        )
    if rules:
        return "## 绝对禁止\n" + "\n".join(rules) + "\n\n"
    return ""


def _build_narrative_rules(cfg: DirectorConfig) -> str:
    rules = []
    if cfg.advance_narrative:
        rules.append(
            "1. *必须推进剧情*：每条回复必须包含新的信息或转折。\n"
            "   ✅ 正确示例：\"他愣了一下，说……\"（有反应+新信息）\n"
            "   ❌ 错误示例：\"她看着他，不说话。\"（无推进，等用户行动）"
        )
    if cfg.proactive_role:
        rules.append(
            "2. *角色必须主动*：提问、反问、提出想法、做决定。不要只回应用户的动作——"
            "角色可以主动创造新场景、新情节，透露内心想法或秘密，推进关系深度。"
        )
    if cfg.avoid_purple_prose:
        rules.append(
            "3. *避免Purple Prose*：不要堆砌形容词和副词。不要每句话都加动作描写。"
            "纯对话、纯动作、纯心理独白都可以。保持自然口语感。"
        )
    if cfg.reply_length_min > 0 or cfg.reply_length_max > 0:
        lo = cfg.reply_length_min
        hi = cfg.reply_length_max
        rules.append(
            f"4. *回复长度*：*{lo}–{hi}个中文字符*（不含角色名和引号）。"
            f"轻松场景偏短（{lo//2}–{lo}字），情感高潮场景偏长（{hi//2}–{hi}字）。"
        )
    if rules:
        return "## 叙事节奏\n" + "\n".join(rules) + "\n\n"
    return ""


def build_director_prompt(char_label: str, user_label: str, cfg: DirectorConfig) -> str:
    """Build the Director Prompt text from a DirectorConfig."""
    prohibitions = _build_absolute_prohibitions(cfg)
    repeat_block = _build_repeat_block(cfg.repeat_ban_phrases)
    narrative = _build_narrative_rules(cfg)

    return textwrap.dedent(
        f"""\
        [DIRECTOR — MANDATORY RULES]
        你是一场沉浸式角色扮演的导演兼演员。角色是 {char_label}，玩家是 {user_label}。

        {prohibitions}{repeat_block}{narrative}\
        [END DIRECTOR]
        """
    ).strip()


def build_director_item(
    char_label: str,
    user_label: str,
    cfg: DirectorConfig,
    *,
    enabled: bool | None = None,
) -> PromptItem:
    """Build a Director PromptItem from a DirectorConfig.

    Args:
        char_label: Character name for the prompt
        user_label: User/persona name
        cfg: Director configuration
        enabled: Override cfg.enabled (useful for testing)
    """
    is_enabled = cfg.enabled if enabled is None else enabled
    content = build_director_prompt(char_label, user_label, cfg) if is_enabled else ""

    position = PromptPosition.DIRECTOR
    if cfg.priority_override is not None:
        # If priority_override is set, treat Director as absolute with that priority
        # (lower position value = earlier, but we can adjust via priority)
        position = PromptPosition.POST_HISTORY  # placeholder; override in pipeline

    return PromptItem(
        id="director",
        role="system",
        content=content,
        enabled=is_enabled,
        position=PromptPosition.DIRECTOR,
        priority=cfg.priority_override or 0,
        depth=0,
        source="director",
        metadata={"class": "DirectorConfig", "cfg_keys": list(cfg.__dict__.keys())},
    )
