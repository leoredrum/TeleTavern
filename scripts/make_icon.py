#!/usr/bin/env python3
"""Generate app/icon.icns (and app/icon.png) for TelegramTavern.

Design: rounded dark-amber gradient tile, a foamy tavern mug, and a speech
bubble — drawn with Pillow primitives so no external assets are needed.
Run:  ./venv/bin/python scripts/make_icon.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = ROOT / "app" / "icon.png"
OUT_ICNS = ROOT / "app" / "icon.icns"
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_icon() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # background: vertical gradient inside a rounded square (macOS style ~22% radius)
    bg = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGB", (1, S))
    top, bottom = (58, 34, 22), (26, 18, 14)
    for y in range(S):
        grad.putpixel((0, y), lerp(top, bottom, y / S))
    grad = grad.resize((S, S))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((60, 60, S - 60, S - 60), radius=225, fill=255)
    bg.paste(grad, (0, 0), mask)
    img.alpha_composite(bg)

    d = ImageDraw.Draw(img)
    # warm glow behind the mug
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((250, 300, 780, 830), fill=(255, 170, 60, 110))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    img.alpha_composite(glow)
    d = ImageDraw.Draw(img)

    # mug body
    amber, amber_dark, foam = (247, 164, 44), (196, 118, 24), (255, 248, 232)
    body = (330, 400, 640, 820)
    d.rounded_rectangle(body, radius=48, fill=amber, outline=amber_dark, width=10)
    # beer shading stripes
    for x in range(372, 600, 56):
        d.rounded_rectangle((x, 440, x + 22, 790), radius=11, fill=(255, 190, 80))
    # handle
    d.rounded_rectangle((620, 480, 760, 720), radius=70, outline=amber_dark, width=42)
    d.rounded_rectangle((640, 500, 740, 700), radius=50, fill=(0, 0, 0, 0))
    # foam
    d.rounded_rectangle((300, 330, 670, 440), radius=55, fill=foam)
    for cx, cy, r in ((330, 340, 62), (420, 300, 78), (520, 310, 72), (610, 335, 62), (660, 390, 48)):
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=foam)

    # speech bubble (top-right) with three dots
    bub = (560, 150, 900, 380)
    d.rounded_rectangle(bub, radius=70, fill=(255, 255, 255))
    d.polygon([(640, 370), (690, 370), (610, 440)], fill=(255, 255, 255))
    for i, cx in enumerate((650, 730, 810)):
        d.ellipse((cx - 24, 241, cx + 24, 289), fill=(64, 140, 255) if i == 1 else (120, 120, 130))
    return img


def build_icns(png: Path, icns: Path) -> None:
    if not shutil.which("iconutil") or not shutil.which("sips"):
        print("iconutil/sips not available; wrote PNG only")
        return
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)


if __name__ == "__main__":
    OUT_PNG.parent.mkdir(exist_ok=True)
    draw_icon().save(OUT_PNG)
    build_icns(OUT_PNG, OUT_ICNS)
    print("wrote", OUT_PNG, "and", OUT_ICNS if OUT_ICNS.exists() else "(no icns)")
    sys.exit(0)
