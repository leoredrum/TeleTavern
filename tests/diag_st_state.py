"""Diagnostic: after ChatBridge auto-activate, dump ST's actual character state."""
import asyncio
from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        page.on("console", lambda m: print(f"[{m.type}] {m.text[:300]}") if "ChatBridge" in m.text or "force-activate" in m.text or "active char" in m.text else None)
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(15)

        out = await page.evaluate("""
            async () => {
                const mod = await import('/script.js');
                const idx = mod.this_chid;
                const c = idx !== undefined ? mod.characters[idx] : null;
                return {
                    this_chid: idx,
                    active_character_mod_var: mod.active_character,
                    name2: mod.name2,
                    user_avatar: mod.user_avatar,
                    chat_len: mod.chat?.length || 0,
                    chat_first: mod.chat?.[0]?.mes?.slice(0, 200) || null,
                    chat_last: mod.chat?.[mod.chat.length-1]?.mes?.slice(0, 200) || null,
                    char_name: c?.name,
                    char_avatar: c?.avatar,
                    char_description_len: c?.description?.length || 0,
                    char_description_head: c?.description?.slice(0, 200) || null,
                    char_system_prompt: c?.data?.system_prompt?.slice(0, 200) || null,
                    char_personality: c?.personality?.slice(0, 200) || null,
                };
            }
        """)
        print("=== ST state after ChatBridge auto-activate ===")
        for k, v in out.items():
            print(f"  {k}: {v}")
        await b.close()
        return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))