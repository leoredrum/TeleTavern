#!/bin/bash
# Start SAENGMYEONG st-runner (headless Chromium keeps ST :8020 WebUI alive).
# Uses dedicated profile ~/.st-runner-saengmyeong-profile so it doesn't conflict
# with Penelope (:8000) or DM (:8010).
set -e
export ST_URL=http://127.0.0.1:8020
export ST_RUNNER_PROFILE="$HOME/.st-runner-saengmyeong-profile"
export ST_BRIDGE_WS_URL=ws://127.0.0.1:8021
cd "$(dirname "$0")/.."
mkdir -p logs
nohup /Users/leo/Documents/SillyTavern/connector/venv/bin/python \
    tests/dm_st_runner.py \
    > logs/saengmyeong-st-runner.log 2>&1 &
echo $! > logs/saengmyeong-st-runner.pid
echo "SAENGMYEONG st-runner started, PID $(cat logs/saengmyeong-st-runner.pid)"
