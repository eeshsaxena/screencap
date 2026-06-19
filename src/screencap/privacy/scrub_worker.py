"""Temporary SCR-33 U3 shim — removed in U5.

``scrub_worker`` (the capture-time row-deletion sidecar — NOT the post-hoc
``screencap.redaction`` text scrubber) moved to
:mod:`screencap.enforcement.scrub_worker`. This module re-exports its public
surface so existing ``from screencap.privacy.scrub_worker import ...`` sites
keep working until the U5 cutover. Plain re-export (preserves object identity).
"""

from __future__ import annotations

from screencap.enforcement.scrub_worker import ScrubWorker  # noqa: F401

__all__ = ["ScrubWorker"]
