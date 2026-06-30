#!/bin/bash
# Start SAENGMYEONG bot (圣生) process.
# Reads .env (mode 600, gitignored).
set -e
BOT_DIR="$(dirname "$0")/../saengmyeong-bot"
cd "$BOT_DIR"
set -a
. ./.env
set +a
mkdir -p logs
nohup /Users/leo/Documents/SillyTavern/connector/venv/bin/python \
    bot.py \
    > logs/saengmyeong-bot.log 2>&1 &
echo $! > logs/saengmyeong-bot.pid
echo "SAENGMYEONG bot (圣生) started, PID $(cat logs/saengmyeong-bot.pid)"
