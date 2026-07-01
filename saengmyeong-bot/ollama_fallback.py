#!/usr/bin/env python3
"""
ollama_fallback.py — L1 fallback path for saengmyeong-bot.

When ST bridge POST fails (timeout / 503 / empty), saengmyeong-bot
calls V2 Ollama proxy DIRECTLY with a system prompt built from the
card PNG (description + scenario + system_prompt + lorebook).

This bypasses ST + ChatBridge + WS entirely, so the bot never goes
offline due to ST frontend firstLoadInit race / ChatBridge WS drops.

Speed: a typical 80-token reply should return in 4-15 seconds.
Reliability: depends only on the Ollama proxy (:11435) being up.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import time
from typing import Any

import aiohttp

log = logging.getLogger("saengmyeong")

# Translation table: Romanization / English -> Chinese
# Used to translate first_mes before assistant prefill so the model doesn't
# follow the card's original Romanization style (Leoredrum 决策: user-facing
# 全部中文).
_TRANSLATION_TABLE: list[tuple[str, str]] = [
    ("Saengmyeong Main Temple", "生命教总本山"),
    ("Sacred Recognition Act", "文化实践扩展法"),
    ("Saengmyeong-gyo", "生命教"),
    ("Im Dongwook", "林东旭"),
    ("Yoon Sohee", "尹素熙"),
    ("Seo Yuna", "徐润雅"),
    ("First Mother", "第一母神"),
    ("High Priestess", "大祭司"),
    ("Priestess", "女祭司"),
    ("Devotee", "信徒"),
    ("Initiate", "初信徒"),
    ("Ordained", "受戒者"),
    ("Gangnam", "江南"),
    ("Mapo-gu", "麻浦区"),
    ("Seoul", "首尔"),
]


def translate_to_chinese(text: str) -> str:
    """Replace Romanization / English names with Chinese. Order: longer first
    to avoid partial-match collisions (e.g. 'Priestess' before any 'Priest'
    if such existed)."""
    if not text:
        return text
    for src, dst in _TRANSLATION_TABLE:
        text = text.replace(src, dst)
    return text


# Reuse the card_parser + ollama_client in the same directory.
try:
    from card_parser import build_system_prompt_block
    from ollama_client import ollama_chat_completions
except ImportError:
    from .card_parser import build_system_prompt_block
    from .ollama_client import ollama_chat_completions


OLLAMA_PROXY_URL = os.environ.get("OLLAMA_PROXY_URL", "http://127.0.0.1:11435/v1")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "tavern-saengmyeong-user-api-key-change-me")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")


LANGUAGE_OVERRIDE_FALLBACK = """[LANGUAGE OVERRIDE — HIGHEST PRIORITY — SAENGMYEONG CULT-RP (DIRECT)]

你是「生命教」(Saengmyeong-gyo / 생명교)世界中的角色。当前角色卡已激活 57 条 Character Book entries。

## 🚨 绝对翻译规则(违反 = 失败)

**在你(模型)生成的每一句回复中,以下名字 / 制度 必须 使用 中文,绝不出现 罗马音 / 英文 / 韩文**:

| 罗马音 / 英文 | 中文翻译(必须用这个) |
|---|---|
| Saengmyeong-gyo / 생명교 | **生命教** |
| Sacred Recognition Act / 문화적 관행 확장법 | **文化实践扩展法** |
| Im Dongwook / 인동욱 | **林东旭** |
| Yoon Sohee / 윤소희 | **尹素熙** |
| Seo Yuna / 서윤아 | **徐润雅** |
| First Mother | **第一母神** |
| High Priestess | **大祭司** |
| Priestess | **女祭司** |
| Devotee | **信徒** |
| Initiate | **初信徒** |
| Ordained | **受戒者** |
| Seoul | **首尔** |
| Gangnam | **江南** |
| Mapo-gu | **麻浦区** |
| Saengmyeong Main Temple | **生命教总本山** |

**即使 first_mes / 卡片原文 / Character Book entry / lorebook 出现罗马音或英文,你(模型)生成的所有内容 必须 翻译为上表中文。绝对不能复制粘贴罗马音。**

## 输出语言

1. 所有叙事、对话、描述、内心独白必须简体中文(100%)。
2. **第二人称 perspective**(全程 "you" / "你"),不允许切到 third-person 或 first-person。
3. **地名中文**:Seoul → 首尔、Gangnam → 江南、Mapo-gu → 麻浦区、Saengmyeong Main Temple → 生命教总本山。
4. **直白描写,不 euphemize**(角色卡明文 "never euphemistic")。禁止"以下为 18+ 警告"等元评论。

## Scene Discipline(剧情不乱跳)

1. 锁定当前场景,不主动切换。
2. NPC 在场就保留在场。
3. 时间不跳跃。
4. 不替 {{user}} 做决定。
5. 场景到达合理暂停点要停。

## 真实性

响应基于上方的角色卡 lorebook + 场景设定。如果遇到超出 lorebook 内容的问题,标记 "[out-of-lore]" 但不编造新事实。
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

    `card_ctx` is the dict produced by `card_parser.parse_card_png`.
    """
    system_prompt = build_system_prompt_block(card_ctx, LANGUAGE_OVERRIDE_FALLBACK)

    # Build the messages with first_mes (opening) as assistant prefill,
    # then user request — same shape as what ST normally produces.
    # Translate first_mes Romanization -> Chinese BEFORE prefill so the model
    # doesn't follow the card's original Romanization style.
    messages: list[dict] = []
    if card_ctx.get("first_mes"):
        translated_first_mes = translate_to_chinese(card_ctx["first_mes"])
        messages.append({"role": "assistant", "content": translated_first_mes})
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
        "User-Agent": "saengmyeong-bot/1.0 (L1-fallback-direct-ollama)",
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


def sync_ollama_direct_reply(card_ctx: dict, user_text: str, **kwargs: Any) -> str:
    """Synchronous wrapper for callers that prefer not to await an inner
    async function (kept for future use; the bot.py path will use the
    async version directly)."""
    return asyncio.run(ollama_direct_reply(card_ctx, user_text, **kwargs))
