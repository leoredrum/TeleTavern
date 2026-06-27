#!/bin/bash
# Start the DM-isolated V2 bridge (loopback): HTTP :8013 / WS :8011.
# Reuses bridge/st_bridge.py via env. Does NOT touch :8003/:8001 (Penelope).
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"; cd "$HERE"
mkdir -p logs
PID_FILE="$HERE/logs/dm-bridge.pid"
LOG_FILE="$HERE/logs/dm-bridge.log"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "dm-bridge already running PID $(cat "$PID_FILE")"; exit 0
fi
[ -d venv ] || { echo "ERROR: venv missing"; exit 1; }
USER_API_PORT=8013 WS_PORT=8011 USER_API_HOST=127.0.0.1 WS_HOST=127.0.0.1 \
USER_API_KEY=tavern-dm-user-api-key-change-me REQUEST_TIMEOUT_S=300 \
nohup ./venv/bin/python bridge/st_bridge.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "dm-bridge started PID $(cat "$PID_FILE"); HTTP :8013 / WS :8011; logs $LOG_FILE"
sleep 1
kill -0 "$(cat "$PID_FILE")" 2>/dev/null || { echo "dm-bridge died:"; tail -20 "$LOG_FILE"; exit 1; }
