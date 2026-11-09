"""Generate the SYNTHETIC demo photo fixtures (deterministic).

INTEGRATION BASELINE — owner: Khalil (dataset / sandbox).
Written by Abdallah only to unblock agent integration. These images are
procedurally generated stand-ins, NOT KonIQ-10k photographs. Replace them by
dropping real images into ``sandbox/fixtures/images`` and editing
``catalog.json`` (same schema).

Different blur / exposure settings are applied on purpose so that the
quality heuristic produces a realistic spread of scores.

Run:  python -m sandbox.make_fixtures
"""

from __future__ import annotations

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

FIXTURES = Path(__file__).resolve().parent / "fixtures"
IMAGES = FIXTURES / "images"
SIZE = (768, 512)

# photo_id, palette (bg top, bg bottom, accent), blur radius, brightness factor
RENDER_PLAN = {
    "rest_001": ((40, 22, 14), (120, 70, 30), (250, 200, 120), 0.0, 1.00),
    "rest_002": ((25, 25, 30), (90, 90, 100), (240, 120, 60), 0.0, 1.05),
    "rest_003": ((200, 120, 60), (90, 50, 40), (255, 230, 170), 1.2, 1.00),
    "rest_004": ((30, 20, 40), (130, 90, 120), (255, 220, 230), 0.0, 1.00),
    "rest_005": ((60, 40, 30), (110, 80, 60), (230, 180, 140), 6.0, 0.55),
    "rest_006": ((50, 15, 20), (110, 50, 50), (200, 160, 90), 0.4, 0.95),
    "int_001": ((200, 200, 195), (120, 115, 105), (60, 70, 80), 0.0, 1.00),
    "int_002": ((150, 130, 100), (60, 50, 40), (230, 210, 160), 0.6, 1.00),
    "int_003": ((220, 220, 225), (160, 160, 170), (40, 90, 120), 0.0, 1.10),
    "int_004": ((110, 90, 70), (50, 40, 30), (200, 170, 110), 4.0, 2.40),
}


def _render(photo_id: str, plan: tuple) -> Image.Image:
    top, bottom, accent, blur, brightness = plan
    rng = random.Random(photo_id)  # deterministic per photo
    img = Image.new("RGB", SIZE)
    draw = ImageDraw.Draw(img)
    w, h = SIZE
    for y in range(h):  # vertical gradient background
        t = y / (h - 1)
        draw.line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    for _ in range(14):  # "furniture" / "tableware" blocks
        x0, y0 = rng.randint(0, w - 120), rng.randint(h // 3, h - 60)
        x1, y1 = x0 + rng.randint(40, 200), y0 + rng.randint(20, 120)
        shade = tuple(max(0, min(255, c + rng.randint(-60, 40))) for c in accent)
        draw.rectangle([x0, y0, x1, y1], fill=shade, outline=(15, 15, 15), width=2)
    for _ in range(9):  # "lights" / "plates"
        cx, cy, r = rng.randint(0, w), rng.randint(0, h), rng.randint(8, 45)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=accent, outline=(255, 255, 255), width=2)
    for i in range(0, w, 48):  # fine texture lines give the sharpness metric something to measure
        draw.line([(i, 0), (i + 30, h // 3)], fill=(min(255, top[0] + 40),) * 3, width=1)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    return ImageEnhance.Brightness(img).enhance(brightness)


def main() -> None:
    IMAGES.mkdir(parents=True, exist_ok=True)
    catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    for record in catalog["photos"]:
        plan = RENDER_PLAN[record["photo_id"]]
        _render(record["photo_id"], plan).save(IMAGES / record["file"], quality=90)
    print(f"Wrote {len(catalog['photos'])} synthetic images to {IMAGES}")


if __name__ == "__main__":
    main()
