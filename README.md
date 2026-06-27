# SillyTavern Connector — V2 Telegram Tavern

End-to-end Telegram bot for SillyTavern (1.18.0) + Ollama + Qwen3.6-35B-A3B.

```
Telegram bot  ──HTTP/OpenAI──>  Bridge :8003  (this dir/bridge/st_bridge.py)
Bridge        ──WebSocket──>   ST extension (SillyTavern third-party)
ST extension  ──DOM API──>     SillyTavern WebUI :8000
SillyTavern   ──HTTP/native──> Ollama :11434/api/chat
```

## Layout

```
~/Documents/SillyTavern/connector/
├── bridge/
│   ├── st_bridge.py          # Python bridge (OpenAI HTTP + WebSocket server)
│   ├── .env.example          # → copy to .env (no secrets in repo)
│   └── .env                  # local secrets (gitignored)
├── telegram-bot/
│   ├── bot.py                # PTB 22.x bot (OpenAI-format consumer)
│   └── .env.example
├── tests/
│   ├── smoke.py              # HTTP-level smoke (no browser)
│   ├── headless_extension.py # simulates the ST extension over WebSocket
│   └── e2e_playwright.py     # full real-ST e2e (CT-001..CT-011)
├── scripts/
│   ├── start-bridge.sh       # foreground/background bridge launcher
│   ├── stop-bridge.sh
│   ├── start-bot.sh          # foreground/background bot launcher
│   ├── stop-bot.sh
│   └── backup.sh             # timestamped tarball, excludes secrets
├── logs/                     # bridge.log / bot.log / *.pid (runtime)
├── data/                     # local SQLite (none yet; ST is canonical store)
└── venv/                     # Python 3.14 venv (gitignored)
```

## Quick start

```bash
cd ~/Documents/SillyTavern/connector

# 1. venv (already exists from Phase 4)
ls venv/bin/python || python3 -m venv venv && ./venv/bin/pip install -q \
    'python-telegram-bot>=21.0' aiohttp websockets python-dotenv playwright

# 2. Start the bridge
./scripts/start-bridge.sh

# 3. Install ChatBridge ST extension (one-time)
#    Already done in Phase 4. Files live at:
#    /Users/leo/Documents/SillyTavern/SillyTavern/public/scripts/extensions/third-party/SillyTavern-Extension-ChatBridge/

# 4. Open ST WebUI at http://localhost:8000
#    - The ChatBridge extension auto-connects on load
#    - Status indicator turns green when bridge is reachable

# 5. Configure and start the Telegram bot
cp telegram-bot/.env.example telegram-bot/.env
$EDITOR telegram-bot/.env       # fill TELEGRAM_BOT_TOKEN
./scripts/start-bot.sh --bg

# 6. Send /ping to the bot in Telegram
# 7. Send any text message to chat
```

## Run as LaunchAgents (optional)

Two plists installed but NOT auto-loaded. To enable:

```bash
launchctl load -w ~/Library/LaunchAgents/com.leo.sillytavern.plist
launchctl load -w ~/Library/LaunchAgents/com.leo.tavern-v2-bridge.plist
```

`com.leo.tavern-v2-bridge` keeps the bridge up and reconnects on crash.
The Telegram bot is intentionally NOT a LaunchAgent — it should be started
manually with `./scripts/start-bot.sh --bg` so Leo can stop it from
Telegram by sending `/stop` if desired, and so token rotation doesn't
require plist re-install.

## Tests

```bash
# Smoke (HTTP-only, no ST browser needed; uses headless extension simulator)
./scripts/start-bridge.sh
./venv/bin/python tests/headless_extension.py &   # leave running
./venv/bin/python tests/smoke.py

# E2E (real ST WebUI in headless Chromium; requires ST running + bridge running)
./venv/bin/python tests/e2e_playwright.py
```

## Backup

```bash
./scripts/backup.sh
# writes ~/Documents/SillyTavern/backups/<timestamp>.tgz (excludes .env files)
```

## Security posture

- All components bind to `127.0.0.1`.
- Telegram bot token lives in `telegram-bot/.env` (mode 600, never committed).
  When V2 graduates from prototype, move token to macOS Keychain and have
  `bot.py` read it via `security find-generic-password -s tavern-tg-bot -w`.
- Bridge user API requires `Authorization: Bearer <key>` (key from
  `bridge/.env`); ST internal auth is `ST_API_KEY` set in bridge .env.
- AGPL-3.0 obligation: ChatBridge is AGPL-3.0. For personal loopback use this
  does not trigger. **Do not serve ChatBridge over a public network without
  publishing source.**

## Known issues (carried from Phase 2/3 risk register)

- **R-101**: APNG character card thumbnails fail (cosmetic; chat works).
- **R-103**: ChatBridge concurrency reply duplication; mitigated for V2 by
  the bridge's "st_response replaces accumulated" rule. Single-user only.
- **R-104**: ST was previously manual-startup; now a LaunchAgent plist is
  installed but not loaded. Load on demand (see above).

## References

- `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/CodingMarkdown/Projects/telegram-tavern/` — Obsidian planning vault.
- `~/Documents/telegramtavern/` — V1 (deprecated) Telegram bot code; do NOT modify.
- `~/Documents/SillyTavern/SillyTavern/` — SillyTavern install.
- `~/Documents/SillyTavern/SillyTavern/public/scripts/extensions/third-party/SillyTavern-Extension-ChatBridge/` — ST extension install.