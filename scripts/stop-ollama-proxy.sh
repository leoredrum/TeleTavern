#!/bin/bash
# Stop the Ollama OpenAI-compat proxy.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
PID_FILE="$HERE/logs/ollama_proxy.pid"

if [ ! -f "$PID_FILE" ]; then
    echo "no pidfile; nothing to stop"
    exit 0
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    sleep 1
    kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    echo "ollama_proxy stopped PID $PID"
else
    echo "ollama_proxy pid $PID not running"
fi
rm -f "$PID_FILE"