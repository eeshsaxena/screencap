"""Temporary re-export shim for the split context module (SCR-33 U4).

``context.py`` was two-faced and got split along its real seam:

- the shared, pure, DB-free classifier slice → ``screencap.privacy.classify``
  (``DefaultContextClassifier``, ``BUNDLE_ID_MAP``, ``BROWSER_BUNDLE_IDS``,
  ``PASSWORD_MANAGER_BUNDLES``, ``domain_from_url``, ``_classify_title``, …),
  constructed on BOTH the capture-time and post-hoc paths;
- the scrub-time DB-geometry readers (carrying the
  ``screencap.recording_db`` dependency) → ``screencap.redaction.geometry``
  (``load_window_geometry``, ``load_window_events``, ``associate_screenshot``,
  ``WindowContext``, …).

This shim keeps ``from screencap.privacy.context import X`` working for
not-yet-cut consumers (``scrubber.py``, ``video_mask.py``,
``chunk_processor.py``, ``cli``, ``menubar``, ``setup_wizard``,
``app_discovery``) through U5, when it is deleted.

Crucially, importing a *classifier* symbol through this shim must NOT
transitively import ``screencap.redaction.geometry`` — otherwise a
capture-side consumer importing the classifier through the shim would create
a forbidden ``enforcement → redaction`` edge. So the classifier symbols are
imported eagerly at module level, while the geometry names are forwarded
lazily via a PEP 562 ``__getattr__`` that only touches
``screencap.redaction.geometry`` when one of those names is actually
accessed. Forwarding preserves object identity (we re-export the real
objects, never copies).
"""

from __future__ import annotations

# Eager re-export of the shared classifier slice (no redaction import).
from screencap.privacy.classify import (  # noqa: F401
    _AUTH_KEYWORDS,
    _PAYMENT_KEYWORDS,
    _SEGMENT_SPLIT_RE,
    _TITLE_HEURISTICS,
    BROWSER_BUNDLE_IDS,
    BUNDLE_ID_MAP,
    PASSWORD_MANAGER_BUNDLES,
    DefaultContextClassifier,
    _classify_title,
    domain_from_url,
)

# Geometry names forwarded lazily from screencap.redaction.geometry so that
# importing a classifier symbol through this shim does not pull in redaction.
_GEOMETRY_NAMES = frozenset(
    {
        "WindowContext",
        "WindowGeometrySnapshot",
        "parse_screenshot_timestamp",
        "_active_at_index",
        "find_nearest_window",
        "_load_geometry_row",
        "load_window_geometry",
        "list_geometry_sample_timestamps",
        "geometry_capture_failures_in_span",
        "load_window_events",
        "associate_screenshot",
    }
)


def __getattr__(name: str):
    if name in _GEOMETRY_NAMES:
        from screencap.redaction import geometry as _geometry

        return getattr(_geometry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
