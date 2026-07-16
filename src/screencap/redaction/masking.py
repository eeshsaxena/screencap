"""Policy-driven post-hoc structural masking for privacy v3 phase 4.

Applies coarse, context-driven masks to screenshots so communication
surfaces are protected without relying on OCR/NER.

This is the scrub-time orchestration layer: it selects a mask strategy
from the privacy policy and drives the shared pixel/region/geometry
primitives in ``screencap.privacy.mask_primitives``. The primitives are
shared with the capture-time path; only this strategy-selection +
file-writing orchestration is redaction-specific.

Owns:
- support matrix (ContextClass -> mask strategy)
- strategy-driven screenshot masking entry point
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from screencap.privacy.mask_primitives import (
    MASK_COLOR,
    MaskRegion,
    _apply_bitmap_mask_to_image,
    _apply_mask_to_image,
    full_window_geometry,
    pane_geometry,
)
from screencap.privacy.policy import ContextClass

__all__ = [
    "MaskStrategy",
    "get_mask_strategy",
    "mask_screenshot",
    "mask_regions_to_bytes",
]


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
    # CODE_EDITOR_TERMINAL / ADMIN_CONSOLE -> TEXT_REDACT (no masking needed)
}


def get_mask_strategy(context_class: ContextClass) -> MaskStrategy | None:
    """Look up the mask strategy for a context class.

    Returns None for contexts that don't use masking (EXCLUDE or OCR paths).
    """
    return _SURFACE_STRATEGY.get(context_class)


# ---------------------------------------------------------------------------
# Strategy-driven screenshot masking
# ---------------------------------------------------------------------------


def mask_regions_to_bytes(
    image_bytes: bytes,
    regions: list[MaskRegion],
) -> bytes:
    """Return JPEG bytes of ``image_bytes`` with ``regions`` painted out.

    Sibling of :func:`mask_screenshot` for the frame-egress producer
    (``screencap.segmentation.frame_egress``): it uses the SAME fully-opaque
    :func:`_apply_mask_to_image` paint, but renders to an in-memory buffer and
    returns the bytes — it NEVER opens, reads, or rewrites any file, so the
    on-disk original is guaranteed untouched (R9). Re-encoding through Pillow
    also strips EXIF/metadata from the emitted copy.

    ``regions`` may be empty — the image is re-encoded unchanged (an ALLOW frame
    with no residual PII still leaves as a metadata-stripped copy). Raises on
    undecodable input (the caller fails closed by dropping that frame rather than
    emitting it raw).
    """
    import io

    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as probe:
        converted = probe.convert("RGB")
    try:
        if regions:
            _apply_mask_to_image(converted, regions)
        buf = io.BytesIO()
        converted.save(buf, "JPEG", quality=85, exif=b"")
        return buf.getvalue()
    finally:
        converted.close()


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
        img = Image.new("RGB", (w, h), MASK_COLOR)
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
