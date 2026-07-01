# TeleTavern

Personal Telegram entrypoint for a local SillyTavern server.

TeleTavern lets a Telegram bot talk to SillyTavern through an OpenAI-compatible bridge, while SillyTavern remains the source of truth for characters, lorebooks / World Info, prompts, chat history, memory, and extensions.

```text
Telegram Bot
  -> TeleTavern bot adapter
  -> OpenAI-compatible bridge
  -> ChatBridge WebSocket extension
  -> SillyTavern WebUI
  -> your local / remote model backend
```

This repository is a reference implementation extracted from a personal Mac Studio deployment. It is intended to help you build your own private "SillyTavern over Telegram" setup.

## What This Is

- A Python Telegram bot layer using `python-telegram-bot`.
- A local OpenAI-compatible HTTP bridge.
- A WebSocket bridge into SillyTavern through ChatBridge.
- Utility scripts for running multiple isolated bot / bridge stacks.
- Examples for Chinese-language output enforcement and Telegram long-message splitting.

## What This Is Not

- It is not SillyTavern itself.
- It is not a hosted service.
- It does not include private Telegram tokens.
- It does not include private chat logs, databases, SillyTavern data folders, or roleplay history.
- It does not include character card PNGs. Bring your own cards.

## Upstream And Attribution

- SillyTavern official website: https://sillytavern.app/
- SillyTavern source: https://github.com/SillyTavern/SillyTavern
- ChatBridge upstream used as the bridge basis: https://github.com/AyeeMinerva/SillyTavern-Extension-ChatBridge
- Telegram connector candidate evaluated during planning: https://github.com/qiqi20020612/SillyTavern-Telegram-Connector
- Character card source used in the original private deployment: https://aicharactercards.com/

Character cards are not redistributed here. If you use cards from a third-party site, follow that site's terms and the card author's permissions.

## License

This project includes bridge work derived from / designed around the AGPL-3.0 ChatBridge ecosystem. The repository is published under AGPL-3.0. If you run a modified network-accessible version, make the corresponding source available as required by the license.

## Repository Layout

```text
bridge/
  st_bridge.py              OpenAI-compatible HTTP API + WebSocket bridge

telegram-bot/
  bot.py                    generic single-character / switchable-character bot
  telegram_splitter.py      Telegram 4096-character splitting helper

dungeon-master-bot/
  bot.py                    RPG-oriented example bot
  game_state.py             optional structured state engine
  rpg_engine.py             optional RPG rules/state engine
  director_engine.py        optional scene/director engine

mushoku-bot/
  bot.py                    scenario-bot example with local fallback path
  card_parser.py            PNG character-card metadata parser
  ollama_client.py          direct model fallback helper
  ollama_fallback.py        scenario fallback prompt helper

saengmyeong-bot/
  bot.py                    second scenario-bot example
  card_parser.py            PNG character-card metadata parser
  ollama_client.py          direct model fallback helper
  ollama_fallback.py        scenario fallback prompt helper

scripts/
  start-*.sh / stop-*.sh    local process helpers

tests/
  smoke.py                  bridge smoke test
  e2e_playwright.py         real SillyTavern browser test
```

The scenario bot folders are examples. You will almost certainly want to rename them, replace their prompts, and point them at your own character cards.

## Requirements

- macOS or Linux.
- Python 3.11+.
- Node.js supported by your SillyTavern version.
- A working SillyTavern install.
- A model backend configured in SillyTavern, such as Ollama.
- A Telegram bot token from BotFather.
- The ChatBridge extension installed into SillyTavern.

## Quick Start

1. Install and run SillyTavern.

   Follow the official project: https://github.com/SillyTavern/SillyTavern

1. Install the ChatBridge extension.

   Use the upstream project as the source: https://github.com/AyeeMinerva/SillyTavern-Extension-ChatBridge

1. Create a Python virtualenv.

   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -U pip
   ./venv/bin/pip install \
     aiohttp \
     websockets \
     python-dotenv \
     python-telegram-bot \
     playwright
   ./venv/bin/playwright install chromium
   ```

1. Configure the bridge.

   ```bash
   cp bridge/.env.example bridge/.env
   $EDITOR bridge/.env
   ```

   Keep `USER_API_HOST=127.0.0.1` unless you know exactly why you are exposing it.

1. Start the bridge.

   ```bash
   ./scripts/start-bridge.sh
   ```

1. Open SillyTavern in a browser and connect ChatBridge to the bridge WebSocket.

   Default WebSocket URL:

   ```text
   ws://127.0.0.1:8001
   ```

1. Configure the Telegram bot.

   ```bash
   cp telegram-bot/.env.example telegram-bot/.env
   chmod 600 telegram-bot/.env
   $EDITOR telegram-bot/.env
   ```

1. Start the Telegram bot.

   ```bash
   ./scripts/start-bot.sh --bg
   ```

1. In Telegram, send:

   ```text
   /ping
   /character
   /start
   ```

## Character Cards

TeleTavern does not manage character cards itself. SillyTavern does.

Recommended flow:

1. Import your PNG / JSON character card into SillyTavern.
1. Confirm the character works in SillyTavern WebUI.
1. Confirm ChatBridge can see the character.
1. Use `/character` in Telegram to switch characters.

If you use the scenario fallback helpers, set the relevant card path in your bot `.env`:

```bash
CHARACTER_CARD_PATH=/absolute/path/to/your-card.png
```

Do not commit paid, private, adult, or author-restricted character cards unless you have permission.

## Secrets

Never commit:

- `.env`
- Telegram bot tokens
- API keys
- SillyTavern `secrets.json`
- `cookie-secret.txt`
- chat logs
- SQLite save databases
- character cards you cannot redistribute

The included `.gitignore` excludes local env files, logs, databases, sessions, backups, and runtime data.

## Common Customization Points

- `telegram-bot/bot.py`: character menu labels, command handlers, prompt wrapper.
- `bridge/st_bridge.py`: OpenAI-compatible bridge behavior and language override prefix.
- `telegram_splitter.py`: long Telegram reply splitting.
- `*_bot/.env.example`: ports and per-bot endpoints.
- `scripts/start-*.sh`: local process layout.

## Running Multiple Bots

The original private deployment used multiple bot stacks, each with separate ports:

```text
main bot       HTTP 8003 / WS 8001
RPG bot        HTTP 8013 / WS 8011
scenario bot   HTTP 8017 / WS 8016
scenario bot   HTTP 8022 / WS 8021
```

You can copy that pattern, but give each bot:

- its own Telegram token,
- its own bridge HTTP port,
- its own bridge WebSocket port,
- its own SillyTavern data root or active-character strategy,
- its own ignored `.env`.

## Maintenance Notes

- Keep SillyTavern and ChatBridge updated intentionally. Test after updating either one.
- If Telegram replies duplicate, check for multiple pollers using the same token.
- If the bridge returns `503 No ST extension connected`, reconnect ChatBridge or restart the browser runner.
- If long replies stop mid-sentence, check the model / SillyTavern max-token setting before blaming Telegram.
- If Chinese output leaks English, enforce language at the prompt/template layer; do not edit third-party character card metadata unless you have permission.

## Security Defaults

- Bind local services to `127.0.0.1`.
- Use a reverse proxy and TLS if you expose anything beyond localhost.
- Rotate a Telegram token immediately if it appears in a log or shell history.
- Keep bot logs out of git.

## Status

The original private deployment was tested with SillyTavern + Ollama + Telegram on macOS. This public repository is a sanitized template: expect to adapt paths, ports, prompts, and character configuration for your own setup.
