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

from screencap.privacy.actions import KEYSTROKE_NULL_ACTIONS
from screencap.privacy.policy import ContextClass, FrameMetadata


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
# Per-window selective masking from stored geometry
# ---------------------------------------------------------------------------


def window_regions_from_geometry(
    window_list: list[dict],
    image_width: int,
    image_height: int,
    pixel_ratio: float,
    classifier,
    evaluator,
    display_origin: tuple[float, float] = (0.0, 0.0),
) -> list[MaskRegion]:
    """Generate mask regions from per-screenshot window geometry.

    Evaluates each window's bundle_id against the privacy policy and
    returns a ``MaskRegion`` for every window whose action is EXCLUDE
    or MASK_WINDOW.

    Args:
        window_list: Parsed JSON array from ``window_geometry.window_list_json``.
        image_width: Screenshot width in pixels.
        image_height: Screenshot height in pixels.
        pixel_ratio: Retina scaling factor (1.0 or 2.0).
        classifier: ``DefaultContextClassifier`` instance.
        evaluator: ``DefaultPolicyEvaluator`` instance.
        display_origin: ``(x, y)`` origin of the main display in global
            coordinates. Window bounds are offset by this to map to the
            screenshot coordinate space.

    Returns:
        List of ``MaskRegion`` for all sensitive windows.
    """
    # KEYSTROKE_NULL_ACTIONS = {EXCLUDE, MASK_WINDOW} — both should be masked.
    mask_actions = KEYSTROKE_NULL_ACTIONS

    regions: list[MaskRegion] = []
    disp_x, disp_y = display_origin

    for win in window_list:
        bundle_id = win.get("bundle_id", "")
        if not bundle_id:
            continue

        meta = FrameMetadata(
            bundle_id=bundle_id,
            window_title=win.get("app_name", ""),
            domain=None,
            timestamp=0.0,
        )
        ctx = classifier.classify(meta)
        decision = evaluator.evaluate(ctx, meta)

        if decision.action not in mask_actions:
            continue

        # Convert from logical points to screenshot pixels
        raw_x = (win.get("x", 0) - disp_x) * pixel_ratio
        raw_y = (win.get("y", 0) - disp_y) * pixel_ratio
        raw_w = win.get("width", 0) * pixel_ratio
        raw_h = win.get("height", 0) * pixel_ratio

        # Clip to image bounds — skip windows entirely outside
        x1 = max(0, int(raw_x))
        y1 = max(0, int(raw_y))
        x2 = min(image_width, int(raw_x + raw_w))
        y2 = min(image_height, int(raw_y + raw_h))

        if x2 <= x1 or y2 <= y1:
            continue  # window entirely outside screenshot bounds

        regions.append(MaskRegion(
            x=x1,
            y=y1,
            width=x2 - x1,
            height=y2 - y1,
            label=ctx.context_class.value,
        ))

    return regions


# ---------------------------------------------------------------------------
# Image mask rendering
# ---------------------------------------------------------------------------

# Visual treatment constants
_MASK_COLOR = (30, 30, 30)  # near-black
_LABEL_COLOR = (180, 180, 180)  # light gray text


def _apply_mask_to_image(img, regions: list[MaskRegion]) -> None:
    """Apply visual masks to an already-opened PIL Image, in-place.

    Renders a fully opaque solid fill for each region with a small
    text label so users know the omission was intentional.

    The fill is fully opaque — no original pixel data bleeds through.
    This is a privacy requirement: even 10% bleed-through on high-contrast
    text (black on white) leaves content recoverable via contrast stretch.
    """
    from PIL import ImageDraw

    draw = ImageDraw.Draw(img)

    for region in regions:
        x1, y1 = region.x, region.y
        x2 = min(region.x + region.width, img.width)
        y2 = min(region.y + region.height, img.height)

        draw.rectangle([x1, y1, x2, y2], fill=_MASK_COLOR)

        # Draw label centered in the region
        if region.label:
            label = f"Masked: {region.label}"
            bbox = draw.textbbox((0, 0), label)
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]
            text_x = x1 + (x2 - x1 - text_w) // 2
            text_y = y1 + (y2 - y1 - text_h) // 2
            draw.text((text_x, text_y), label, fill=_LABEL_COLOR)


def mask_screenshot(
    image_path: Path,
    context_class: ContextClass | None,
    strategy: MaskStrategy | None = None,
    app_hint: str = "",
    regions: list[MaskRegion] | None = None,
) -> bool:
    """Apply structural masking to a screenshot based on context.

    If ``regions`` is provided, masks only those specific regions
    (selective per-window masking). Otherwise uses the full-window or
    pane strategy.

    Returns True if masking was applied, False if no masking strategy
    exists for this context (caller should handle via other means).
    """
    from PIL import Image

    # Selective masking with pre-computed regions
    if regions is not None:
        if not regions:
            return False  # no regions to mask
        with Image.open(image_path) as probe:
            converted = probe.convert("RGB")
        try:
            _apply_mask_to_image(converted, regions)
            converted.save(image_path, "JPEG", quality=85, exif=b"")
        finally:
            converted.close()
        return True

    # Strategy-based masking (original behavior)
    if strategy is None:
        strategy = get_mask_strategy(context_class)
    if strategy is None:
        return False

    if strategy == MaskStrategy.FULL_WINDOW:
        # Read dimensions without decoding pixel data, then create blank image
        with Image.open(image_path) as probe:
            w, h = probe.size
        img = Image.new("RGB", (w, h), _MASK_COLOR)
        full_regions = full_window_geometry(w, h, context_class)
        # Draw labels on the blank image
        _apply_mask_to_image(img, full_regions)
        img.save(image_path, "JPEG", quality=85, exif=b"")
        img.close()
    else:
        with Image.open(image_path) as probe:
            converted = probe.convert("RGB")
        try:
            w, h = converted.size
            pane_regions = pane_geometry(w, h, context_class, app_hint)
            _apply_mask_to_image(converted, pane_regions)
            converted.save(image_path, "JPEG", quality=85, exif=b"")
        finally:
            converted.close()

    return True
