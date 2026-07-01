#!/bin/bash
# Start the persistent headless ST WebUI runner.
# This keeps the ST ChatBridge extension alive so the V2 Telegram bot
# can reach SillyTavern through the bridge.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

mkdir -p logs

PID_FILE="$HERE/logs/st-runner.pid"
LOG_FILE="$HERE/logs/st-runner.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "st-runner already running PID $(cat "$PID_FILE")"
    exit 0
fi

# Verify dependencies
if [ ! -d venv ]; then
    echo "ERROR: venv missing. Run: python3 -m venv venv && ./venv/bin/pip install -r requirements-runner.txt"
    exit 1
fi

# Ensure Playwright chromium is installed
if ! "$HERE/venv/bin/playwright" install --dry-run chromium 2>&1 | grep -q "is installed"; then
    "$HERE/venv/bin/playwright" install chromium 2>&1 | tail -3
fi

ST_BRIDGE_WS_URL=ws://127.0.0.1:8001 nohup ./venv/bin/python tests/persistent_st_runner.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
PID=$(cat "$PID_FILE")
echo "st-runner started PID $PID; logs at $LOG_FILE"

# Wait briefly and verify WS connection
sleep 8
if kill -0 "$PID" 2>/dev/null; then
    WS=$(lsof -iTCP:8001 -sTCP:ESTABLISHED -P -n 2>/dev/null | grep -v LISTEN | wc -l)
    if [ "$WS" -gt 0 ]; then
        echo "ChatBridge WS connected to bridge"
    else
        echo "WARNING: runner running but no WS connection yet; check $LOG_FILE"
    fi
else
    echo "ERROR: runner died; tail of log:"
    tail -20 "$LOG_FILE"
    exit 1
fi