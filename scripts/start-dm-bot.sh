#!/bin/bash
# Start the Dungeon Master Telegram bot (foreground or --bg).
# Isolated instance (plan B): own token, DM bridge :8013, own DB.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

ENV="$HERE/dungeon-master-bot/.env"
if [ ! -f "$ENV" ]; then
    echo "ERROR: dungeon-master-bot/.env not found. Copy .env.example and fill TELEGRAM_BOT_TOKEN."
    exit 1
fi
set -a; . "$ENV"; set +a

PID_FILE="$HERE/logs/dm-bot.pid"
LOG_FILE="$HERE/logs/dm-bot.log"

if [ "${1:-}" = "--bg" ]; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "dm-bot already running PID $(cat "$PID_FILE")"; exit 0
    fi
    nohup ./venv/bin/python dungeon-master-bot/bot.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "dm-bot started PID $(cat "$PID_FILE")"
else
    exec ./venv/bin/python dungeon-master-bot/bot.py
fi
