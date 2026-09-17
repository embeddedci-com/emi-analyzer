"""Draw the toolbar icons: a board with a trace on it, radiating.

Run with any Python that has Pillow:  python scripts/make_icons.py
Writes icons/emi-<size>[-dark].png. The PNGs are committed; this is here so they can be
redrawn rather than edited by hand.

Drawn at 8x and scaled down, which is cheaper than antialiasing each shape and gives the
same result at the two sizes KiCad asks for.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "icons"
SCALE = 8


def draw(size: int, fg: str, accent: str) -> Image.Image:
    n = size * SCALE
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = n / 24.0  # the icon is designed on a 24x24 grid

    def xy(*points):
        return [(x * s, y * s) for x, y in points]

    # The board: an outline with two pads on it.
    d.rounded_rectangle(
        xy((1.5, 5.5), (16.5, 20.5))[0] + xy((1.5, 5.5), (16.5, 20.5))[1],
        radius=1.2 * s,
        outline=fg,
        width=int(1.3 * s),
    )
    for cx, cy in ((4.5, 17.5), (13.5, 8.5)):
        d.ellipse(xy((cx - 1.1, cy - 1.1), (cx + 1.1, cy + 1.1)), fill=fg)

    # The trace between them, with the corner every emission problem starts at.
    d.line(xy((4.5, 17.5), (4.5, 12.0), (9.5, 12.0), (9.5, 8.5), (13.5, 8.5)),
           fill=accent, width=int(1.6 * s), joint="curve")

    # What it radiates: arcs leaving the board at the top right.
    for r in (5.0, 8.0, 11.0):
        box = xy((16.5 - r, 8.5 - r), (16.5 + r, 8.5 + r))
        d.arc(box[0] + box[1], start=-55, end=15, fill=accent, width=int(1.3 * s))

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    light = ("#3a3a3a", "#0a7d5a")
    dark = ("#e6e6e6", "#37d39b")
    for size in (24, 48):
        draw(size, *light).save(OUT / f"emi-{size}.png")
        draw(size, *dark).save(OUT / f"emi-{size}-dark.png")
    # The Plugin and Content Manager's icon: 64x64, light only.
    draw(64, *light).save(OUT / "emi-64.png")
    print(f"wrote {OUT}/emi-*.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
