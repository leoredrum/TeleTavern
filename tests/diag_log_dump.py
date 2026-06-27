"""Diagnostic: capture ST ChatBridge debug log to disk after a bridge request."""
import asyncio
import sys
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

BRIDGE_URL = "http://127.0.0.1:8003/v1"
BRIDGE_KEY = "tavern-v2-user-api-key-change-me"
LOG_PATH = "/tmp/chatbridge_debug.log"


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(15)

        # Read debug log via ST DOM
        pre_log = await page.evaluate("""
            () => $('#chatbridge_debug_log').val() || ''
        """)
        print(f"=== debug log pre-request (last 800 chars) ===\n{pre_log[-800:]}")

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

        post_log = await page.evaluate("""
            () => $('#chatbridge_debug_log').val() || ''
        """)
        # Find what changed
        added = post_log[len(pre_log):]
        print(f"\n=== debug log added during request ({len(added)} chars) ===\n{added}")

        print(f"\n=== bridge content head ===\n{bridge_content[:200]}")

        # ST chat
        chat_dump = await page.evaluate("""
            () => (window.chat || []).map((m, i) => ({
                i, is_user: m.is_user, name: m.name,
                mes_head: (m.mes || '').slice(0, 80),
            }))
        """)
        print("\n=== ST chat[] ===")
        for c in chat_dump:
            print(f"  [{c['i']}] user={c['is_user']} name={c['name']}: {c['mes_head']!r}")

        # Save debug log to file
        Path(LOG_PATH).write_text(post_log)
        print(f"\nFull debug log written to {LOG_PATH}")

        await b.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))