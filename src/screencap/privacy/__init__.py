"""Privacy package — shared privacy-model vocabulary (in transition).

The detection / anonymization **engine** that used to live in this
``__init__`` has moved to ``screencap.redaction`` (SCR-33 U1). This module is
now a **temporary, light re-export shim**: legacy ``from screencap.privacy
import <engine symbol>`` call sites keep resolving via the lazy PEP 562
``__getattr__`` below until the consumer cutover unit (U5) repoints them.

IMPORTANT — keep this import light. Importing ``screencap.privacy`` (or any
of its submodules, e.g. ``screencap.privacy.actions``) must **not** eagerly
import ``screencap.redaction.engine``; the whole point of the split is that a
small shared symbol no longer drags in the 488-line detection engine. The
``__getattr__`` defers the engine import until an engine symbol is actually
accessed, and forwards straight to the engine module so the returned objects
are the *identical* class/function objects (e.g.
``screencap.privacy.AllDetectorsFailedError is
screencap.redaction.engine.AllDetectorsFailedError``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-checkers / IDEs resolve the real symbols without runtime import cost.
    from screencap.redaction.engine import (
        AllDetectorsFailedError,
        Anonymizer,
        Detection,
        DetectionFilter,
        DetectionPipeline,
        DetectionResult,
        EntityType,
        TextDetector,
        are_nlp_models_cached,
        create_default_pipeline,
        normalize_text,
    )

# Engine symbols re-exported by the temporary shim. Includes the private
# helpers a few call sites still reach for (``_GLINER_ONNX_VARIANT`` consumed
# by the GLiNER backend; ``_cleanup_stale_onnx_blobs`` / ``_merge_detections``
# referenced by tests). Removed in U5/U6 once consumers are cut over.
__all__ = [
    "AllDetectorsFailedError",
    "Anonymizer",
    "Detection",
    "DetectionFilter",
    "DetectionPipeline",
    "DetectionResult",
    "EntityType",
    "TextDetector",
    "are_nlp_models_cached",
    "create_default_pipeline",
    "normalize_text",
]


def __getattr__(name: str) -> object:
    """Lazily forward engine symbols to ``screencap.redaction.engine`` (PEP 562).

    Defers the heavy-engine import until an engine symbol is accessed, so
    ``import screencap.privacy`` itself stays light (R1). Forwarding to the
    engine module (not a copy) preserves object identity — critical for
    ``except AllDetectorsFailedError`` to keep matching across the shim window.
    """
    from screencap.redaction import engine

    try:
        return getattr(engine, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
