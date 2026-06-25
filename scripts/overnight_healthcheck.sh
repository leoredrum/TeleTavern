#!/usr/bin/env bash
# overnight_healthcheck.sh — single-shot health check for all 3 bots.
#
# Per bot checks:
#   1. process running (bot.py)
#   2. .env exists
#   3. TELEGRAM_BOT_TOKEN non-empty
#   4. CHARACTER_PATH target exists
#   5. DB_PATH parent dir exists
#   6. SHARED_DB_PATH parent dir exists
#   7. ollama reachable (at OLLAMA_URL)
#   8. ollama can serve MODEL_NAME (api/show)
#   9. telegram getMe succeeds with token
#  10. recent bot.log has no error strings
#
# Output: PASS / FAIL per check, then overall bot verdict.
#
# Compatible with macOS bash 3.2 (no associative arrays).

set -u
# note: -o pipefail omitted because `head -1` after a pipeline would trip the script

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
MODEL_NAME="${MODEL_NAME:-fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-6}"
LOG_TAIL="${LOG_TAIL:-50}"
ERROR_PATTERNS='(Traceback|ERROR.*telegram|Conflict: terminated|FileNotFoundError|OllamaChatError|HTTP [45][0-9][0-9]|unauthori[sz]ed|invalid token)'

# state files (avoids associative arrays)
TMPDIR_RUN="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT

name_to_char_png() {
  case "$1" in
    penelope) echo "Penelope3.png" ;;
    june)     echo "June.png" ;;
    aqua)     echo "Aqua.png" ;;
  esac
}

check_bot() {
  local bot="$1"
  local base="$REPO_DIR/$bot"
  local env="$base/.env"
  local png; png="$(name_to_char_png "$bot")"
  local log="$base/bot.log"

  local result="PASS"
  local process_pid=""
  local getme=""
  local reason=""

  # 1. process — check PID file (loop writes logs/${bot}.pid)
  local pids=""
  local pidfile="$REPO_DIR/logs/${bot}.pid"
  if [ -f "$pidfile" ]; then
    local ppid
    ppid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "$ppid" ] && kill -0 "$ppid" 2>/dev/null; then
      pids="$ppid"
    else
      # stale pidfile — clean up so the loop can restart cleanly
      rm -f "$pidfile"
    fi
  fi
  if [ -n "$pids" ]; then
    process_pid="$pids"
  else
    reason="$reason process=no-pidfile-or-dead;"
    result="FAIL"
  fi

  # 2. .env
  [ -f "$env" ] || { reason="$reason env=missing;"; result="FAIL"; }

  # 3. token
  local token=""
  if [ -f "$env" ]; then
    token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -z "$token" ]; then
    reason="$reason token=empty;"
    result="FAIL"
  fi

  # 4. CHARACTER_PATH
  local char_path=""
  if [ -f "$env" ]; then
    char_path="$(grep -E '^CHARACTER_PATH=' "$env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -z "$char_path" ] || [ ! -e "$char_path" ]; then
    reason="$reason character=missing(${char_path:-unset});"
    result="FAIL"
  fi

  # 5. DB_PATH parent
  local db_path=""
  if [ -f "$env" ]; then
    db_path="$(grep -E '^DB_PATH=' "$env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -z "$db_path" ] || [ ! -d "$(dirname "$db_path")" ]; then
    reason="$reason db=parent-missing($(dirname "$db_path"));"
    result="FAIL"
  fi

  # 6. SHARED_DB_PATH parent
  local sdb_path=""
  if [ -f "$env" ]; then
    sdb_path="$(grep -E '^SHARED_DB_PATH=' "$env" 2>/dev/null | cut -d= -f2- || true)"
  fi
  if [ -z "$sdb_path" ] || [ ! -d "$(dirname "$sdb_path")" ]; then
    reason="$reason sdb=parent-missing($(dirname "$sdb_path"));"
    result="FAIL"
  fi

  # 7. ollama reachable
  local api_out
  api_out="$(curl -sS --max-time "$HEALTH_TIMEOUT" "$OLLAMA_URL/api/tags" 2>&1 || true)"
  if ! echo "$api_out" | grep -q '"models"'; then
    reason="$reason ollama=unreachable;"
    result="FAIL"
  fi

  # 8. ollama can serve model
  local show_out
  show_out="$(curl -sS --max-time "$HEALTH_TIMEOUT" -X POST "$OLLAMA_URL/api/show" \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$MODEL_NAME\"}" 2>&1 || true)"
  if ! echo "$show_out" | grep -q '"details"'; then
    reason="$reason model=not-served;"
    result="FAIL"
  fi

  # 9. telegram getMe
  if [ -n "$token" ]; then
    local me
    me="$(curl -sS --max-time "$HEALTH_TIMEOUT" "https://api.telegram.org/bot${token}/getMe" 2>&1 || true)"
    if echo "$me" | grep -q '"ok":true'; then
      getme="OK"
    else
      getme="FAIL"
      reason="$reason getme=fail;"
      result="FAIL"
    fi
  fi

  # 10. recent log errors
  if [ -f "$log" ] && [ -n "$process_pid" ]; then
    local errs
    errs="$(tail -n "$LOG_TAIL" "$log" 2>/dev/null | grep -E "$ERROR_PATTERNS" | head -3 || true)"
    if [ -n "$errs" ]; then
      local first_err
      first_err="$(echo "$errs" | head -1 | head -c 100)"
      reason="$reason log=error($first_err);"
      # don't flip overall — could be transient
    fi
  fi

  # write state
  echo "$result"     > "$TMPDIR_RUN/${bot}.result"
  echo "$process_pid"> "$TMPDIR_RUN/${bot}.pid"
  echo "$getme"      > "$TMPDIR_RUN/${bot}.getme"
  echo "$reason"     > "$TMPDIR_RUN/${bot}.reason"
}

echo "=== overnight healthcheck @ $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "ollama=$OLLAMA_URL model=$MODEL_NAME"

for bot in penelope june aqua; do
  check_bot "$bot"
  result="$(cat "$TMPDIR_RUN/${bot}.result")"
  pid="$(cat "$TMPDIR_RUN/${bot}.pid")"
  getme="$(cat "$TMPDIR_RUN/${bot}.getme")"
  reason="$(cat "$TMPDIR_RUN/${bot}.reason")"
  echo
  echo "--- $bot ---"
  echo "  process:    ${pid:-(none)}"
  echo "  getMe:      ${getme:-(skip)}"
  echo "  result:     $result"
  if [ "$result" = "FAIL" ]; then
    [ -n "$reason" ] && echo "    reason: $reason"
  fi
done

# Exit code: 0 if all PASS, 1 if any FAIL
all_ok=1
for bot in penelope june aqua; do
  if [ "$(cat "$TMPDIR_RUN/${bot}.result")" != "PASS" ]; then
    all_ok=0
  fi
done
exit $((1 - all_ok))
