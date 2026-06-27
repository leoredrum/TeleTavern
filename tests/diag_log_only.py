"""Diagnostic: dump ST debug log via DOM after waiting."""
import asyncio
import sys
from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(15)
        log = await page.evaluate("() => $('#chatbridge_debug_log').val() || ''")
        print(f"=== ST debug log (last 1500 chars) ===\n{log[-1500:]}")
        await b.close()
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))