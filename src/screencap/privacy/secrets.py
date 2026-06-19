"""Temporary re-export shim — ``secrets`` moved to ``screencap.redaction.secrets`` (SCR-33 U1).

A deferred consumer (``cli``) still imports
``from screencap.privacy.secrets import DetectSecretsDetector``; this shim keeps
that resolving until the U5 consumer cutover. Imported only in-function, never
on the package-import path, so it does not regress import lightness (R1).
"""

from __future__ import annotations

from screencap.redaction.secrets import DetectSecretsDetector  # noqa: F401
