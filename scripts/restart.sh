#!/usr/bin/env bash
# restart.sh — 停止后启动
#
# Usage:
#   ./scripts/restart.sh [penelope|june|aqua|all] [--bg] [--from-repo]

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/stop.sh" "$@"
sleep 1
"$SCRIPT_DIR/start.sh" "$@"