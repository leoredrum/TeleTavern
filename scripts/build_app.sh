#!/usr/bin/env bash
# Build TelegramTavern.app (menu-bar app bundling the tavern engine).
# Usage: ./scripts/build_app.sh            → dist/TelegramTavern.app
#        ./scripts/build_app.sh --install  → also copy to /Applications
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x venv/bin/pyinstaller ] || { echo "venv missing pyinstaller: ./venv/bin/pip install pyinstaller rumps"; exit 1; }

rm -rf build/TelegramTavern dist/TelegramTavern dist/TelegramTavern.app
./venv/bin/pyinstaller app/TelegramTavern.spec --noconfirm --log-level WARN

APP=dist/TelegramTavern.app
[ -d "$APP" ] || { echo "build failed: $APP not found"; exit 1; }
# ad-hoc sign so Gatekeeper lets a local build run (replace with a Developer ID for distribution)
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
du -sh "$APP" | awk '{print "built " $2 " (" $1 ")"}'

if [ "${1:-}" = "--install" ]; then
    rm -rf /Applications/TelegramTavern.app
    cp -R "$APP" /Applications/
    # make Launch Services / Dock pick up the (new) icon instead of a cached generic one
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/TelegramTavern.app >/dev/null 2>&1 || true
    touch /Applications/TelegramTavern.app
    killall Dock >/dev/null 2>&1 || true
    echo "installed /Applications/TelegramTavern.app"
fi
