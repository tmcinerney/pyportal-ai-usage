#!/usr/bin/env python3
"""Render exact facsimiles of the three usage-dashboard screens.

Replicates projects/usage-dashboard/code.py's layout and colors at native
320x240 (then upscales) so the hero generator receives screens that are
correct by construction: real accent colors, segmented bars whose fill
always equals the printed percentage, and the real row labels.

Usage: python scripts/render-screens.py [outdir]
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 320, 240
BLACK = (0, 0, 0)
WHITE = (0xFF, 0xFF, 0xFF)
ORANGE = (0xCC, 0x6B, 0x3D)
TEAL = (0x10, 0xA3, 0x7F)
BLUE = (0x3B, 0x82, 0xF6)
SUB = (0x66, 0x66, 0x88)
DIM = (0x33, 0x33, 0x55)
GREEN = (0x22, 0xC5, 0x5E)
AMBER = (0xF5, 0x9E, 0x0B)
RED = (0xEF, 0x44, 0x44)

BL = 6          # left border x
Y_BTOP = 2
Y_BSEP = 50
Y_BBOT = H - 2
ROW_SLOT = 42
Y_ROW1 = Y_BSEP + 22
TX_C = BL + 12
RIGHT = W - 2 - 12

BARS = 16
CELL_W = 10
BAR_GAP = 2
BAR_H = 12

try:
    FONT_PATH = str(Path(__file__).resolve().parents[1] / "fonts" / "6x10.bdf")
    FONT = ImageFont.load(FONT_PATH)
except Exception:
    FONT = ImageFont.load_default()


def bar_color(pct):
    if pct is None:
        return DIM
    if pct < 50:
        return GREEN
    if pct < 80:
        return AMBER
    return RED


def draw_frame(d, title_main, title_sub, color):
    for (x, y, w, h) in [
        (BL, Y_BTOP, W - BL, 2), (BL, Y_BBOT, W - BL, 2),
        (BL, Y_BTOP, 2, Y_BBOT - Y_BTOP + 2), (W - 2, Y_BTOP, 2, Y_BBOT - Y_BTOP + 2),
        (BL, Y_BSEP, W - BL, 2),
    ]:
        d.rectangle([x, y, x + w - 1, y + h - 1], fill=color)
    d.text((TX_C, 14), title_main, font=FONT, fill=color)
    d.text((TX_C + (len(title_main) + 1) * 18, 14), title_sub, font=FONT, fill=SUB)
    age = "2m ago"
    d.text((RIGHT - len(age) * 6, 14), age, font=FONT, fill=SUB)


def draw_bar_row(d, y, name, pct, reset_text, col, sub_text=None):
    d.text((TX_C, y), name, font=FONT, fill=WHITE)
    bx = TX_C + 4 * 12 + 4
    filled = 0 if pct is None else max(0, min(BARS, BARS * int(pct) // 100))
    for i in range(BARS):
        cx = bx + i * (CELL_W + BAR_GAP)
        if i < filled:
            d.rectangle([cx, y - 8, cx + CELL_W - 1, y - 8 + BAR_H - 1], fill=col)
        else:
            d.rectangle([cx, y - 8, cx + CELL_W - 1, y - 8 + BAR_H - 1], fill=DIM)
    pct_s = (" --%" if pct is None else (" " if pct < 10 else "") + str(int(pct)) + "%")
    d.text((RIGHT - 3 * 12, y), pct_s, font=FONT, fill=SUB if pct is None else WHITE)
    detail = (sub_text or "") + ((" " if sub_text and reset_text else "") + reset_text if reset_text else "")
    if detail:
        d.text((RIGHT - len(detail) * 6, y + 20), detail, font=FONT,
               fill=GREEN if sub_text == "ACTIVE" else AMBER)


def claude_screen():
    im = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(im)
    draw_frame(d, "CLAUDE", "usage", ORANGE)
    rows = [("5h ", 34, "r4h"), ("7d ", 52, "r2d"), ("fbl", 28, "r6d"), ("xtr", 31, "$623/$200k")]
    y = Y_ROW1
    for name, pct, sub in rows:
        draw_bar_row(d, y, name, pct, "", bar_color(pct), sub if name == "xtr" else None)
        y += ROW_SLOT
    return im


def codex_screen():
    im = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(im)
    draw_frame(d, "CODEX", "usage", TEAL)
    draw_bar_row(d, Y_ROW1, "5h ", 18, "r3h", bar_color(18))
    # text row for reset credits
    d.text((TX_C, Y_ROW1 + ROW_SLOT), "rst", font=FONT, fill=WHITE)
    d.text((TX_C + 4 * 12, Y_ROW1 + ROW_SLOT), "60 banked", font=FONT, fill=WHITE)
    d.text((RIGHT - 14 * 6, Y_ROW1 + ROW_SLOT + 20), "60 applicable now", font=FONT, fill=AMBER)
    draw_bar_row(d, Y_ROW1 + 2 * ROW_SLOT, "7d ", 41, "r4d", bar_color(41))
    draw_bar_row(d, Y_ROW1 + 3 * ROW_SLOT, "lim", 0, "", GREEN, "ACTIVE")
    return im


def opencode_screen():
    im = Image.new("RGB", (W, H), BLACK)
    d = ImageDraw.Draw(im)
    draw_frame(d, "OPENCODE", "go", BLUE)
    rows = [("5h ", 9, "r4h"), ("7d ", 26, "r5d"), ("mo ", 31, "r20d")]
    y = Y_ROW1
    for name, pct, sub in rows:
        draw_bar_row(d, y, name, pct, sub, bar_color(pct))
        y += ROW_SLOT
    draw_bar_row(d, Y_ROW1 + 3 * ROW_SLOT, "lim", 0, "", GREEN, "ACTIVE")
    return im


def main():
    outdir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/screens")
    outdir.mkdir(parents=True, exist_ok=True)
    for name, fn in [("claude", claude_screen), ("codex", codex_screen), ("opencode", opencode_screen)]:
        im = fn()
        big = im.resize((W * 4, H * 4), Image.NEAREST)
        big.save(outdir / f"screen_{name}.png")
        print(f"wrote {outdir}/screen_{name}.png")


if __name__ == "__main__":
    main()
