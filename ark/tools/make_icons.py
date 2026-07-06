#!/usr/bin/env python3
"""Generate the ARK web-companion PWA icons (a clean 'archive vault' glyph).

Run:  python tools/make_icons.py     # writes ark/webapp/icon-{192,512}.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (14, 17, 22)          # --bg
ACCENT = (76, 141, 255)    # --accent
FG = (231, 236, 243)       # --fg
OUT = Path(__file__).resolve().parent.parent / "ark" / "webapp"


def _icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)
    s = size
    # rounded accent frame
    pad = s * 0.14
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.14,
                        outline=ACCENT, width=max(2, int(s * 0.03)))
    # vault "shelf" line
    y = s * 0.62
    d.line([s * 0.30, y, s * 0.70, y], fill=FG, width=max(2, int(s * 0.03)))
    # an upward arrow "into the vault" (send / archive)
    cx = s * 0.5
    d.line([cx, s * 0.30, cx, y], fill=FG, width=max(2, int(s * 0.035)))
    head = s * 0.07
    d.line([cx, s * 0.30, cx - head, s * 0.30 + head], fill=FG, width=max(2, int(s * 0.035)))
    d.line([cx, s * 0.30, cx + head, s * 0.30 + head], fill=FG, width=max(2, int(s * 0.035)))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        _icon(size).save(OUT / f"icon-{size}.png")
        print(f"wrote {OUT / f'icon-{size}.png'}")


if __name__ == "__main__":
    main()
