"""Temporary SCR-33 U3 shim — removed in U5.

The window-event privacy filter constructors moved to
:mod:`screencap.enforcement.window_filter` (renamed from ``filter`` to kill the
``filter.py`` / ``filters.py`` collision). This module keeps the OLD
``screencap.privacy.filter`` name working for existing consumers until the U5
cutover. Plain re-export (preserves object identity).
"""

from __future__ import annotations

from screencap.enforcement.window_filter import (  # noqa: F401
    build_cloud_window_filter,
    build_local_window_filter,
    build_privacy_filter,
)

__all__ = [
    "build_cloud_window_filter",
    "build_local_window_filter",
    "build_privacy_filter",
]
