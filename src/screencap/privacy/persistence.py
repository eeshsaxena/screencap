"""Temporary SCR-33 U3 shim — removed in U5.

``persistence`` moved to :mod:`screencap.enforcement.persistence`. This module
re-exports its public surface so existing
``from screencap.privacy.persistence import ...`` sites keep working until the
U5 cutover. Plain re-export (preserves object identity).
"""

from __future__ import annotations

from screencap.enforcement.persistence import persist_disable  # noqa: F401

__all__ = ["persist_disable"]
