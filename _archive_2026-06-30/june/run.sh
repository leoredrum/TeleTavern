#!/usr/bin/env bash
# Run the SillyTavern Telegram bridge.
# Usage: ./run.sh            (foreground)
#        ./run.sh --bg       (background, logs to bot.log)
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
    echo "[run.sh] creating venv..."
    python3 -m venv venv
    ./venv/bin/pip install -q --upgrade pip
    ./venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
    echo "[run.sh] .env missing — copy from .env.example and fill in TELEGRAM_BOT_TOKEN"
    exit 1
fi

if [ "${1:-}" = "--bg" ]; then
    nohup ./venv/bin/python bot.py > bot.log 2>&1 &
    echo "PID=$!"
    echo "logs: tail -f $(pwd)/bot.log"
else
    exec ./venv/bin/python bot.py
fi