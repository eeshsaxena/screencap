"""Temporary re-export shim for the split masking module (SCR-33 U2).

``masking.py`` was split into two homes:

- shared pixel/region/geometry primitives → ``screencap.privacy.mask_primitives``
  (consumed by BOTH the capture-time and post-hoc paths);
- policy-driven scrub-time orchestration → ``screencap.redaction.masking``.

This shim keeps ``from screencap.privacy.masking import X`` working for
not-yet-cut consumers (``scrubber.py``, ``video_mask.py``, ``cli``) through
U5, when it is deleted.

Crucially, importing a *primitive* through this shim must NOT transitively
import ``screencap.redaction.masking`` — otherwise the capture path would
pick up a ``redaction`` dependency. So the primitives are imported eagerly
at module level, while the orchestration names are forwarded lazily via a
PEP 562 ``__getattr__`` that only touches ``screencap.redaction.masking``
when one of those names is actually accessed. Forwarding preserves object
identity (we re-export the real objects, never copies).
"""

from __future__ import annotations

# Eager re-export of the shared primitives (no redaction import).
from screencap.privacy.mask_primitives import (  # noqa: F401
    _LABEL_COLOR,
    _MASK_COLOR,
    MaskRegion,
    _apply_bitmap_mask_to_image,
    _apply_mask_to_image,
    _build_z_order_regions,
    _window_to_pixel_rect,
    full_window_geometry,
    pane_geometry,
    window_regions_from_geometry,
)

# Orchestration names forwarded lazily from screencap.redaction.masking so
# that importing a primitive through this shim does not pull in redaction.
_ORCHESTRATION_NAMES = frozenset(
    {"MaskStrategy", "get_mask_strategy", "mask_screenshot"}
)


def __getattr__(name: str):
    if name in _ORCHESTRATION_NAMES:
        from screencap.redaction import masking as _orchestration

        return getattr(_orchestration, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
