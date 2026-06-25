#!/usr/bin/env bash
# local_chat_smoke_test.sh — drive each bot's pipeline + ollama directly
# (no Telegram) and assert a sane Chinese reply.
#
# Per bot:
#   - loads .env, pipeline, character card
#   - sends "你好，今天我们做什么？"
#   - checks reply: non-empty, >30% Chinese, no traceback, no English leak
#     (small amount of English RP slang is OK), no assistant meta-talk
#
# Outputs PASS/FAIL per bot + first 100 chars of reply.

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SMOKE_USER_MSG="${SMOKE_USER_MSG:-你好，今天我们做什么？}"
LOG_DIR="$REPO_DIR/logs"
mkdir -p "$LOG_DIR"
SMOKE_LOG="$LOG_DIR/smoke_test.log"

name_to_char_png() {
  case "$1" in
    penelope) echo "Penelope3.png" ;;
    june)     echo "June.png" ;;
    aqua)     echo "Aqua.png" ;;
  esac
}

run_one() {
  local bot="$1"
  local base="$REPO_DIR/$bot"
  local png; png="$(name_to_char_png "$bot")"
  local py="$base/venv/bin/python"

  if [ ! -x "$py" ]; then
    echo "  $bot: FAIL — venv python missing ($py)"
    return 1
  fi

  local result
  result="$(
    cd "$base" && \
    OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}" \
    MODEL_NAME="${MODEL_NAME:-fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest}" \
    SMOKE_USER_MSG="$SMOKE_USER_MSG" \
    "$py" - <<'PYEOF' 2>&1
import asyncio, os, sys, json
sys.path.insert(0, os.getcwd())
from dotenv import dotenv_values
from character_card import load_character
from pipeline import PromptPipeline
from director import DirectorConfig
from ollama_client import OllamaClient

USER_MSG = os.environ.get("SMOKE_USER_MSG", "你好，今天我们做什么？")

cfg = dotenv_values(".env")
char_path = cfg.get("CHARACTER_PATH", "data/character.png")
card = load_character(char_path)

director_cfg = DirectorConfig(
    enabled=True, no_option_listing=True, no_meta_commentary=True,
    no_prompt_explanation=True, no_fourth_wall=True,
    no_repeat_micro_expressions=True, no_emotion_loops=True,
    no_user_repeat=True, advance_narrative=True, proactive_role=True,
    avoid_purple_prose=True, reply_length_min=80, reply_length_max=280,
)
pipeline = PromptPipeline(
    card=card,
    director_cfg=director_cfg,
    persona=None,
    char_label=cfg.get("CHAR_LABEL", "Character"),
    user_label=cfg.get("USER_LABEL", "Friend"),
    reply_language=cfg.get("REPLY_LANGUAGE", "Chinese") or "Chinese",
)
client = OllamaClient(
    base_url=os.environ.get("OLLAMA_URL", cfg.get("OLLAMA_URL", "http://127.0.0.1:11434")),
    model=os.environ.get("MODEL_NAME", cfg.get("OLLAMA_MODEL", "fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:latest")),
    max_tokens=int(cfg.get("MAX_TOKENS", "1024")),
)

history = [{"role": "user", "content": USER_MSG}]
messages = pipeline.assemble(history, "")

async def run():
    out = []
    async for piece in client.stream_chat(messages):
        out.append(piece)
    return "".join(out)

reply = asyncio.run(run())
print("===REPLY_START===")
print(reply)
print("===REPLY_END===")
PYEOF
  )"

  # Save to log
  echo "=== $bot @ $(date '+%H:%M:%S') ===" >> "$SMOKE_LOG"
  echo "$result" >> "$SMOKE_LOG"

  # Extract reply
  local reply
  reply="$(echo "$result" | awk '/===REPLY_START===/{f=1;next}/===REPLY_END===/{f=0}f')"

  if [ -z "$reply" ]; then
    echo "  $bot: FAIL — empty reply (log: $SMOKE_LOG)"
    return 1
  fi
  if echo "$reply" | grep -qiE 'Traceback|Error|Exception'; then
    echo "  $bot: FAIL — error in reply: $(echo "$reply" | head -1 | head -c 100)"
    return 1
  fi

  # Chinese ratio
  local cjk; cjk="$(echo "$reply" | python3 -c "import sys; s=sys.stdin.read(); print(sum(1 for c in s if '\u4e00'<=c<='\u9fff'))")"
  local total; total="$(echo "$reply" | python3 -c "import sys; s=sys.stdin.read(); print(sum(1 for c in s if not c.isspace()))")"
  local ratio
  if [ "${total:-0}" -gt 0 ]; then
    ratio=$(( cjk * 100 / total ))
  else
    ratio=0
  fi

  if [ "${ratio}" -lt 30 ]; then
    echo "  $bot: FAIL — only ${ratio}% Chinese"
    echo "    preview: $(echo "$reply" | head -c 100)"
    return 1
  fi

  # Check for obvious meta-talk
  if echo "$reply" | grep -qE '作为.*AI|语言模型|让我来扮演|根据.{0,8}(设定|指令)'; then
    echo "  $bot: FAIL — meta-talk detected"
    echo "    preview: $(echo "$reply" | head -c 100)"
    return 1
  fi

  echo "  $bot: PASS — zh=${ratio}% len=${#reply}"
  echo "    preview: $(echo "$reply" | head -c 100)"
  return 0
}

echo "=== local chat smoke test @ $(date '+%Y-%m-%d %H:%M:%S') ==="
echo "user_msg: $SMOKE_USER_MSG"

fail=0
for bot in penelope june aqua; do
  if ! run_one "$bot"; then
    fail=$((fail+1))
  fi
done

exit $fail
