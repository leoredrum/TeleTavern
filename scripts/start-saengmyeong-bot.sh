#!/bin/bash
# Start SAENGMYEONG bot (圣生) process.
# Reads .env (mode 600, gitignored).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOT_DIR="$ROOT/saengmyeong-bot"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/venv/bin/python}"
cd "$BOT_DIR"
set -a
. ./.env
set +a
mkdir -p logs
nohup "$PYTHON_BIN" \
    bot.py \
    > logs/saengmyeong-bot.log 2>&1 &
echo $! > logs/saengmyeong-bot.pid
echo "SAENGMYEONG bot (圣生) started, PID $(cat logs/saengmyeong-bot.pid)"
