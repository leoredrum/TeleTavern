#!/bin/bash
# Start MUSHOKU st-runner (headless Chromium keeps ST :8015 WebUI alive).
# Uses dedicated profile ~/.st-runner-mushoku-profile so it doesn't conflict
# with Penelope (:8000) or DM (:8010).
set -e
export ST_URL=http://127.0.0.1:8015
export ST_RUNNER_PROFILE="$HOME/.st-runner-mushoku-profile"
export ST_BRIDGE_WS_URL=ws://127.0.0.1:8016
cd "$(dirname "$0")/.."
mkdir -p logs
nohup /opt/homebrew/Cellar/python@3.14/3.14.3_1/Frameworks/Python.framework/Versions/3.14/bin/python3 \
    tests/dm_st_runner.py \
    > logs/mushoku-st-runner.log 2>&1 &
echo $! > logs/mushoku-st-runner.pid
echo "MUSHOKU st-runner started, PID $(cat logs/mushoku-st-runner.pid)"
