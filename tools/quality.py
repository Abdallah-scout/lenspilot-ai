"""Image-quality estimation.

INTEGRATION BASELINE — owner: Khalil (image-quality model integration).

KonCept512 is NOT available in this repository, so the default estimator is
a transparent pixel heuristic (sharpness, exposure, contrast). Its output is
always labelled with ``estimator`` so nobody mistakes it for KonCept512.
To plug the real model, implement ``QualityEstimator.assess`` and pass it to
agent.integration.build_services(quality=...).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageFilter, ImageStat


class QualityEstimator(Protocol):
    name: str

    def assess(self, image_path: Path) -> dict: ...


class HeuristicQualityEstimator:
    """Deterministic 0-100 score from measurable image properties."""

    name = "heuristic-sharpness-exposure-v1 (NOT KonCept512)"
    PUBLISH_THRESHOLD = 60.0

    def assess(self, image_path: Path) -> dict:
        with Image.open(image_path) as img:
            gray = img.convert("L")
            gray.thumbnail((768, 768))
            edges = gray.filter(ImageFilter.FIND_EDGES)
            sharp_var = ImageStat.Stat(edges).var[0]
            stat = ImageStat.Stat(gray)
            mean, std = stat.mean[0], stat.stddev[0]
        # Normalise each component to 0..1 (constants calibrated on fixtures).
        sharpness = min(1.0, math.log1p(sharp_var) / math.log1p(1600))
        exposure = max(0.0, 1.0 - abs(mean - 118) / 80)
        contrast = min(1.0, std / 64)
        score = round(100 * (0.5 * sharpness + 0.3 * exposure + 0.2 * contrast), 1)
        return {
            "score": score,
            "components": {
                "sharpness": round(sharpness, 3),
                "exposure": round(exposure, 3),
                "contrast": round(contrast, 3),
            },
            "recommendation": "publish-ready" if score >= self.PUBLISH_THRESHOLD else "not recommended",
            "estimator": self.name,
        }
