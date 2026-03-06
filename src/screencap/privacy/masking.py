"""Structural masking for privacy v3 phase 4.

Applies coarse, context-driven masks to screenshots so communication
surfaces are protected without relying on OCR/NER.

Owns:
- support matrix (ContextClass -> mask strategy)
- mask geometry generation
- image mask rendering via Pillow
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from screencap.privacy.policy import ContextClass


class MaskStrategy(Enum):
    """How aggressively to mask a surface."""

    FULL_WINDOW = "full_window"
    PANE = "pane"


# ---------------------------------------------------------------------------
# Support matrix: which surfaces get which mask treatment
# ---------------------------------------------------------------------------

# Conservative first pass: all communication surfaces use FULL_WINDOW.
# Pane-level masking is infrastructure-ready but only activates when
# MASK_REGION actions are enabled (shared mode, currently deferred).
_SURFACE_STRATEGY: dict[ContextClass, MaskStrategy] = {
    ContextClass.EMAIL: MaskStrategy.FULL_WINDOW,
    ContextClass.CHAT: MaskStrategy.FULL_WINDOW,
    ContextClass.CALENDAR: MaskStrategy.FULL_WINDOW,
    ContextClass.VIDEO_CALL: MaskStrategy.FULL_WINDOW,
    ContextClass.BROWSER_UNVERIFIED: MaskStrategy.FULL_WINDOW,
    ContextClass.UNKNOWN: MaskStrategy.FULL_WINDOW,
    # PASSWORD_MANAGER / BANKING -> EXCLUDE (no masking, file deleted)
    # CODE_EDITOR_TERMINAL / ADMIN_CONSOLE -> OCR_FALLBACK (no masking)
}


def get_mask_strategy(context_class: ContextClass) -> MaskStrategy | None:
    """Look up the mask strategy for a context class.

    Returns None for contexts that don't use masking (EXCLUDE or OCR paths).
    """
    return _SURFACE_STRATEGY.get(context_class)


# ---------------------------------------------------------------------------
# Mask geometry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaskRegion:
    """A rectangular region to mask, in pixel coordinates.

    Origin is top-left. Coordinates are absolute pixels within the image.
    """

    x: int
    y: int
    width: int
    height: int
    label: str = ""  # human-readable context, e.g. "email", "chat"


def full_window_geometry(
    image_width: int, image_height: int, context_class: ContextClass
) -> list[MaskRegion]:
    """Generate a single region covering the entire image."""
    return [
        MaskRegion(
            x=0,
            y=0,
            width=image_width,
            height=image_height,
            label=context_class.value,
        )
    ]


# ---------------------------------------------------------------------------
# Pane geometry providers (infrastructure for future MASK_REGION)
# ---------------------------------------------------------------------------

# Initial app/site support matrix for known pane layouts.
# Keys are (ContextClass, app_hint) where app_hint is a bundle ID or domain.
# Values are callables that produce MaskRegion lists given image dimensions.
#
# These are conservative estimates. For the first pass, all communication
# surfaces fall back to full-window masking anyway. This mapping exists
# so shared-mode pane masking has a starting point when enabled.

_KNOWN_PANE_LAYOUTS: dict[
    tuple[ContextClass, str],
    type[None],  # placeholder — will be geometry callables
] = {}


def pane_geometry(
    image_width: int,
    image_height: int,
    context_class: ContextClass,
    app_hint: str = "",
) -> list[MaskRegion]:
    """Generate pane-level mask regions for a known surface.

    Falls back to full-window if no pane layout is known for the
    (context_class, app_hint) pair. This is the conservative default.
    """
    # Future: look up _KNOWN_PANE_LAYOUTS[(context_class, app_hint)]
    # and return fine-grained regions. For now, always fall back.
    return full_window_geometry(image_width, image_height, context_class)


# ---------------------------------------------------------------------------
# Image mask rendering
# ---------------------------------------------------------------------------

# Visual treatment constants
_MASK_COLOR = (30, 30, 30)  # near-black
_MASK_OPACITY = 230  # 0-255, high = more opaque
_LABEL_COLOR = (180, 180, 180)  # light gray text


def apply_mask(image_path: Path, regions: list[MaskRegion]) -> None:
    """Apply visual masks to a screenshot image file, in-place.

    Renders a semi-opaque dark overlay for each region with a small
    text label so users know the omission was intentional.

    Requires Pillow (available in record deps).
    """
    from PIL import Image, ImageDraw

    img = Image.open(image_path).convert("RGBA")

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for region in regions:
        x1, y1 = region.x, region.y
        x2 = min(region.x + region.width, img.width)
        y2 = min(region.y + region.height, img.height)

        draw.rectangle(
            [x1, y1, x2, y2],
            fill=(*_MASK_COLOR, _MASK_OPACITY),
        )

        # Draw label centered in the region
        if region.label:
            label = f"Masked: {region.label}"
            bbox = draw.textbbox((0, 0), label)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            text_x = x1 + (x2 - x1 - text_w) // 2
            text_y = y1 + (y2 - y1 - text_h) // 2
            draw.text(
                (text_x, text_y),
                label,
                fill=(*_LABEL_COLOR, 255),
            )

    result = Image.alpha_composite(img, overlay)
    # Save as JPEG (screenshots are .jpg) — flatten alpha
    result = result.convert("RGB")
    result.save(image_path, "JPEG", quality=85)


def mask_screenshot(
    image_path: Path,
    context_class: ContextClass,
    strategy: MaskStrategy | None = None,
    app_hint: str = "",
) -> bool:
    """Apply structural masking to a screenshot based on context.

    Returns True if masking was applied, False if no masking strategy
    exists for this context (caller should handle via other means).
    """
    if strategy is None:
        strategy = get_mask_strategy(context_class)
    if strategy is None:
        return False

    from PIL import Image

    with Image.open(image_path) as img:
        w, h = img.size

    if strategy == MaskStrategy.PANE:
        regions = pane_geometry(w, h, context_class, app_hint)
    else:
        regions = full_window_geometry(w, h, context_class)

    apply_mask(image_path, regions)
    return True
