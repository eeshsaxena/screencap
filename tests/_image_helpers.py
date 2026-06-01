"""Shared image test helpers."""

from __future__ import annotations

from pathlib import Path


def _avg_brightness(path: Path) -> float:
    """Return average pixel brightness (0-255) of a JPEG."""
    from PIL import Image

    img = Image.open(path).convert("L")  # grayscale
    pixels = list(img.getdata())
    return sum(pixels) / len(pixels)
