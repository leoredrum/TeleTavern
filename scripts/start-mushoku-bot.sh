#!/bin/bash
# Start MUSHOKU bot process.
# Reads .env (mode 600, gitignored).
set -e
BOT_DIR="$(dirname "$0")/../mushoku-bot"
cd "$BOT_DIR"
set -a
. ./.env
set +a
mkdir -p logs
nohup /opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    bot.py \
    > logs/mushoku-bot.log 2>&1 &
echo $! > logs/mushoku-bot.pid
echo "MUSHOKU bot started, PID $(cat logs/mushoku-bot.pid)"
