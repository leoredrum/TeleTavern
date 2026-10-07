# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — cross-platform.
#   macOS:   ./venv/bin/pyinstaller app/TelegramTavern.spec --noconfirm  → dist/TelegramTavern.app
#   Windows: pyinstaller app/TelegramTavern.spec --noconfirm             → dist/TelegramTavern/TelegramTavern.exe
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
IS_MAC = sys.platform == "darwin"

wv_datas, wv_bins, wv_hidden = collect_all("webview")
hidden = (wv_hidden + collect_submodules("tavern") + collect_submodules("telegram") + collect_submodules("aiohttp")
          + ["yaml", "dotenv", "PIL", "PIL.Image", "PIL.PngImagePlugin", "character_card",
             "pipeline", "prompt_item", "director", "story_engine", "ollama_client", "db", "config"])
if IS_MAC:
    hidden += ["rumps"]  # menubar.py shell, macOS only

ICON = os.path.join(ROOT, "app", "icon.icns" if IS_MAC else "icon.ico")

a = Analysis(
    [os.path.join(ROOT, "app", "window.py")],
    pathex=[ROOT],
    binaries=wv_bins,
    datas=wv_datas + [(os.path.join(ROOT, "app", "ui.html"), ".")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="TelegramTavern",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TelegramTavern")
if IS_MAC:
    app = BUNDLE(
        coll,
        name="TelegramTavern.app",
        icon=ICON,
        bundle_identifier="com.leoredrum.telegramtavern",
        info_plist={
            "LSUIElement": False,                # normal window app with Dock icon
            "CFBundleShortVersionString": "3.0.2",
            "CFBundleName": "TelegramTavern",
            "CFBundleDisplayName": "Telegram Tavern",
            "CFBundleIconFile": "icon.icns",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
        },
    )
