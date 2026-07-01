#!/bin/bash
# Start SAENGMYEONG st-runner (headless Chromium keeps ST :8020 WebUI alive).
# Uses dedicated profile ~/.st-runner-saengmyeong-profile so it doesn't conflict
# with other local SillyTavern browser profiles.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ST_URL="${ST_URL:-http://127.0.0.1:8020}"
export ST_RUNNER_PROFILE="$HOME/.st-runner-saengmyeong-profile"
export ST_BRIDGE_WS_URL="${ST_BRIDGE_WS_URL:-ws://127.0.0.1:8021}"
export ST_RUNNER_PID_FILE="${ST_RUNNER_PID_FILE:-logs/saengmyeong-st-runner.pid}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/venv/bin/python}"
cd "$ROOT"
mkdir -p logs
nohup "$PYTHON_BIN" \
    tests/dm_st_runner.py \
    > logs/saengmyeong-st-runner.log 2>&1 &
echo $! > logs/saengmyeong-st-runner.pid
echo "SAENGMYEONG st-runner started, PID $(cat logs/saengmyeong-st-runner.pid)"
