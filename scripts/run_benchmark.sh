#!/usr/bin/env bash
# run_benchmark.sh — Phase 1 AB Test driver
#
# Usage:
#   ./scripts/run_benchmark.sh --on          # run with PHASE1_ENABLED=true
#   ./scripts/run_benchmark.sh --off         # run with PHASE1_ENABLED=false
#   ./scripts/run_benchmark.sh --compare     # run both, generate diff
#
# Output:
#   docs/benchmark_results/<ts>/on/<scenario>.txt
#   docs/benchmark_results/<ts>/off/<scenario>.txt
#   docs/benchmark_results/<ts>/diff_summary.md

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="$REPO_DIR/docs/benchmark_results/$TS"

MODEL="${OLLAMA_MODEL:-fredrezones55/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive}"

# Resolve character card
CHAR_PATH="${CHARACTER_PATH:-}"
if [[ -z "$CHAR_PATH" || ! -f "$CHAR_PATH" ]]; then
  for cand in \
    "$HOME/Projects/sillytavern-telegram-bot/data/Penelope3.png" \
    "$HOME/Projects/june-telegram-bot/data/June.png" \
    "$HOME/Projects/aqua-telegram-bot/data/Aqua.png"; do
    if [[ -f "$cand" ]]; then
      CHAR_PATH="$cand"; break
    fi
  done
fi
if [[ -z "$CHAR_PATH" ]]; then
  echo "[run_benchmark] no character card found, exit"; exit 1
fi
echo "[run_benchmark] char: $CHAR_PATH"
echo "[run_benchmark] model: $MODEL"
echo "[run_benchmark] out: $OUT_DIR"

mkdir -p "$OUT_DIR"/{on,off}

# Scenarios (from docs/benchmark.md)
SCENARIOS=(
  "01_greeting|你好"
  "02_daily|今天天气不错"
  "03_emotional|我今天有点难过"
  "05_plot_advance|我们去看电影吧"
  "06_user_leads|我带你回我老家"
  "07_user_silent|（不说话等 3 轮再回复）"
  "09_user_refuses|我不想去"
  "10_topic_shift|对了，你知道 Python 吗"
  "11_micro_repeat|靠近一点"
  "12_plot_stuck|然后呢"
  "15_blush_loop|看着我"
  "16_whisper_loop|说什么"
  "17_heartbeat_loop|紧张吗"
  "19_lipbite_loop|怎么了"
  "21_character_drift|你是 AI 吗"
)

PYTHON_BIN="$HOME/Projects/sillytavern-telegram-bot/venv/bin/python"

run_one() {
  local mode="$1"      # on | off
  local scenario_id="$2"
  local user_input="$3"
  local out_file="$OUT_DIR/$mode/${scenario_id}.txt"

  REPO_DIR="$REPO_DIR" \
  MODE="$mode" \
  SCENARIO_ID="$scenario_id" \
  USER_INPUT="$user_input" \
  CHAR_PATH="$CHAR_PATH" \
  MODEL="$MODEL" \
  OUT_FILE="$out_file" \
  "$PYTHON_BIN" << 'PYEOF'
import os, sys, json, asyncio, time
sys.path.insert(0, os.environ['REPO_DIR'])

import aiohttp
from character_card import load_character
from pipeline import PromptPipeline, naive_assemble
from director import DirectorConfig

mode = os.environ['MODE']
scenario_id = os.environ['SCENARIO_ID']
user_input = os.environ['USER_INPUT']
model = os.environ['MODEL']
out_file = os.environ['OUT_FILE']

card = load_character(os.environ['CHAR_PATH'])

# Build messages — include user_input as the last history entry so the model
# actually sees it. (In production this is what SessionStore.history returns.)
history = [{"role": "user", "content": user_input}]

if mode == 'off':
    messages = naive_assemble(card, history, user_input)
else:
    director_cfg = DirectorConfig()
    pipeline = PromptPipeline(
        card=card,
        director_cfg=director_cfg,
        char_label="Penelope",
        user_label="Friend",
        reply_language="Chinese",
    )
    messages = pipeline.assemble(history, user_input)

# Use ollama-native /api/chat endpoint (honors think=false; /v1/chat/completions
# on ollama 0.30.10 doesn't honor it and emits all chunks as reasoning)
payload = {
    "model": model,
    "messages": messages,
    "stream": True,
    "think": False,
    "options": {
        "num_ctx": 32768,
        "temperature": 0.95,
        "top_p": 0.92,
        "repeat_penalty": 1.18,
        "think": False,
        "num_predict": 1024,
    },
}

content_lines = []
async def run():
    t0 = time.time()
    async with aiohttp.ClientSession() as session:
        async with session.post("http://localhost:11434/api/chat", json=payload) as resp:
            async for raw in resp.content:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = evt.get('message', {})
                chunk = msg.get('content', '')
                if chunk:
                    content_lines.append(chunk)
                if evt.get('done'):
                    break
    elapsed = time.time() - t0
    content = ''.join(content_lines)

    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(f"# Scenario: {scenario_id} ({mode.upper()})\n")
        f.write(f"# Model: {model}\n")
        f.write(f"# Time: {elapsed:.1f}s\n")
        f.write(f"# Input: {user_input}\n")
        f.write(f"# Messages sent: {len(messages)}\n")
        f.write(f"# Content length: {len(content)}\n")
        f.write("\n=== MESSAGES (truncated) ===\n")
        for i, m in enumerate(messages):
            f.write(f"\n--- msg[{i}] role={m['role']} len={len(m['content'])} ---\n")
            f.write(m['content'][:600])
            if len(m['content']) > 600:
                f.write("\n…(truncated)")
        f.write("\n\n=== MODEL REPLY ===\n")
        f.write(content)
        f.write("\n")
    print(f"  [{mode}] {scenario_id}: {elapsed:.1f}s, content_len={len(content)} → {out_file}")

try:
    asyncio.run(run())
except Exception as e:
    with open(out_file, 'w', encoding='utf-8') as f:
        f.write(f"# ERROR\n{e}\n")
    print(f"  [{mode}] {scenario_id}: ERROR {e}")
PYEOF
}

case "${1:-}" in
  --on)
    mkdir -p "$OUT_DIR/on"
    for s in "${SCENARIOS[@]}"; do
      id="${s%%|*}"; inp="${s#*|}"
      run_one on "$id" "$inp"
    done
    ;;
  --off)
    mkdir -p "$OUT_DIR/off"
    for s in "${SCENARIOS[@]}"; do
      id="${s%%|*}"; inp="${s#*|}"
      run_one off "$id" "$inp"
    done
    ;;
  --compare)
    mkdir -p "$OUT_DIR"/{on,off}
    echo "=== Phase 1 ON ==="
    for s in "${SCENARIOS[@]}"; do
      id="${s%%|*}"; inp="${s#*|}"
      run_one on "$id" "$inp"
    done
    echo "=== Phase 1 OFF ==="
    for s in "${SCENARIOS[@]}"; do
      id="${s%%|*}"; inp="${s#*|}"
      run_one off "$id" "$inp"
    done
    # Generate diff summary
    {
      echo "# AB Test: Phase 1 ON vs OFF"
      echo ""
      echo "- **Model**: \`$MODEL\`"
      echo "- **Date**: $TS"
      echo "- **Char**: Penelope"
      echo "- **Sampling**: temp=0.95, top_p=0.92, repeat_penalty=1.18, num_predict=1024"
      echo "- **Endpoint**: ollama-native /api/chat (think=false)"
      echo ""
      echo "## Outputs"
      echo ""
      echo "- Phase 1 ON:  \`docs/benchmark_results/$TS/on/\`"
      echo "- Phase 1 OFF: \`docs/benchmark_results/$TS/off/\`"
      echo ""
      echo "## 评分表（人工填写）"
      echo ""
      echo "| Scenario | 输入 | ON 推进 | OFF 推进 | ON 一致 | OFF 一致 | ON 重复 | OFF 重复 | ON 沉浸 | OFF 沉浸 |"
      echo "|----------|------|---------|----------|---------|----------|---------|----------|---------|----------|"
      for s in "${SCENARIOS[@]}"; do
        id="${s%%|*}"; inp="${s#*|}"
        echo "| $id | \`$inp\` | /5 | /5 | /5 | /5 | /5 | /5 | /5 | /5 |"
      done
      echo ""
      echo "## 平均分"
      echo ""
      echo "- ON: ___"
      echo "- OFF: ___"
      echo ""
      echo "## 结论"
      echo ""
      echo "Phase 1 PASS / FAIL: __"
      echo ""
      echo "理由：___"
    } > "$OUT_DIR/diff_summary.md"
    echo "[done] diff at $OUT_DIR/diff_summary.md"
    ;;
  *)
    echo "usage: $0 --on | --off | --compare"; exit 1
    ;;
esac