"""Full trace: open ST, send one bridge request, dump chat[] before AND after."""
import asyncio
import json
import os
from pathlib import Path

import aiohttp
from playwright.async_api import async_playwright

BRIDGE_URL = "http://127.0.0.1:8003/v1"
BRIDGE_KEY = "tavern-v2-user-api-key-change-me"


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(15)  # let ST init + ChatBridge auto-activate

        # Dump state pre-request
        pre = await page.evaluate("""
            async () => {
                const mod = await import('/script.js');
                return {
                    this_chid: mod.this_chid,
                    chat_len: mod.chat?.length || 0,
                    chat_summary: (mod.chat || []).map((m, i) => ({
                        i, is_user: m.is_user, name: m.name,
                        mes_head: (m.mes || '').slice(0, 80),
                    })),
                    char_name: mod.characters[mod.this_chid]?.name,
                };
            }
        """)
        print("=== PRE-REQUEST STATE ===")
        print(f"this_chid: {pre['this_chid']}, char: {pre['char_name']}, chat_len: {pre['chat_len']}")
        for s in pre['chat_summary']:
            print(f"  [{s['i']}] user={s['is_user']} name={s['name']}: {s['mes_head']!r}")

        # Send a bridge request
        print("\n=== SENDING BRIDGE REQUEST ===")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                BRIDGE_URL.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {BRIDGE_KEY}", "Content-Type": "application/json"},
                json={"model": "st-bridge", "messages": [{"role": "user", "content": "你好"}], "stream": False},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.json()
                content = body["choices"][0]["message"]["content"]
                print(f"bridge returned ({len(content)} chars): {content[:200]!r}")

        await asyncio.sleep(2)

        # Dump state post-request
        post = await page.evaluate("""
            async () => {
                const mod = await import('/script.js');
                return {
                    this_chid: mod.this_chid,
                    chat_len: mod.chat?.length || 0,
                    chat_summary: (mod.chat || []).map((m, i) => ({
                        i, is_user: m.is_user, name: m.name,
                        mes_head: (m.mes || '').slice(0, 80),
                    })),
                };
            }
        """)
        print("\n=== POST-REQUEST STATE ===")
        print(f"this_chid: {post['this_chid']}, chat_len: {post['chat_len']}")
        for s in post['chat_summary']:
            print(f"  [{s['i']}] user={s['is_user']} name={s['name']}: {s['mes_head']!r}")

        # Compare
        print(f"\nbridge content head: {content[:120]!r}")
        await b.close()
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))