"""Tile screenshots into one image, optionally as before/after rows.

    contact_sheet.py out.png dirA [dirB] [--only lobby,people] [--width 300]

One row per directory, columns in --only order (default: every png in dirA)."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument("out")
ap.add_argument("dirs", nargs="+")
ap.add_argument("--only")
ap.add_argument("--width", type=int, default=300)
a = ap.parse_args()

dirs = [Path(d) for d in a.dirs]
names = a.only.split(",") if a.only else sorted(p.stem for p in dirs[0].glob("*.png"))
label_h, gap = 28, 12
tiles = [[Image.open(d / f"{n}.png").convert("RGB") for n in names] for d in dirs]
h = max(int(t.height * a.width / t.width) for row in tiles for t in row)
sheet = Image.new("RGB", (len(names) * (a.width + gap) + gap, len(dirs) * (h + label_h + gap) + gap), "white")
draw = ImageDraw.Draw(sheet)
for r, row in enumerate(tiles):
    y = gap + r * (h + label_h + gap)
    for c, img in enumerate(row):
        x = gap + c * (a.width + gap)
        draw.text((x, y + 6), f"{dirs[r].name} / {names[c]}", fill="black")
        sheet.paste(img.resize((a.width, int(img.height * a.width / img.width))), (x, y + label_h))
sheet.save(a.out)
