#!/usr/bin/env python3
"""Regenerate docs/mockups/pyportal-ai-usage-hero.png.

Renders the three provider screens (Claude, Codex, OpenCode Go) as a simple
facsimile of the device UI, each in a PyPortal-style enclosure, side by side.
Run from the repo root:

    python scripts/render-hero.py

The output is a mockup, not a device screenshot; it exists so the README hero
shows all three providers.
"""

from pathlib import Path

from PIL import Image, ImageDraw

# Screen content per provider: title, sub, and bar rows (label, pct, color).
NAVY = (16, 22, 38)
BLUE = (60, 130, 255)
TEAL = (42, 170, 138)
AMBER = (220, 159, 40)
GREEN = (56, 142, 60)
DIM = (90, 98, 116)
WHITE = (235, 238, 244)

SCREENS = [
    {
        "title": "CLAUDE", "sub": "code", "color": BLUE,
        "rows": [("5h", 34), ("7d", 52), ("mo", 68)],
        "status": "ACTIVE",
    },
    {
        "title": "CODEX", "sub": "gpt", "color": TEAL,
        "rows": [("5h", 18), ("7d", 41), ("rst", 60)],
        "status": "ACTIVE",
    },
    {
        "title": "OPENCODE", "sub": "go", "color": AMBER,
        "rows": [("5h", 9), ("7d", 26), ("mo", 31)],
        "status": "ACTIVE",
    },
]

# Device geometry (mockup scale: 4x the real 320x240 screen)
SCALE = 4
SCR_W, SCR_H = 320 * SCALE, 240 * SCALE
BEZEL = 26 * SCALE
STAND_H = 40 * SCALE


def draw_screen(base, spec):
    d = ImageDraw.Draw(base)
    w, h = base.size
    d.rectangle([0, 0, w, h], fill=NAVY)

    # Header: title left, sub in parens, age right
    d.text((16, 14), spec["title"], fill=WHITE)
    sub_x = 16 + 11 * (len(spec["title"]) + 1)
    d.text((sub_x, 14), spec["sub"], fill=DIM)
    d.text((w - 120, 14), "2m ago", fill=DIM)
    d.line([0, 46, w, 46], fill=(40, 48, 72), width=2)

    # Bars
    bar_x, bar_w = 60, w - 60 - 40
    y = 80
    for label, pct in spec["rows"]:
        d.text((16, y + 6), label, fill=WHITE)
        d.rectangle([bar_x, y, bar_x + bar_w, y + 34], fill=(30, 38, 60), width=2)
        fill_w = int(bar_w * pct / 100)
        d.rectangle([bar_x, y, bar_x + fill_w, y + 34], fill=spec["color"])
        d.text((bar_x + bar_w + 14, y + 6), f"{pct}%", fill=WHITE)
        y += 52

    # Status bar
    d.rectangle([16, h - 58, bar_x + bar_w, h - 24], fill=(30, 38, 60), width=2)
    fill_w = int((bar_x + bar_w - 16) * (0 if spec["status"] == "ACTIVE" else 100) / 100)
    d.rectangle([16, h - 58, 16 + fill_w, h - 24], fill=GREEN)
    d.text((16 + (bar_x + bar_w - 16) // 2 - 36, h - 50), spec["status"], fill=WHITE)


def draw_device(spec):
    scr = Image.new("RGB", (SCR_W, SCR_H))
    draw_screen(scr, spec)

    frame_w = SCR_W + BEZEL * 2
    frame_h = SCR_H + BEZEL * 2 + STAND_H
    device = Image.new("RGB", (frame_w, frame_h), (244, 244, 242))
    dd = ImageDraw.Draw(device)
    dd.rectangle([BEZEL // 2, BEZEL // 2, frame_w - BEZEL // 2, SCR_H + BEZEL * 3 // 2],
                 fill=(228, 228, 226), outline=(196, 198, 200), width=3)
    device.paste(scr, (BEZEL, BEZEL))
    # stand
    dd.polygon([
        (frame_w // 2 - 130, frame_h - STAND_H - 6),
        (frame_w // 2 + 130, frame_h - STAND_H - 6),
        (frame_w // 2 + 90, frame_h - 8),
        (frame_w // 2 - 90, frame_h - 8),
    ], fill=(216, 217, 215), outline=(196, 198, 200))
    return device


def main():
    devices = [draw_device(spec) for spec in SCREENS]
    gap = 70
    total_w = sum(d.width for d in devices) + gap * (len(devices) - 1)
    total_h = max(d.height for d in devices) + 60
    hero = Image.new("RGB", (total_w, total_h), (250, 250, 249))
    x = 0
    for d in devices:
        hero.paste(d, (x, 30))
        x += d.width + gap
    out = Path(__file__).resolve().parents[1] / "docs/mockups/pyportal-ai-usage-hero.png"
    hero.save(out)
    print(f"Wrote {out} ({hero.width}x{hero.height})")


if __name__ == "__main__":
    main()
