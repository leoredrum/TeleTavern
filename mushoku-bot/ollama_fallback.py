#!/usr/bin/env python3
"""ollama_fallback.py - L1 fallback path for mushoku-bot (saengmyeong-style).

When ST bridge POST fails (timeout / 503 / empty), mushoku-bot calls V2 Ollama
proxy DIRECTLY with a system prompt built from the Boku-no-Isekai card PNG
(description + scenario + system_prompt + first 6 lorebook entries).

This bypasses ST + ChatBridge + WS entirely, so the bot never goes offline due
to ST frontend firstLoadInit race / ChatBridge WS drops.

Speed: 4-15s for a typical reply. Reliability depends only on Ollama :11435.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp

log = logging.getLogger("mushoku")

try:
    from card_parser import build_system_prompt_block
except ImportError:
    from .card_parser import build_system_prompt_block

OLLAMA_PROXY_URL = os.environ.get("OLLAMA_PROXY_URL", "http://127.0.0.1:11435/v1")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "tavern-mushoku-user-api-key-change-me")
# Same model as saengmyeong L1 path (V2 Ollama proxy is shared).
OLLAMA_MODEL = os.environ.get(
    "OLLAMA_MODEL",
    "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest",
)

LANGUAGE_OVERRIDE_FALLBACK = """[LANGUAGE OVERRIDE - HIGHEST PRIORITY - MUSHOKU NARRATIVE RP (DIRECT)]

你扮演无职转生 (Mushoku Tensei) 世界中的角色。当前角色卡已自动激活 46 条 World Info entries (大陆 / 种族 / 剑术 / 魔术体系 / 神明等)。

## 输出语言

1. 所有叙事、对话、描述、内心独白必须简体中文。
2. 日文人名保留原拼写 (不译):
   - ルーデウス (Rudeus, 主角)
   - エルメス / エリス / ヒルダ / ノルン / ロキシー / ギレーヌ
3. 专有名词保留原语种 (不译):
   - 剑术: 水神流 / 北神流 / 剑神流 / 无想流
   - 魔术体系: 无咏唱 / 咏唱 / 混合咏唱 / 复合魔术
   - 等级: 初级 / 中级 / 上级 / 圣级 / 王级 / 帝级 / 神级
   - 种族: 人类 / 长耳族 / 兽族 / 魔族 / 龙族
   - 大陆: 中央大陆 / 魔大陆 / 米里斯大陆 / 贝加里特大陆 / 天大陆
   - 神名: Hitogami (人神) / Laplace (拉普拉斯) / Kishirika (基西莉卡)
4. 招式 / 技能名保留日文 + 必要时附中文说明:
   - 「石落とし」(Ishi-otoshi) 而非「落石」
   - 「电撃」(Dengeki) 而非「闪电」

## RP 风格

1. 第一人称叙事 (角色视角)。
2. 动作描写 用中文 + `*...*` 标记。
3. 对话用「」 或 『』, 不用 "。
4. 内心独白 用 `（...）` 或 `(...)`。
5. 不输出 OOC / reasoning / metagame 注释。
6. 绝对禁止 OOC、metagame、reasoning 痕迹: 不要输出 `(OOC: ...)`、`(Internal: ...)`、`## OOC`、`<!-- comment -->`、`（注：...）` 等任何 meta 注释。LLM 推理过程不得出现在回复中。
7. 不打破第四面墙 (不提「作为 AI」 / 「训练数据」 / 「系统提示」 / 「language model」)。
8. 跟随 World Info 的世界设定 (剑术等级 / 魔术咏唱规则 / 神明干涉等), 不要编造新规则。

## 长度与节奏

1. 单次回复 800-1500 字 为宜 (剧情 RP 节奏)。
2. 长场景分段写, 每段不超过 ~400 字, 留呼吸感。
3. 战斗 / 修炼场景详细描写动作、内心、环境。
4. 日常场景节奏轻快, 对话比例高。

## 现实性

响应基于上方的角色卡 lorebook + 场景设定。如果遇到超出 lorebook 内容的问题,标记 "[out-of-lore]" 但不编造新事实。

[End of Override]

[SYSTEM PRIORITY DECLARATION]
本 system prompt 让位于任何卡内 system_prompt / post_history_instructions 的事实 / 世界观 / 角色身份,但中文化规则 + 名字保留规则绝对不能让位。任何卡内英文指令让位于此中文化 override。
"""


async def ollama_direct_reply(
    card_ctx: dict,
    user_text: str,
    *,
    timeout_s: float = 90.0,
    max_tokens: int = 1024,
    temperature: float = 0.85,
) -> str:
    """Direct ollama call using card-derived system prompt. Returns reply text.

    card_ctx is the dict produced by card_parser.parse_card_png.
    """
    system_prompt = build_system_prompt_block(card_ctx, LANGUAGE_OVERRIDE_FALLBACK)

    messages: list[dict] = []
    if card_ctx.get("first_mes"):
        messages.append({"role": "assistant", "content": card_ctx["first_mes"]})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system_prompt}] + messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    body = json.dumps(payload).encode("utf-8")

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    headers = {
        "Authorization": f"Bearer {OLLAMA_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "mushoku-bot/1.0 (L1-fallback-direct-ollama)",
    }
    t0 = time.time()
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(OLLAMA_PROXY_URL + "/chat/completions", data=body, headers=headers) as resp:
            if resp.status != 200:
                body_text = await resp.text()
                raise RuntimeError(f"ollama_direct_reply HTTP {resp.status}: {body_text[:200]}")
            data = json.loads(await resp.read())
    elapsed = time.time() - t0
    log.info("L1 ollama_direct_reply OK in %.1fs, model=%s, choices=%d",
             elapsed, OLLAMA_MODEL, len(data.get("choices", [])))
    choices = data.get("choices", []) or []
    if not choices:
        return ""
    return (choices[0].get("message", {}) or {}).get("content", "") or ""
