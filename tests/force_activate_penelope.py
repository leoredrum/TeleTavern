"""Force-activate Penelope3 character on every ST load.

ST has two relevant module-level vars:
- this_chid  : numeric character id, restored from in-memory state only
- active_character : avatar filename, used for settings persistence

On cold start this_chid is undefined and active_character is just a string
read from settings.json — ST never uses it to populate this_chid.

Fix: explicitly set both, then trigger saveSettings so settings.json
matches the runtime.
"""
import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await b.new_context()
        page = await ctx.new_page()
        await page.goto("http://127.0.0.1:8000/", wait_until="networkidle")
        await asyncio.sleep(30)

        result = await page.evaluate("""
            async () => {
                const out = { steps: [] };
                try {
                    const mod = await import('/script.js');
                    out.steps.push('imported');
                    const chars = mod.characters || [];
                    out.numChars = chars.length;
                    out.before = { this_chid: mod.this_chid, active_character: mod.active_character };

                    const target = chars.find(c => c.avatar === 'aicc-2026-06-15_Penelope3.png');
                    if (!target) {
                        out.error = 'Penelope3 not found';
                        return out;
                    }
                    const idx = chars.indexOf(target);

                    // Step 1: selectCharacterById sets this_chid + loads character
                    await mod.selectCharacterById(idx);
                    out.steps.push(`selectCharacterById(${idx})`);

                    // Step 2: setActiveCharacter sets the persisted `active_character` var
                    // signature: setActiveCharacter(entityOrKey) where key is the avatar
                    mod.setActiveCharacter(target.avatar);
                    out.steps.push(`setActiveCharacter('${target.avatar}')`);

                    // Step 3: force save
                    if (typeof mod.saveSettings === 'function') {
                        await mod.saveSettings();
                        out.steps.push('saveSettings() called');
                    }

                    await new Promise(r => setTimeout(r, 2000));

                    out.after = {
                        this_chid: mod.this_chid,
                        active_character: mod.active_character,
                        active_name: mod.characters[mod.this_chid]?.name,
                        active_avatar: mod.characters[mod.this_chid]?.avatar,
                    };
                    return out;
                } catch (e) {
                    out.error = e.message;
                    out.stack = e.stack?.slice(0, 400);
                    return out;
                }
            }
        """)

        print("=== force activate (full) ===")
        for k, v in result.items():
            if k == "stack":
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {v}")

        if result.get("error"):
            print(f"\nFAIL: {result['error']}")
            return 1

        await asyncio.sleep(3)
        s = json.loads(Path("/Users/leo/Documents/SillyTavern/SillyTavern/data/default-user/settings.json").read_text())
        print(f"\nsettings.json active_character: {s.get('active_character')}")

        await b.close()
        return 0 if result.get("after", {}).get("this_chid") is not None else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))