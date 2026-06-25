#!/usr/bin/env bash
# stop.sh — 停止一个或全部 Telegram Tavern bot 实例
#
# Usage:
#   ./scripts/stop.sh                     # 停全部
#   ./scripts/stop.sh penelope|june|aqua  # 停单个
#
# 优先使用 ./logs/<name>.pid 记录的 PID；找不到则尝试 pkill python 路径匹配。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-all}"

name_to_dir() {
  case "$1" in
    penelope) echo "sillytavern-telegram-bot" ;;
    june)     echo "june-telegram-bot" ;;
    aqua)     echo "aqua-telegram-bot" ;;
    *) echo ""; return 1 ;;
  esac
}

stop_one() {
  local bot="$1"
  local dir
  dir="$(name_to_dir "$bot")"
  local pidfile="$REPO_DIR/logs/${bot}.pid"

  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile")"
    if ps -p "$pid" > /dev/null 2>&1; then
      echo "[stop.sh] killing $bot PID=$pid"
      kill "$pid"
      sleep 1
      if ps -p "$pid" > /dev/null 2>&1; then
        echo "[stop.sh] still alive, SIGKILL"
        kill -9 "$pid"
      fi
    else
      echo "[stop.sh] $bot PID $pid not running"
    fi
    rm -f "$pidfile"
  else
    # Fallback: kill python bot.py in the instance dir
    if [[ -n "$dir" ]]; then
      local pids
      pids="$(pgrep -f "Projects/$dir/.*bot.py" || true)"
      if [[ -n "$pids" ]]; then
        echo "[stop.sh] killing $bot by path match: $pids"
        kill $pids
      else
        echo "[stop.sh] no running process for $bot"
      fi
    fi
  fi
}

case "$TARGET" in
  all)
    for b in penelope june aqua; do stop_one "$b"; done
    ;;
  penelope|june|aqua)
    stop_one "$TARGET"
    ;;
  *)
    echo "usage: $0 [penelope|june|aqua|all]"; exit 1
    ;;
esac