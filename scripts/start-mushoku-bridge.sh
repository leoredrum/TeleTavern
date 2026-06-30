#!/bin/bash
# Start MUSHOKU-dedicated bridge (HTTP :8017 / WS :8016).
# Reuses st_bridge.py with MUSHOKU-specific env vars.
set -e
export BRIDGE_HTTP_PORT=8017
export BRIDGE_WS_PORT=8016
export ST_BASE_URL=http://127.0.0.1:8015
export ST_EXTENSION_SETTINGS_DIR=/Users/leo/Documents/SillyTavern/mushoku-data/default-user
cd "$(dirname "$0")/.."
mkdir -p logs
nohup /opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    bridge/st_bridge.py \
    > logs/mushoku-bridge.log 2>&1 &
echo $! > logs/mushoku-bridge.pid
echo "MUSHOKU bridge started (HTTP :8017 / WS :8016), PID $(cat logs/mushoku-bridge.pid)"
