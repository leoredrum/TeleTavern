#!/usr/bin/env bash
# healthcheck.sh — 检查 bot 是否在跑 + 拉一次 getMe 验证 token
#
# Usage:
#   ./scripts/healthcheck.sh [penelope|june|aqua|all]
#
# Liveness signal priority:
#   1. PID in ./logs/<name>.pid (only set if started via ./scripts/start.sh --bg)
#   2. /getMe HTTP call to Telegram API  (always works if token valid + bot process alive)

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

check_one() {
  local bot="$1"
  local dir
  dir="$(name_to_dir "$bot")"
  local inst="$HOME/Projects/$dir"
  local pidfile="$REPO_DIR/logs/${bot}.pid"

  echo "=== $bot ($inst) ==="

  # (1) pidfile check
  if [[ -f "$pidfile" ]]; then
    local pid
    pid="$(cat "$pidfile")"
    if ps -p "$pid" > /dev/null 2>&1; then
      echo "  pidfile: PID $pid running"
    else
      echo "  pidfile: PID $pid gone (stale)"
    fi
  fi

  # (2) token + getMe
  if [[ -f "$inst/.env" ]]; then
    local token
    token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$inst/.env" | cut -d= -f2-)"
    if [[ -n "$token" && "$token" != "replace_me_with_real_token" ]]; then
      local me
      me="$(curl -sS --max-time 8 "https://api.telegram.org/bot${token}/getMe" 2>&1 || echo '{"ok":false,"error":"curl failed"}')"
      if echo "$me" | grep -q '"ok":true'; then
        local uname
        uname="$(echo "$me" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("result",{}).get("username","?"))' 2>/dev/null || echo '?')"
        echo "  getMe: OK (@${uname})"
      else
        echo "  getMe: FAILED — $me"
      fi
    else
      echo "  TELEGRAM_BOT_TOKEN: <unset or placeholder>"
    fi
  else
    echo "  .env: missing"
  fi

  # (3) optional last log line
  local logfile="$REPO_DIR/logs/${bot}.log"
  if [[ -f "$logfile" ]]; then
    local last
    last="$(tail -n 1 "$logfile" 2>/dev/null || true)"
    if [[ -n "$last" ]]; then
      echo "  last log: $last"
    fi
  fi
}

case "$TARGET" in
  all)
    for b in penelope june aqua; do check_one "$b"; done
    ;;
  penelope|june|aqua)
    check_one "$TARGET"
    ;;
  *)
    echo "usage: $0 [penelope|june|aqua|all]"; exit 1
    ;;
esac