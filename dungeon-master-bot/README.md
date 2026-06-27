# Dungeon Master Bot

Fourth Telegram bot. RPG Game Master. **Isolated instance (plan B)** — does not
share the Penelope bridge or ST instance.

## Status

**Skeleton (DM-Phase 0). Not running.** Will not start until:
1. `.env` has a fourth Telegram Bot Token (create one via @BotFather), AND
2. the DM bridge (`:8013`/`:8011`) + DM st-runner (isolated Chromium with
   `DungeonMaster12` active) are deployed.

See `../../DungeonMaster/` (Obsidian) for full design: ARCHITECTURE, COMMANDS,
SAVE_SYSTEM, GAME_DESIGN (Chinese Language Override), etc.

## Run (once prerequisites are met)

```bash
cd ~/Documents/SillyTavern/connector
cp dungeon-master-bot/.env.example dungeon-master-bot/.env   # then edit token
./scripts/start-dm-bot.sh --bg
```

## Isolation

- Own token, own bridge port (`:8013`), own SQLite DB (`logs/dm_save.db`).
- Does not touch Penelope / June / Aqua or the shared `:8003` bridge.
- Character card `DungeonMaster12.png` and its `DnD-Base` lorebook stay
  untouched (English); Chinese is enforced at the Prompt layer only.
