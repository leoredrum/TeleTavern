#!/bin/bash
# Start the Telegram bot (foreground or --bg).
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

if [ ! -f telegram-bot/.env ]; then
    echo "ERROR: telegram-bot/.env not found. Copy .env.example and fill TELEGRAM_BOT_TOKEN."
    exit 1
fi

PID_FILE="$HERE/logs/bot.pid"
LOG_FILE="$HERE/logs/bot.log"

if [ "${1:-}" = "--bg" ]; then
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "bot already running PID $(cat "$PID_FILE")"
        exit 0
    fi
    nohup ./venv/bin/python telegram-bot/bot.py >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "bot started PID $(cat "$PID_FILE"); logs at $LOG_FILE"
    sleep 1
    if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "bot failed to start; tail of log:"
        tail -20 "$LOG_FILE"
        exit 1
    fi
else
    exec ./venv/bin/python telegram-bot/bot.py
fi