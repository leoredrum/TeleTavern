#!/bin/bash
# Start the Ollama OpenAI-compat proxy that translates ST's
# /v1/chat/completions → Ollama's native /api/chat with think:false.
set -euo pipefail
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
cd "$HERE"

mkdir -p logs

PID_FILE="$HERE/logs/ollama_proxy.pid"
LOG_FILE="$HERE/logs/ollama_proxy.log"

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "ollama_proxy already running PID $(cat "$PID_FILE")"
    exit 0
fi

if [ ! -d venv ]; then
    echo "ERROR: venv missing"
    exit 1
fi

nohup ./venv/bin/python scripts/ollama_openai_proxy.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
PID=$(cat "$PID_FILE")
echo "ollama_proxy started PID $PID on 127.0.0.1:11435; logs at $LOG_FILE"

# Quick health check
sleep 2
if curl -sf http://127.0.0.1:11435/v1/models -o /dev/null; then
    echo "proxy healthy (/v1/models returned 200)"
else
    echo "WARN: /v1/models check failed; check $LOG_FILE"
fi