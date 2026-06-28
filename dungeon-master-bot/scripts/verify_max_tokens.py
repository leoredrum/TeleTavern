#!/usr/bin/env python3
"""验证 max_tokens=4096 是否让 bridge 生成完整长回复（不戛断）。
POST DM bridge :8013 带 max_tokens=4096，要求长回复，打印：
  REPLY_LEN / finish_reason（stop=自然结尾，length=max_tokens 上限戛断）/ TAIL（尾部 80 字）。
无 token 硬编码：OPENAI_BASE_URL / OPENAI_API_KEY 从 .env 读，绝不打印。
"""
import os
import sys
import json
import asyncio
from pathlib import Path

import aiohttp

_DM_DIR = Path(__file__).resolve().parent.parent  # dungeon-master-bot/


def load_env():
    for env in (_DM_DIR / ".env", _DM_DIR.parent / ".env"):
        if env.exists():
            kv = {}
            for line in env.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip().strip('"').strip("'")
            return kv
    return {}


async def main():
    env = load_env()
    base = env.get("OPENAI_BASE_URL", "").rstrip("/")
    key = env.get("OPENAI_API_KEY", "")
    if not base or not key:
        print("ERROR: OPENAI_BASE_URL / OPENAI_API_KEY not found in .env")
        sys.exit(2)
    url = base + "/chat/completions"
    payload = {
        "model": env.get("OPENAI_MODEL", "dungeon-master"),
        "stream": False,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "你是中文奇幻 RPG 地下城主，回复必须用中文，描写详尽完整，不省略。"},
            {"role": "user", "content": "请详细描写一场 Boss 战（影魔 Shadow Demon）的完整结算，依次包含："
             "（1）战斗收尾与影魔死亡的详细描述；（2）完整战利品列表（名称/稀有度/数量）；"
             "（3）玩家状态栏（HP/经验/等级/金币/背包/装备）；（4）后续走廊的线索与悬念。"
             "至少 5000 字，必须写完整结尾，不要在句子中间停下。"},
        ],
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(url, headers={"Authorization": "Bearer %s" % key,
                                        "Content-Type": "application/json"},
                          json=payload) as r:
            txt = await r.text()
            try:
                obj = json.loads(txt)
                content = obj["choices"][0]["message"]["content"]
                finish = obj["choices"][0].get("finish_reason")
            except Exception as e:
                print("PARSE_FAIL %s RAW=%s" % (e, txt[:400]))
                return
            print("REPLY_LEN=%d finish_reason=%s" % (len(content), finish))
            print("TAIL=%s" % repr(content[-80:]))
            print("TRUNCATED_BY_MAXTOKENS=%s" % (finish == "length"))


if __name__ == "__main__":
    asyncio.run(main())
