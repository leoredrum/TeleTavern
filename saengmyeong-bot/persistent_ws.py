#!/usr/bin/env python3
"""
SAENGMYEONG persistent WS holder.

Replaces dm_st_runner.py for SAENGMYEONG only — keeps the headless
Chromium profile alive indefinitely so ChatBridge WS stays connected,
but does NOT auto-restart on transient ST frontend errors (those
restart cycles were wiping character state).

If Chromium or ST crashes, this script logs and exits so the
orchestrator can decide what to do.
"""
from __future__ import annotations
import asyncio
import signal
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ST_URL = "http://127.0.0.1:8020"
USER_DATA = Path.home() / ".st-runner-saengmyeong-profile"


async def main() -> int:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        page.on("console", lambda m: sys.stdout.write(
            f"[browser:{m.type}] {m.text[:300]}\n"
        ) if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: sys.stdout.write(f"[pageerr] {e}\n"))

        print(f"[persistent_ws] navigating {ST_URL}", flush=True)
        try:
            await page.goto(ST_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"[persistent_ws] navigation error: {exc}", flush=True)
            await ctx.close()
            return 1

        print("[persistent_ws] page loaded. idle forever (no restart).", flush=True)

        stop = asyncio.Event()

        def _stop(_sig, _frm):
            print("[persistent_ws] signal, stopping", flush=True)
            stop.set()

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        while not stop.is_set():
            await asyncio.sleep(60)

        await ctx.close()
        print("[persistent_ws] done", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
