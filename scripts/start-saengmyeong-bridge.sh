#!/bin/bash
# Start SAENGMYEONG-dedicated bridge (HTTP :8022 / WS :8021).
# Reuses st_bridge.py with SAENGMYEONG-specific env vars.
set -e
export USER_API_PORT=8022
export USER_API_HOST=127.0.0.1
export USER_API_KEY=tavern-saengmyeong-user-api-key-change-me
export WS_PORT=8021
export ST_BASE_URL=http://127.0.0.1:8020
export ST_EXTENSION_SETTINGS_DIR=/Users/leo/Documents/SillyTavern/saengmyeong-data/default-user
cd "$(dirname "$0")/.."
mkdir -p logs
nohup /Users/leo/Documents/SillyTavern/connector/venv/bin/python \
    bridge/st_bridge.py \
    > logs/saengmyeong-bridge.log 2>&1 &
echo $! > logs/saengmyeong-bridge.pid
echo "SAENGMYEONG bridge started (HTTP :8022 / WS :8021), PID $(cat logs/saengmyeong-bridge.pid)"
