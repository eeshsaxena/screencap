"""Temporary re-export shim — ``pii`` moved to ``screencap.redaction.pii`` (SCR-33 U1).

A deferred consumer (``setup_wizard``) still imports
``from screencap.privacy.pii import PiiDetector``; this shim keeps that resolving
until the U5 consumer cutover. Imported only in-function, never on the
package-import path, so it does not regress import lightness (R1).
"""

from __future__ import annotations

from screencap.redaction.pii import PiiDetector  # noqa: F401
