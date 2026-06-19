"""Post-hoc redaction package: the standalone detection + anonymization engine.

This package owns the post-hoc string-detection / anonymization pipeline
(``engine`` + its regex/secrets/pii/filters/resolver backends and the
Apple Vision ``ocr`` reader). It depends one-way on the shared
``screencap.privacy`` core; nothing here imports ``screencap.enforcement``.

The package ``__init__`` is intentionally LIGHT: importing
``screencap.redaction`` must not eagerly pull in the heavy engine module
(which defers ``transformers``/``torch``/GLiNER loads, but is itself a
488-line module). The public engine surface is re-exported lazily via
PEP 562 ``__getattr__`` so that ``from screencap.redaction import
create_default_pipeline`` works without importing the engine until a symbol
is actually accessed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Static type-checkers / IDEs see the real symbols without runtime cost.
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

# Public engine surface re-exported lazily from ``engine``. Listed for
# ``from screencap.redaction import *`` and for the ``__getattr__`` allowlist.
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
    """Lazily resolve public engine symbols (PEP 562).

    Keeps ``import screencap.redaction`` light: the heavy ``engine`` module is
    only imported when a re-exported symbol is first accessed.
    """
    if name in __all__:
        from screencap.redaction import engine

        return getattr(engine, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
