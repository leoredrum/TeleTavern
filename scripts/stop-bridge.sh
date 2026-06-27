#!/bin/bash
# Stop the V2 bridge.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
PID_FILE="$HERE/logs/bridge.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "no pidfile; nothing to stop"
    exit 0
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in 1 2 3 4 5; do
        if ! kill -0 "$PID" 2>/dev/null; then break; fi
        sleep 0.5
    done
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    echo "bridge stopped PID $PID"
else
    echo "bridge pid $PID not running"
fi
rm -f "$PID_FILE"