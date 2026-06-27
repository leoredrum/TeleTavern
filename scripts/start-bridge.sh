#!/bin/bash
# Start the V2 bridge (loopback).
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

mkdir -p logs
PID_FILE="$HERE/logs/bridge.pid"
LOG_FILE="$HERE/logs/bridge.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "bridge already running PID $(cat "$PID_FILE")"
    exit 0
fi

if [ ! -d venv ]; then
    python3 -m venv venv
    ./venv/bin/pip install --quiet --upgrade pip
    ./venv/bin/pip install --quiet 'python-telegram-bot>=21.0' aiohttp websockets python-dotenv
fi

if [ ! -f bridge/.env ]; then
    cp bridge/.env.example bridge/.env
    echo "Wrote bridge/.env from example. Edit if you want non-default ports/keys."
fi

nohup ./venv/bin/python bridge/st_bridge.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "bridge started PID $(cat "$PID_FILE"); logs at $LOG_FILE"
sleep 1
if ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "bridge failed to start; tail of log:"
    tail -20 "$LOG_FILE"
    exit 1
fi