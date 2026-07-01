#!/bin/bash
# Start MUSHOKU-dedicated bridge (HTTP :8017 / WS :8016).
# Reuses st_bridge.py with MUSHOKU-specific env vars.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export USER_API_PORT="${USER_API_PORT:-8017}"
export USER_API_HOST="${USER_API_HOST:-127.0.0.1}"
export USER_API_KEY="${USER_API_KEY:-tavern-mushoku-user-api-key-change-me}"
export WS_PORT="${WS_PORT:-8016}"
export ST_BASE_URL="${ST_BASE_URL:-http://127.0.0.1:8015}"
export ST_EXTENSION_SETTINGS_DIR="${ST_EXTENSION_SETTINGS_DIR:-$ROOT/../mushoku-data/default-user}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/venv/bin/python}"
cd "$ROOT"
mkdir -p logs
nohup "$PYTHON_BIN" \
    bridge/st_bridge.py \
    > logs/mushoku-bridge.log 2>&1 &
echo $! > logs/mushoku-bridge.pid
echo "MUSHOKU bridge started (HTTP :8017 / WS :8016), PID $(cat logs/mushoku-bridge.pid)"
