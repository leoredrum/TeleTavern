"""
Smoke test for the V2 bridge.

Does not need a real Telegram bot token. Simulates a user_request by
POSTing directly to the bridge's /v1/chat/completions endpoint.

Usage:
    1. Start the bridge:      ../venv/bin/python ../bridge/st_bridge.py
    2. Start SillyTavern:     cd ~/Documents/SillyTavern/SillyTavern && npm start
    3. Open WebUI in browser:  http://localhost:8000
    4. Make sure ST extension "Chat Bridge" is connected (click 连接).
    5. Run this script:        ../venv/bin/python smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8003/v1")
BRIDGE_KEY = os.environ.get("USER_API_KEY", "tavern-v2-user-api-key-change-me")
TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "180"))


async def smoke_non_streaming() -> None:
    print("=== non-streaming smoke ===")
    payload = {
        "model": "st-bridge",
        "messages": [{"role": "user", "content": "用一句话自我介绍。"}],
        "stream": False,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            BRIDGE_URL.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {BRIDGE_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        ) as resp:
            text = await resp.text()
            print(f"HTTP {resp.status}")
            print(text[:2000])
            if resp.status != 200:
                sys.exit(1)


async def smoke_streaming() -> None:
    print("=== streaming smoke ===")
    payload = {
        "model": "st-bridge",
        "messages": [{"role": "user", "content": "给我讲个一句话的冷笑话。"}],
        "stream": True,
    }
    started = time.monotonic()
    chunks = 0
    chars = 0
    final = ""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            BRIDGE_URL.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {BRIDGE_KEY}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
        ) as resp:
            print(f"HTTP {resp.status} content-type={resp.headers.get('Content-Type')}")
            if resp.status != 200:
                body = await resp.text()
                print(body[:500])
                sys.exit(1)
            buffer = ""
            async for raw in resp.content.iter_any():
                buffer += raw.decode("utf-8", errors="replace")
                while "\n\n" in buffer:
                    line, buffer = buffer.split("\n\n", 1)
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        evt = json.loads(data)
                    except Exception:
                        continue
                    delta = ((evt.get("choices") or [{}])[0].get("delta") or {}).get("content")
                    if delta:
                        chunks += 1
                        chars += len(delta)
                        final += delta
    elapsed = time.monotonic() - started
    print(f"chunks={chunks} chars={chars} elapsed={elapsed:.1f}s")
    print("final:", final[:500])


async def smoke_models() -> None:
    print("=== models smoke ===")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            BRIDGE_URL.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {BRIDGE_KEY}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            print(f"HTTP {resp.status}")
            print(await resp.text())


async def main() -> None:
    await smoke_models()
    await smoke_non_streaming()
    await smoke_streaming()
    print("=== smoke OK ===")


if __name__ == "__main__":
    asyncio.run(main())