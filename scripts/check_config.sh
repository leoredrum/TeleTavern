#!/usr/bin/env bash
# check_config.sh — validate that the three Telegram Tavern bot instances
# are wired up correctly AND that secrets stay out of git.
#
# Usage:
#   ./scripts/check_config.sh                  # check all three
#   ./scripts/check_config.sh penelope         # check one
#
# Exit code is 0 only when every check passes.
#
# What we check (per bot):
#   1. .env exists at <bot>/.env
#   2. TELEGRAM_BOT_TOKEN is set and non-empty
#   3. CHARACTER_PATH target exists (file or dir)
#   4. DB_PATH parent directory exists
#   5. SHARED_DB_PATH parent directory exists
#   6. <bot>/.env is NOT tracked by git (and not staged)
#   7. <bot>/.env.example IS tracked by git
#
# Repo-wide:
#   8. No TELEGRAM_BOT_TOKEN= occurrence in tracked files
#   9. No real Telegram-bot token form (`<digits>:<base64>`) in tracked files

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

BOTS=(penelope june aqua)
TOTAL=0
FAIL=0
declare -a FAILURES=()

ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail() { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAIL=$((FAIL+1)); FAILURES+=("$1"); }
note() { printf "        %s\n" "$1"; }

# Telegram bot token format: <bot_id>:<35+ chars of base64/urlsafe chars>
# We don't try to be exhaustive — we just look for the <digits>:<long> shape.
TOKEN_RE='[0-9]{6,12}:[A-Za-z0-9_-]{30,}'

check_bot() {
  local bot="$1"
  local base="$REPO_DIR/$bot"
  local env="$base/.env"

  echo "=== $bot ==="
  TOTAL=$((TOTAL+1))

  # 1. .env exists
  if [[ -f "$env" ]]; then ok "env file present"; else fail "env file present ($env missing)"; return; fi

  # 2. TELEGRAM_BOT_TOKEN non-empty
  local token
  token="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$env" | cut -d= -f2- || true)"
  if [[ -n "$token" ]]; then
    ok "TELEGRAM_BOT_TOKEN set"
  else
    fail "TELEGRAM_BOT_TOKEN set (empty or missing)"
  fi

  # 3. CHARACTER_PATH target exists
  local char_path
  char_path="$(grep -E '^CHARACTER_PATH=' "$env" | cut -d= -f2- || true)"
  if [[ -n "$char_path" ]]; then
    if [[ -e "$char_path" ]]; then
      ok "CHARACTER_PATH exists ($char_path)"
    else
      fail "CHARACTER_PATH target missing ($char_path)"
    fi
  else
    fail "CHARACTER_PATH set (empty)"
  fi

  # 4. DB_PATH parent directory exists
  local db_path
  db_path="$(grep -E '^DB_PATH=' "$env" | cut -d= -f2- || true)"
  if [[ -n "$db_path" ]]; then
    local db_parent
    db_parent="$(dirname "$db_path")"
    if [[ -d "$db_parent" ]]; then
      ok "DB_PATH parent exists ($db_parent)"
    else
      fail "DB_PATH parent missing ($db_parent)"
    fi
  else
    fail "DB_PATH set (empty)"
  fi

  # 5. SHARED_DB_PATH parent directory exists
  local shared_path
  shared_path="$(grep -E '^SHARED_DB_PATH=' "$env" | cut -d= -f2- || true)"
  if [[ -n "$shared_path" ]]; then
    local shared_parent
    shared_parent="$(dirname "$shared_path")"
    if [[ -d "$shared_parent" ]]; then
      ok "SHARED_DB_PATH parent exists ($shared_parent)"
    else
      fail "SHARED_DB_PATH parent missing ($shared_parent)"
    fi
  else
    fail "SHARED_DB_PATH set (empty)"
  fi

  # 6. .env must NOT be tracked or staged
  if git ls-files --error-unmatch "$env" >/dev/null 2>&1; then
    fail ".env is tracked by git (UNSAFE — remove from index!)"
  elif git diff --cached --name-only -- "$env" 2>/dev/null | grep -q .; then
    fail ".env is staged for commit (UNSAFE — unstage!)"
  else
    ok ".env is not tracked or staged"
  fi

  # 7. .env.example MUST be tracked
  if git ls-files --error-unmatch "$base/.env.example" >/dev/null 2>&1; then
    ok ".env.example is tracked"
  else
    fail ".env.example is NOT tracked (template won't ship)"
  fi
}

echo "Repo: $REPO_DIR"
echo

for b in "$@"; do
  case "$b" in
    penelope|june|aqua) check_bot "$b" ;;
    *) echo "unknown bot: $b"; exit 2 ;;
  esac
done

if [[ $# -eq 0 ]]; then
  for b in "${BOTS[@]}"; do check_bot "$b"; done
fi

echo
echo "=== Repo-wide secret scan (tracked files only) ==="
TOTAL=$((TOTAL+1))
# 8. No literal TELEGRAM_BOT_TOKEN= in tracked files (except .env.example which
#    should leave it blank or as a placeholder).
hit_env_kv="$(git grep -nE '^TELEGRAM_BOT_TOKEN=[^[:space:]]+' -- ':!.env.example' ':!docs/' ':!scripts/' 2>/dev/null || true)"
if [[ -n "$hit_env_kv" ]]; then
  fail "TELEGRAM_BOT_TOKEN=<value> found in tracked files:"
  note "$hit_env_kv"
else
  ok "no real TELEGRAM_BOT_TOKEN value in tracked files"
fi

# 9. No real Telegram bot token shape in tracked files
hit_token="$(git grep -nE "$TOKEN_RE" -- ':!.env.example' ':!docs/' ':!scripts/' 2>/dev/null || true)"
if [[ -n "$hit_token" ]]; then
  fail "real-looking Telegram bot token shape found in tracked files:"
  note "$hit_token"
else
  ok "no Telegram-bot-token shape in tracked files"
fi

echo
echo "=== Summary ==="
echo "  checks: $TOTAL, failures: $FAIL"
if [[ $FAIL -gt 0 ]]; then
  echo "  RESULT: FAIL"
  for f in "${FAILURES[@]}"; do echo "    - $f"; done
  exit 1
fi
echo "  RESULT: PASS"
exit 0
