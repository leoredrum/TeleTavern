"""Activate Penelope3 by clicking on its character card image.

ST renders character cards in the character list panel as <img> elements with
src like /thumbnail?type=avatar&file=aicc-2026-06-15_Penelope3.png.

We don't need characters[] to be loaded; we just need the DOM and a click event.
ST's character click handler reads the data attributes and calls
selectCharacterById internally.
"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        # ST needs ~20-30s to fully load on first launch
        await asyncio.sleep(30)

        # Find Penelope3 image in character list panel
        # ST renders characters in #rm_characters_block or similar
        candidates = await page.evaluate("""
            () => {
                const out = [];
                const imgs = document.querySelectorAll('img[src*="Penelope"]');
                for (const img of imgs) {
                    out.push({
                        src: img.src,
                        alt: img.alt,
                        parent_id: img.parentElement?.id,
                        parent_class: img.parentElement?.className,
                    });
                }
                return out;
            }
        """)
        print(f"Penelope images: {len(candidates)}")
        for c in candidates[:5]:
            print(f"  {c}")

        # Click the first one
        loc = page.locator("img[src*='Penelope']").first
        if await loc.count() == 0:
            print("FAIL: no Penelope image found")
            return 1

        await loc.click()
        await asyncio.sleep(4)

        # Trigger settings save
        await page.evaluate("""
            () => {
                // Force saveSettingsDebounced
                if (typeof saveSettingsDebounced === 'function') {
                    saveSettingsDebounced();
                }
            }
        """)
        await asyncio.sleep(2)

        # Verify
        post = await page.evaluate("""
            () => {
                const out = {};
                const img = document.querySelector('#avatar_img_div img, .avatar img');
                if (img) out.avatar_src = img.src;
                return out;
            }
        """)
        print(f"after click: {post}")

        # Verify on disk
        import json
        s = json.loads(Path("/Users/leo/Documents/SillyTavern/SillyTavern/data/default-user/settings.json").read_text())
        print(f"settings.json active_character: {s.get('active_character')}")
        await b.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))