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

from screencap.privacy.actions import KEYSTROKE_NULL_ACTIONS, PrivacyAction
from screencap.privacy.context import DefaultContextClassifier
from screencap.privacy.policy import (
    ContextClass,
    DefaultPolicyEvaluator,
    FrameMetadata,
)


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


def _window_to_pixel_rect(
    win: dict,
    pixel_ratio: float,
    disp_x: float,
    disp_y: float,
    image_width: int,
    image_height: int,
) -> tuple[int, int, int, int] | None:
    """Convert a window dict to clipped pixel coordinates (x1, y1, x2, y2).

    Returns None if the window is entirely outside the image bounds.
    """
    raw_x = (win.get("x", 0) - disp_x) * pixel_ratio
    raw_y = (win.get("y", 0) - disp_y) * pixel_ratio
    raw_w = win.get("width", 0) * pixel_ratio
    raw_h = win.get("height", 0) * pixel_ratio

    x1 = max(0, int(raw_x))
    y1 = max(0, int(raw_y))
    x2 = min(image_width, int(raw_x + raw_w))
    y2 = min(image_height, int(raw_y + raw_h))

    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def window_regions_from_geometry(
    window_list: list[dict],
    image_width: int,
    image_height: int,
    pixel_ratio: float,
    classifier: DefaultContextClassifier,
    evaluator: DefaultPolicyEvaluator,
    display_origin: tuple[float, float] = (0.0, 0.0),
    mask_actions: frozenset[PrivacyAction] | None = None,
    respect_z_order: bool = False,
) -> list[MaskRegion]:
    """Generate mask regions from per-screenshot window geometry.

    Evaluates each window's bundle_id against the privacy policy and
    returns a ``MaskRegion`` for every window whose action is in
    ``mask_actions``.

    When ``respect_z_order`` is True, non-masked foreground windows
    "cut out" any mask regions behind them so that foreground content
    is never obscured. The window_list must be in front-to-back order
    (as returned by ``CGWindowListCopyWindowInfo``).

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
        mask_actions: Set of ``PrivacyAction`` values that trigger masking.
            Defaults to ``KEYSTROKE_NULL_ACTIONS`` (EXCLUDE, MASK_WINDOW).
            Background masking should pass a broader set since we can't
            text-redact a partial screenshot region.
        respect_z_order: If True, use a bitmap approach where non-masked
            foreground windows erase the mask behind them.

    Returns:
        List of ``MaskRegion`` for all sensitive windows. When
        ``respect_z_order`` is True, a single full-image region is
        returned whose label is ``"z_order_mask"`` — the actual
        per-pixel mask is baked into the image by the caller via
        :func:`mask_screenshot_with_bitmap`.
    """
    if mask_actions is None:
        mask_actions = KEYSTROKE_NULL_ACTIONS

    disp_x, disp_y = display_origin

    if not respect_z_order:
        # Original rectangle-based path (no z-order awareness)
        regions: list[MaskRegion] = []
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

            rect = _window_to_pixel_rect(
                win, pixel_ratio, disp_x, disp_y, image_width, image_height,
            )
            if rect is None:
                continue

            x1, y1, x2, y2 = rect
            regions.append(MaskRegion(
                x=x1, y=y1, width=x2 - x1, height=y2 - y1,
                label=ctx.context_class.value,
            ))
        return regions

    # --- Z-order-aware bitmap path ---
    # Classify every window and compute its pixel rect.
    # window_list is front-to-back; we iterate back-to-front so that
    # foreground windows overwrite background decisions.
    entries: list[tuple[bool, tuple[int, int, int, int], str]] = []
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
        should_mask = decision.action in mask_actions

        rect = _window_to_pixel_rect(
            win, pixel_ratio, disp_x, disp_y, image_width, image_height,
        )
        if rect is None:
            continue
        entries.append((should_mask, rect, ctx.context_class.value))

    if not any(should_mask for should_mask, _, _ in entries):
        return []  # nothing to mask

    return _build_z_order_regions(entries, image_width, image_height)


def _build_z_order_regions(
    entries: list[tuple[bool, tuple[int, int, int, int], str]],
    image_width: int,
    image_height: int,
) -> list[MaskRegion]:
    """Build mask regions respecting z-order via a bitmap.

    Paints back-to-front: masked windows set pixels, non-masked
    windows clear them. The result is converted back to MaskRegion
    rectangles by scanning horizontal runs.
    """
    from PIL import Image, ImageDraw

    mask_bmp = Image.new("L", (image_width, image_height), 0)
    draw = ImageDraw.Draw(mask_bmp)

    # entries is front-to-back; reverse for back-to-front painting
    for should_mask, (x1, y1, x2, y2), label in reversed(entries):
        draw.rectangle([x1, y1, x2, y2], fill=255 if should_mask else 0)

    # Check if any pixels are masked
    if mask_bmp.getextrema()[1] == 0:
        mask_bmp.close()
        return []

    # Convert bitmap to a single synthetic region that carries the bitmap.
    # The caller must use mask_screenshot_with_bitmap() instead of the
    # rectangle-based path.
    region = MaskRegion(x=0, y=0, width=image_width, height=image_height,
                        label="z_order_mask")
    # Stash the bitmap on the region for the caller to extract.
    # MaskRegion is frozen, so we use object.__setattr__.
    object.__setattr__(region, "_mask_bitmap", mask_bmp)
    return [region]


# ---------------------------------------------------------------------------
# Image mask rendering
# ---------------------------------------------------------------------------

# Visual treatment constants
_MASK_COLOR = (30, 30, 30)  # near-black
_LABEL_COLOR = (180, 180, 180)  # light gray text


def _apply_bitmap_mask_to_image(img, mask_bitmap) -> None:
    """Apply a bitmap mask to an image, in-place.

    Pixels where ``mask_bitmap`` is white (255) are filled with
    ``_MASK_COLOR``. Pixels where it is black (0) are preserved.
    Used by the z-order-aware masking path.
    """
    from PIL import Image

    mask_fill = Image.new("RGB", img.size, _MASK_COLOR)
    composited = Image.composite(mask_fill, img, mask_bitmap)
    img.paste(composited)
    mask_fill.close()
    composited.close()


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

        # Z-order bitmap path: _build_z_order_regions stashes a PIL
        # bitmap on the first (and only) region.
        mask_bmp = getattr(regions[0], "_mask_bitmap", None)
        if mask_bmp is not None:
            with Image.open(image_path) as probe:
                converted = probe.convert("RGB")
            try:
                _apply_bitmap_mask_to_image(converted, mask_bmp)
                converted.save(image_path, "JPEG", quality=85, exif=b"")
            finally:
                converted.close()
                mask_bmp.close()
            return True

        # Rectangle-based path
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
