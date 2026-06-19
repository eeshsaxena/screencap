"""Temporary SCR-33 U3 shim — removed in U5.

``disable_log`` moved to :mod:`screencap.enforcement.disable_log`. This module
re-exports its public surface so existing
``from screencap.privacy.disable_log import ...`` sites keep working until the
U5 cutover. Plain re-export (preserves object identity).
"""

from __future__ import annotations

from screencap.enforcement.disable_log import DisableLogWriter  # noqa: F401

__all__ = ["DisableLogWriter"]
