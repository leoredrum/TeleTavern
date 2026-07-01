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
nohup ../venv/bin/python \
    bot.py \
    > logs/mushoku-bot.log 2>&1 &
echo $! > logs/mushoku-bot.pid
echo "MUSHOKU bot started, PID $(cat logs/mushoku-bot.pid)"
