#!/usr/bin/env bash
# overnight_loop.sh — auto-fix loop until 2 consecutive PASS rounds.
#
# Per round:
#   1. overnight_healthcheck.sh (process / token / paths / ollama / getMe)
#   2. local_chat_smoke_test.sh   (pipeline + ollama)
#   3. if FAIL: try minimal fix + restart affected bot
#   4. if PASS twice in a row: stop
#
# Max 8 rounds. Logs to logs/overnight_loop.log.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"
LOOP_LOG="$LOG_DIR/overnight_loop.log"

MAX_ROUNDS="${MAX_ROUNDS:-8}"
PASS_STREAK_REQUIRED="${PASS_STREAK_REQUIRED:-2}"

log() {
  local line; line="$(date '+%Y-%m-%d %H:%M:%S')  $*"
  echo "$line" | tee -a "$LOOP_LOG"
}

start_bot() {
  local bot="$1"
  local base="$REPO_DIR/$bot"
  local logfile="$base/bot.log"
  local pidfile="$REPO_DIR/logs/${bot}.pid"
  # Kill by pidfile + any python bot.py that's still alive (be safe)
  if [ -f "$pidfile" ]; then
    local old_pid
    old_pid="$(cat "$pidfile" 2>/dev/null || true)"
    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
      kill -9 "$old_pid" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  fi
  # Truncate log (don't fail if no log exists)
  : > "$logfile" 2>/dev/null || true
  ( cd "$base" && nohup ./venv/bin/python bot.py > "$logfile" 2>&1 & echo $! > "$pidfile" )
  sleep 1
  log "  → started $bot (PID=$(cat "$pidfile" 2>/dev/null || echo '?'))"
}

start_all_bots() {
  log "starting all bots..."
  for bot in penelope june aqua; do
    start_bot "$bot"
  done
  # Give bots time to register with telegram
  sleep 6
}

kill_all_bots() {
  log "killing all bots..."
  for bot in penelope june aqua; do
    local pidfile="$REPO_DIR/logs/${bot}.pid"
    if [ -f "$pidfile" ]; then
      local ppid
      ppid="$(cat "$pidfile" 2>/dev/null || true)"
      if [ -n "$ppid" ] && kill -0 "$ppid" 2>/dev/null; then
        kill "$ppid" 2>/dev/null || true
      fi
    fi
  done
  sleep 2
  # final cleanup if any survive
  pkill -f "bot\.py" 2>/dev/null || true
  sleep 1
}

restart_bot() {
  local bot="$1"
  log "  → restarting $bot"
  start_bot "$bot"
  sleep 4
}

# Minimal auto-fix attempts — most things should be already correct after
# the previous rounds. These cover known failure modes.
auto_fix() {
  local health_out="$1"
  local smoke_out="$2"
  local changed=0

  # Fix 1: any bot with "unreachable" ollama — try restarting ollama via launchctl?
  # (skip — user has ollama managed externally)
  if echo "$health_out" | grep -q "unreachable at"; then
    log "FIX: ollama unreachable — sleeping 5s and re-trying (caller decides restart)"
    sleep 5
    changed=1
  fi

  # Fix 2: process missing for any bot → restart it
  for bot in penelope june aqua; do
    if echo "$health_out" | grep -A2 "^--- $bot ---" | grep -q "process:"; then
      # process line exists but empty means no process
      local proc_line
      proc_line="$(echo "$health_out" | grep -A2 "^--- $bot ---" | grep "process:" | head -1)"
      case "$proc_line" in
        *"no-pidfile-or-dead"*|*"no-process"*)
          log "FIX: starting $bot (no process running)"
          restart_bot "$bot"
          changed=1
          ;;
      esac
    fi
  done

  # Fix 3: smoke test reports "as AI" / "let me play" → not auto-fixable here
  # (would require prompt tuning — out of overnight scope)

  # Fix 4: smoke test reports empty reply — most often ollama cold-load issue;
  # retry once after restart
  if echo "$smoke_out" | grep -q "empty reply"; then
    log "FIX: smoke empty reply — retrying once after short sleep"
    sleep 3
    changed=1
  fi

  return $changed
}

# Main loop
log "=== overnight loop start (max $MAX_ROUNDS rounds, $PASS_STREAK_REQUIRED consecutive PASS required) ==="
log "repo: $REPO_DIR"

# Make sure bots are running at start
start_all_bots

pass_streak=0
total_rounds=0
overall=FAIL

for round in $(seq 1 "$MAX_ROUNDS"); do
  total_rounds=$round
  log ""
  log "===== ROUND $round / $MAX_ROUNDS ====="

  health_out="$(./scripts/overnight_healthcheck.sh 2>&1)"
  health_rc=$?
  echo "$health_out" >> "$LOOP_LOG"

  sleep 1

  smoke_out="$(./scripts/local_chat_smoke_test.sh 2>&1)"
  smoke_rc=$?
  echo "$smoke_out" >> "$LOOP_LOG"

  if [ "$health_rc" -eq 0 ] && [ "$smoke_rc" -eq 0 ]; then
    pass_streak=$((pass_streak + 1))
    log "ROUND $round: PASS (streak=$pass_streak)"
    if [ "$pass_streak" -ge "$PASS_STREAK_REQUIRED" ]; then
      log "reached $PASS_STREAK_REQUIRED consecutive PASS — stopping"
      overall=PASS
      break
    fi
  else
    pass_streak=0
    log "ROUND $round: FAIL — health_rc=$health_rc smoke_rc=$smoke_rc"
    log "  --- health (last 12 lines) ---"
    echo "$health_out" | tail -12 | sed 's/^/  /'
    log "  --- smoke (last 12 lines) ---"
    echo "$smoke_out" | tail -12 | sed 's/^/  /'

    log "attempting minimal fix..."
    if auto_fix "$health_out" "$smoke_out"; then
      sleep 3
    fi
  fi
done

log ""
log "===== OVERNIGHT LOOP END ====="
log "rounds: $total_rounds"
log "pass_streak: $pass_streak"
log "overall: $overall"
log "log: $LOOP_LOG"

if [ "$overall" = "PASS" ]; then
  exit 0
else
  exit 1
fi
