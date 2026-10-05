# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — build with:  ./venv/bin/pyinstaller app/TelegramTavern.spec --noconfirm
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

hidden = (collect_submodules("tavern") + collect_submodules("telegram") + collect_submodules("aiohttp")
          + ["yaml", "dotenv", "PIL", "PIL.Image", "PIL.PngImagePlugin", "rumps", "character_card",
             "pipeline", "prompt_item", "director", "story_engine", "ollama_client", "db", "config"])

a = Analysis(
    [os.path.join(ROOT, "app", "menubar.py")],
    pathex=[ROOT],
    binaries=[],
    datas=[],
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
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="TelegramTavern")
app = BUNDLE(
    coll,
    name="TelegramTavern.app",
    icon=None,
    bundle_identifier="com.leoredrum.telegramtavern",
    info_plist={
        "LSUIElement": True,                 # menu-bar only, no Dock icon
        "CFBundleShortVersionString": "3.0.0",
        "CFBundleName": "TelegramTavern",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "13.0",
    },
)
