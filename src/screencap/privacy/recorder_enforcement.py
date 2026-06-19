"""Temporary SCR-33 U3 shim — removed in U5.

``recorder_enforcement`` moved to :mod:`screencap.enforcement.recorder_enforcement`.
This module re-exports its public surface so existing
``from screencap.privacy.recorder_enforcement import ...`` sites keep working
until the U5 cutover. Plain re-export (preserves object identity).

``KEYSTROKE_CONTENT_FIELDS`` is re-exported because consumers import it from
this module path (it is shared-core vocabulary that ``recorder_enforcement``
itself re-imports from :mod:`screencap.privacy.actions`).
"""

from __future__ import annotations

from screencap.enforcement.recorder_enforcement import (  # noqa: F401
    KEYSTROKE_CONTENT_FIELDS,
    CaptureDisposition,
    RecorderPrivacyFilter,
)

__all__ = [
    "KEYSTROKE_CONTENT_FIELDS",
    "CaptureDisposition",
    "RecorderPrivacyFilter",
]
