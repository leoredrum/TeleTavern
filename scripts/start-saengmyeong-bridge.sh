#!/bin/bash
# Start SAENGMYEONG-dedicated bridge (HTTP :8022 / WS :8021).
# Reuses st_bridge.py with SAENGMYEONG-specific env vars.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export USER_API_PORT="${USER_API_PORT:-8022}"
export USER_API_HOST="${USER_API_HOST:-127.0.0.1}"
export USER_API_KEY="${USER_API_KEY:-tavern-saengmyeong-user-api-key-change-me}"
export WS_PORT="${WS_PORT:-8021}"
export ST_BASE_URL="${ST_BASE_URL:-http://127.0.0.1:8020}"
export ST_EXTENSION_SETTINGS_DIR="${ST_EXTENSION_SETTINGS_DIR:-$ROOT/../saengmyeong-data/default-user}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/venv/bin/python}"
cd "$ROOT"
mkdir -p logs
nohup "$PYTHON_BIN" \
    bridge/st_bridge.py \
    > logs/saengmyeong-bridge.log 2>&1 &
echo $! > logs/saengmyeong-bridge.pid
echo "SAENGMYEONG bridge started (HTTP :8022 / WS :8021), PID $(cat logs/saengmyeong-bridge.pid)"
