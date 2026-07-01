#!/bin/bash
# Start DM-isolated persistent headless ST WebUI runner.
# Connects to DM ST :8010 (DungeonMaster12), profile ~/.st-runner-dm-profile.
# ChatBridge extension auto-activates DungeonMaster12 + auto-connects WS -> :8011.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"; cd "$HERE"
mkdir -p logs
PID_FILE="$HERE/logs/dm-st-runner.pid"
LOG_FILE="$HERE/logs/dm-st-runner.log"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "dm-st-runner already running PID $(cat "$PID_FILE")"; exit 0
fi
[ -d venv ] || { echo "ERROR: venv missing"; exit 1; }
ST_URL=http://127.0.0.1:8010 ST_BRIDGE_WS_URL=ws://127.0.0.1:8011 ST_RUNNER_PROFILE="$HOME/.st-runner-dm-profile" ST_RUNNER_PID_FILE=logs/dm-st-runner.pid nohup ./venv/bin/python tests/dm_st_runner.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "dm-st-runner started PID $(cat "$PID_FILE") -> ST :8010; logs $LOG_FILE"
