#!/usr/bin/env python3
"""
SAENGMYEONG persistent browser + WS holder.

Combines init_chatbridge.py phase + persistent idle loop in ONE long-running process:
  1. Bring headless Chromium up against ST :8020.
  2. Wait until ChatBridge WS connects, force settings, attempt character activation.
  3. Idle forever (re-poll every 60s) so the browser stays alive and ChatBridge WS persists.

Replaces dm_st_runner.py auto-restart cycle for SAENGMYEONG (auto-restart was wiping
character state every 60s).
"""
from __future__ import annotations
import asyncio
import json
import signal
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ST_URL = "http://127.0.0.1:8020"
USER_DATA = Path.home() / ".st-runner-saengmyeong-profile"
TARGET_AVATAR = "Blessed-Are-The-Fruitful-aicharactercards.com_.png"


# Build the JS string up-front (no nested triple-quote hazards).
_INJECT_JS_TEMPLATE = """
async () => {
    try {
        const extMod = await import('/scripts/extensions.js');
        const scriptMod = await import('/script.js');
        const KEY = 'SillyTavern-Extension-ChatBridge';
        if (!extMod.extension_settings[KEY]) extMod.extension_settings[KEY] = {};
        extMod.extension_settings[KEY] = Object.assign({}, extMod.extension_settings[KEY] || {}, {
            wsHost: '127.0.0.1',
            wsPort: '8021',
            autoConnect: true,
            forwardStreaming: true,
            preferredCharacterAvatar: '__AVATAR__',
            directorAvatars: []
        });
        await scriptMod.saveSettings();
        await scriptMod.getCharacters();
        const idx = (scriptMod.characters || []).findIndex(c => c.avatar === '__AVATAR__');
        if (idx >= 0) {
            try { await scriptMod.selectCharacterById(idx); } catch (_) {}
        }
        return {
            saved: true,
            bridge: JSON.parse(JSON.stringify(extMod.extension_settings[KEY])),
            charactersLength: (scriptMod.characters || []).length,
            thisChid: scriptMod.this_chid
        };
    } catch (e) {
        return { saved: false, error: String(e) };
    }
}
"""
INJECT_JS = _INJECT_JS_TEMPLATE.replace("__AVATAR__", TARGET_AVATAR)


async def main() -> int:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    print(f"[persistent_ws] navigating {ST_URL} in profile {USER_DATA}", flush=True)

    stop = asyncio.Event()
    def _stop(_sig, _frm):
        print("[persistent_ws] signal, stopping", flush=True)
        stop.set()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA),
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()
        page.on("console", lambda m: print(f"[browser:{m.type}] {m.text[:300]}", flush=True)
                if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: print(f"[pageerr] {e}", flush=True))

        try:
            await page.goto(ST_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"[persistent_ws] navigation error: {exc}", flush=True)
            await ctx.close()
            return 1

        # Phase 1: wait for ChatBridge WS = 已连接 (up to 120s).
        print("[persistent_ws] phase 1: wait ChatBridge WS (up to 120s)", flush=True)
        ws_connected = False
        for i in range(240):
            try:
                txt = await page.evaluate(
                    "() => document.querySelector('#chatbridge_ws_status')?.textContent?.trim() || ''"
                )
            except Exception:
                txt = ""
            if txt == "已连接":
                ws_connected = True
                print(f"[persistent_ws] ChatBridge WS connected at t={i*0.5:.1f}s", flush=True)
                break
            await asyncio.sleep(0.5)

        if not ws_connected:
            print("[persistent_ws] WARN: ChatBridge not 已连接 in 120s, continuing anyway", flush=True)

        # Phase 2: force settings + tryActivate character (idempotent, survives ST race).
        print("[persistent_ws] phase 2: forcing settings + character activation", flush=True)
        for attempt in range(3):
            try:
                # page.evaluate accepts a string containing an arrow function expression.
                # Wrap INJECT_JS into a forward-invoked IIFE so it actually runs and returns the promise.
                js_to_eval = f"({INJECT_JS})()"
                result = await page.evaluate(js_to_eval)
                print(f"[persistent_ws] inject attempt {attempt+1}: {json.dumps(result, ensure_ascii=False)[:600]}", flush=True)
                if result.get("saved") and result.get("charactersLength", 0) > 0:
                    break
            except Exception as exc:
                print(f"[persistent_ws] inject attempt {attempt+1} error: {exc}", flush=True)
            await asyncio.sleep(5)

        print("[persistent_ws] phase 3: idle forever; refresh every 5min", flush=True)
        last_inject = asyncio.get_event_loop().time()
        while not stop.is_set():
            await asyncio.sleep(60)
            now = asyncio.get_event_loop().time()
            if now - last_inject > 300:
                try:
                    js_to_eval = f"({INJECT_JS})()"
                    await page.evaluate(js_to_eval)
                    last_inject = now
                    print(f"[persistent_ws] re-inject at t={now:.0f}", flush=True)
                except Exception as exc:
                    print(f"[persistent_ws] re-inject failed: {exc}", flush=True)

        await ctx.close()
        print("[persistent_ws] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
