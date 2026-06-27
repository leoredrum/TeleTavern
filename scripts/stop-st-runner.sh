#!/bin/bash
# Stop the persistent headless ST WebUI runner.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
PID_FILE="$HERE/logs/st-runner.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "no pidfile; nothing to stop"
    exit 0
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    sleep 2
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    echo "st-runner stopped PID $PID"
else
    echo "st-runner pid $PID not running"
fi
rm -f "$PID_FILE"
# Also kill any orphaned chromium that came from the runner
pkill -f "chrome-headless-shell.*st-runner-profile" 2>/dev/null || true