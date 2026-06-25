#!/usr/bin/env bash
# start.sh — 启动一个或全部 Telegram Tavern bot 实例
#
# Usage:
#   ./scripts/start.sh                     # 启动全部
#   ./scripts/start.sh penelope|june|aqua  # 启动单个
#   ./scripts/start.sh --bg                # 后台模式（log 写 logs/<name>.log）
#   ./scripts/start.sh --from-repo <name>  # 用 ~/Documents/telegramtavern/ 的代码而非 ~/Projects/<name>/ 的副本
#
# 默认行为：管理 ~/Projects/<name>/ 的运行实例（已部署）。
# 加 --from-repo 时：从本仓库代码启动（需要先在本仓库配 venv + .env + 角色卡）。

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTANCES_DIR="$HOME/Projects"
TARGET="${1:-all}"
MODE="fg"
SOURCE="instance"  # instance | repo

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bg) MODE="bg"; shift ;;
    --from-repo) SOURCE="repo"; shift ;;
    penelope|june|aqua|all) TARGET="$1"; shift ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

# Map short names to instance dirs
name_to_dir() {
  case "$1" in
    penelope) echo "sillytavern-telegram-bot" ;;
    june)     echo "june-telegram-bot" ;;
    aqua)     echo "aqua-telegram-bot" ;;
    *) echo ""; return 1 ;;
  esac
}

ensure_logs() {
  mkdir -p "$REPO_DIR/logs"
}

start_one() {
  local bot="$1"
  local dir
  dir="$(name_to_dir "$bot")"
  if [[ -z "$dir" ]]; then
    echo "[start.sh] unknown bot: $bot"; return 1
  fi

  local bot_dir
  if [[ "$SOURCE" == "repo" ]]; then
    bot_dir="$REPO_DIR"
  else
    bot_dir="$INSTANCES_DIR/$dir"
  fi

  if [[ ! -d "$bot_dir" ]]; then
    echo "[start.sh] dir missing: $bot_dir"; return 1
  fi

  if [[ ! -f "$bot_dir/.env" ]]; then
    echo "[start.sh] .env missing in $bot_dir — copy from .env.example"
    return 1
  fi

  # venv check (auto-create if absent and --from-repo)
  if [[ ! -d "$bot_dir/venv" ]]; then
    if [[ "$SOURCE" == "repo" ]]; then
      echo "[start.sh] creating venv in $bot_dir ..."
      (cd "$bot_dir" && python3 -m venv venv && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt)
    else
      echo "[start.sh] venv missing in $bot_dir — run there once to bootstrap, then re-run start.sh"
      return 1
    fi
  fi

  local logfile="$REPO_DIR/logs/${bot}.log"
  echo "[start.sh] starting $bot from $bot_dir (log: $logfile)"

  if [[ "$MODE" == "bg" ]]; then
    (cd "$bot_dir" && nohup ./venv/bin/python bot.py > "$logfile" 2>&1 & echo $! > "$REPO_DIR/logs/${bot}.pid")
    echo "[start.sh] $bot PID=$(cat "$REPO_DIR/logs/${bot}.pid")"
  else
    (cd "$bot_dir" && exec ./venv/bin/python bot.py)
  fi
}

ensure_logs
case "$TARGET" in
  all)
    for b in penelope june aqua; do
      start_one "$b" || echo "[start.sh] $b failed"
    done
    ;;
  penelope|june|aqua)
    start_one "$TARGET"
    ;;
esac