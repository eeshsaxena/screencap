"""Temporary re-export shim — ``ocr`` moved to ``screencap.redaction.ocr`` (SCR-33 U1).

Deferred consumers (``scrubber``/``chunk_processor``) still import
``from screencap.privacy.ocr import VisionOcr`` / ``build_offset_map`` and tests
still ``monkeypatch.setattr("screencap.privacy.ocr.VisionOcr", ...)``; this shim
keeps those resolving (and patchable on this module object) until the U5
consumer cutover repoints them to ``screencap.redaction.ocr``. Importing this
shim is always deferred (in-function), never on the package-import path, so it
does not regress import lightness (R1).
"""

from __future__ import annotations

from screencap.redaction.ocr import (  # noqa: F401
    VisionOcr,
    build_offset_map,
)
