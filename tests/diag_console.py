"""Capture all browser console messages during a bridge request."""
import asyncio
import sys

import aiohttp
from playwright.async_api import async_playwright

BRIDGE_URL = "http://127.0.0.1:8003/v1"
BRIDGE_KEY = "tavern-v2-user-api-key-change-me"


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()

        # Capture all console messages
        console_msgs = []
        page.on("console", lambda m: console_msgs.append((m.type, m.text)))

        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(15)

        # Send bridge request
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BRIDGE_URL.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {BRIDGE_KEY}", "Content-Type": "application/json"},
                json={"model": "st-bridge", "messages": [{"role": "user", "content": "你好"}], "stream": False},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.json()
                bridge_content = body["choices"][0]["message"]["content"]

        await asyncio.sleep(3)

        print(f"=== bridge content ({len(bridge_content)} chars) ===\n{bridge_content[:300]}")

        # Print only ChatBridge-related console messages
        print("\n=== ChatBridge console messages ===")
        for t, msg in console_msgs:
            if "ChatBridge" in msg or "force-act" in msg or "gen#" in msg or "MESSAGE_UPDATED" in msg or "user_request" in msg or "st_response" in msg:
                print(f"[{t}] {msg[:200]}")

        await b.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))