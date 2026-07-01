#!/usr/bin/env python3
"""
SAENGMYEONG ST init + ChatBridge activate (one-shot).

Bring up SillyTavern :8020 WebUI in headless Chromium, wait for ChatBridge
extension to fully initialize (its WS reaches "已连接" state), then OVERRIDE
the default settings (which may point at another local bot stack / wsPort)
by injecting per-user settings via the **ST frontend API** + `saveSettings()`.

This is the only way to make the override *survive* future debounced
saveSettings() flushes (direct JSON edits get silently stripped — ST frontend
SaveSettingsDebounced writes the known-schema extension_settings keys and
drops anything else).

Safe to re-run; it's idempotent.

Run:
    ../venv/bin/python init_chatbridge.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ST_URL = os.environ.get("ST_URL", "http://127.0.0.1:8020")
USER_DATA = Path.home() / ".st-runner-saengmyeong-profile"
EXPECTED_AVATAR = "Blessed-Are-The-Fruitful-aicharactercards.com_.png"
TARGET_WS_PORT = "8021"
SETTINGS_FILE = os.environ.get(
    "ST_SETTINGS_FILE",
    str(Path.cwd().parent.parent / "saengmyeong-data" / "default-user" / "settings.json"),
)


INJECT_JS = r"""
async () => {
    try {
        // Load via dynamic import so we get live module bindings.
        const extMod = await import('/scripts/extensions.js');
        const scriptMod = await import('/script.js');
        const KEY = 'SillyTavern-Extension-ChatBridge';
        if (!extMod.extension_settings[KEY]) extMod.extension_settings[KEY] = {};
        // Merge: defaults + per-user override (override wins, per V2.1-DM fix comment).
        const target = {
            wsHost: '127.0.0.1',
            wsPort: '""" + TARGET_WS_PORT + r"""',
            autoConnect: true,
            forwardStreaming: true,
            preferredCharacterAvatar: '""" + EXPECTED_AVATAR + r"""',
            directorAvatars: [],
        };
        extMod.extension_settings[KEY] = Object.assign({}, extMod.extension_settings[KEY] || {}, target);
        await scriptMod.saveSettings();
        return {
            saved: true,
            bridge: JSON.parse(JSON.stringify(extMod.extension_settings[KEY])),
            charactersLength: (scriptMod.characters || []).length,
        };
    } catch (e) {
        return { saved: false, error: String(e), stack: String(e.stack || '') };
    }
}
"""


async def main() -> int:
    USER_DATA.mkdir(parents=True, exist_ok=True)
    print(f"[init_chatbridge] navigating {ST_URL} in profile {USER_DATA}", flush=True)

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

        await page.goto(ST_URL, wait_until="domcontentloaded", timeout=60000)

        # Phase 1: wait for ChatBridge to come online (even if pointing at defaults).
        print("[init_chatbridge] phase 1: wait for ChatBridge WS = 已连接 (up to 90s)", flush=True)
        connected = False
        for i in range(180):
            try:
                txt = await page.evaluate(
                    "() => document.querySelector('#chatbridge_ws_status')?.textContent?.trim() || ''"
                )
            except Exception:
                txt = ""
            if txt == "已连接":
                connected = True
                print(f"[init_chatbridge] ChatBridge WS connected at t={i*0.5:.1f}s", flush=True)
                break
            await asyncio.sleep(0.5)
        if not connected:
            print("[init_chatbridge] FAILED: ChatBridge never reached 已连接 in 90s", flush=True)
            await ctx.close()
            return 1

        # Phase 2: inject SAENGMYEONG-specific settings via ST frontend + saveSettings().
        print("[init_chatbridge] phase 2: inject per-user settings + saveSettings()", flush=True)
        result = await page.evaluate(INJECT_JS)
        print(f"[init_chatbridge] inject result: {json.dumps(result, ensure_ascii=False)[:800]}", flush=True)

        if not result.get("saved"):
            print("[init_chatbridge] FAILED: inject failed", flush=True)
            await ctx.close()
            return 2

        # Phase 3: wait for ChatBridge WS to reconnect on port 8021 (port mismatch triggers
        # a teardown + new ws.onopen), then optionally auto-active Blessed.
        print("[init_chatbridge] phase 3: wait for ChatBridge WS reconnect on port=8021", flush=True)
        for i in range(40):
            try:
                txt = await page.evaluate(
                    "() => document.querySelector('#chatbridge_ws_status')?.textContent?.trim() || ''"
                )
            except Exception:
                txt = ""
            chars_len = await page.evaluate(
                "async () => { try { const m = await import('/script.js'); return (m.characters || []).length; } catch { return -1 } }"
            )
            print(f"[init_chatbridge] t={i*0.5:.1f}s ws={txt!r} chars={chars_len}", flush=True)
            if txt == "已连接" and chars_len > 0:
                break
            await asyncio.sleep(0.5)

        # Verify settings.json persisted
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                s = json.load(f)
            cb = s.get("extension_settings", {}).get("SillyTavern-Extension-ChatBridge", {})
            print(f"[init_chatbridge] settings.json persisted wsPort={cb.get('wsPort')} avatar={cb.get('preferredCharacterAvatar')}", flush=True)

        await ctx.close()
        print("[init_chatbridge] done", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
