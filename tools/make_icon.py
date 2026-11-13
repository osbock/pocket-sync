# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10"]
# ///
"""Draws the Pocket Recordings icon as a Windows .ico (same design as
make_icon.swift, which macOS uses).

    uv run tools/make_icon.py OUT.ico
"""

import sys

from PIL import Image, ImageDraw

S = 1024
out = sys.argv[1] if len(sys.argv) > 1 else "pocket.ico"

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

# Rounded square with a vertical gradient (top lighter blue, bottom darker).
inset = 100
grad = Image.new("RGBA", (S, S))
top, bottom = (51, 115, 237), (31, 51, 140)
for y in range(S):
    t = min(max((y - inset) / (S - 2 * inset), 0), 1)
    grad.paste(tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)) + (255,),
               (0, y, S, y + 1))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle((inset, inset, S - inset, S - inset), 185, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
# Waveform bars (y grows downward here, unlike AppKit).
heights = [0.18, 0.34, 0.55, 0.80, 0.62, 0.95, 0.70, 0.45, 0.66, 0.38, 0.22]
bar_w, gap = 38, 22
x = (S - (len(heights) * bar_w + (len(heights) - 1) * gap)) / 2
mid = S / 2 - 20
for h in heights:
    bh = h * 430
    d.rounded_rectangle((x, mid - bh / 2, x + bar_w, mid + bh / 2), bar_w / 2, fill="white")
    x += bar_w + gap

# The "pocket" lip along the bottom, semi-transparent white.
lip = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(lip).rounded_rectangle((250, S - 279, 774, S - 215), 32, fill=(255, 255, 255, 56))
img = Image.alpha_composite(img, lip)

img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"Wrote {out}")
