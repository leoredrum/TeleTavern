#!/bin/bash
# Back up SillyTavern state for V2.
#
# Includes:
#   - SillyTavern data (characters, chats, worlds, settings) — does NOT include
#     secrets.json (empty anyway) or cookie-secret.txt (regenerated on install).
#   - V2 connector config (.env.example, scripts, settings.json templates) — does
#     NOT include .env files (real tokens).
#   - Models manifest (no blobs).
#
# Excludes:
#   - All .env files.
#   - ST's sillytavern.log (no private chat content anyway; safe to exclude).
#   - Ollama model blobs (huge; not portable).
#
# Output: ~/Documents/SillyTavern/backups/<timestamp>.tgz
set -euo pipefail

TS="$(date +%Y%m%d_%H%M%S)"
OUT="${HOME}/Documents/SillyTavern/backups/${TS}.tgz"
SRC_ST="${HOME}/Documents/SillyTavern/SillyTavern/data"
SRC_V2="${HOME}/Documents/SillyTavern/connector"

mkdir -p "$(dirname "$OUT")"

# Sanity: refuse to back up secrets.json if it has data
if [ -s "${SRC_ST}/default-user/secrets.json" ]; then
    if ! grep -q '^{}$' "${SRC_ST}/default-user/secrets.json"; then
        echo "ABORT: secrets.json is non-empty; back up secrets separately per SECRET_HANDLING_PLAYBOOK.md"
        exit 1
    fi
fi

# Build exclude list
EXCLUDES=(
    "--exclude=*/.env"
    "--exclude=*/__pycache__"
    "--exclude=*.pyc"
    "--exclude=venv"
    "--exclude=logs/*.log"
    "--exclude=logs/*.pid"
    "--exclude=tests/screenshots"
    "--exclude=tests/headless.*"
    "--exclude=node_modules"
)

tar -czf "$OUT" "${EXCLUDES[@]}" \
    -C "${SRC_ST}/.." "$(basename "$SRC_ST")" \
    -C "${SRC_V2}/.." "$(basename "$SRC_V2")"

echo "wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Auto-prune: keep the 10 most recent
ls -1t "${HOME}/Documents/SillyTavern/backups/"*.tgz 2>/dev/null | tail -n +11 | while read -r old; do
    rm -f "$old"
done