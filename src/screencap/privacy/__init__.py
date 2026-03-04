"""Privacy detection and anonymization engine.

A pluggable three-stage pipeline (secrets, PII, regex) that operates purely
on strings. Zero imports from screencap.* — this subpackage is standalone.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Zero-width characters to strip during normalization
_ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")


# ---------------------------------------------------------------------------
# Entity types — all detectors MUST map to these
# ---------------------------------------------------------------------------


class EntityType:
    # PII (6)
    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    SSN = "SSN"
    CREDIT_CARD = "CREDIT_CARD"
    ADDRESS = "ADDRESS"

    # Secrets (6)
    API_KEY = "API_KEY"
    PRIVATE_KEY = "PRIVATE_KEY"
    PASSWORD = "PASSWORD"
    JWT = "JWT"
    CONNECTION_STRING = "CONNECTION_STRING"
    SECRET = "SECRET"


@dataclass(frozen=True)
class Detection:
    """A detected entity span within text."""

    entity_type: str  # EntityType constant
    start: int  # character offset (inclusive)
    end: int  # character offset (exclusive)
    score: float  # confidence 0.0-1.0
    source: str  # detector name: "secrets", "pii", "regex"


class TextDetector(Protocol):
    """Protocol for pluggable text detection backends."""

    def detect(self, text: str) -> list[Detection]: ...


class AllDetectorsFailedError(Exception):
    """Raised when every detector in the pipeline fails."""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """NFKC normalize and strip zero-width characters."""
    text = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH.sub("", text)


def _sanitize_error(error: Exception, input_text: str) -> str:
    """Sanitize error message — never log input text."""
    class_name = type(error).__name__
    msg = str(error)[:80]
    if input_text and len(input_text) >= 4:
        msg = msg.replace(input_text, "[TEXT]")
        # Also replace substrings of input that might appear
        for i in range(0, len(input_text) - 3, 4):
            chunk = input_text[i : i + 8]
            if chunk in msg:
                msg = msg.replace(chunk, "[TEXT]")
    return f"{class_name}: {msg}"


def _merge_detections(detections: list[Detection]) -> list[Detection]:
    """Sort, dedup identical spans, and merge overlapping/nested spans."""
    if not detections:
        return []

    # Sort by (start, end)
    sorted_dets = sorted(detections, key=lambda d: (d.start, d.end))

    # Dedup identical (start, end) — keep higher score
    deduped: list[Detection] = []
    for det in sorted_dets:
        if deduped and deduped[-1].start == det.start and deduped[-1].end == det.end:
            if det.score > deduped[-1].score:
                deduped[-1] = det
        else:
            deduped.append(det)

    # Merge overlapping/nested spans
    merged: list[Detection] = [deduped[0]]
    for det in deduped[1:]:
        prev = merged[-1]
        if det.start < prev.end:
            # Overlapping or nested — expand to union
            new_start = min(prev.start, det.start)
            new_end = max(prev.end, det.end)
            # Use higher-scoring detection's entity type
            winner = det if det.score > prev.score else prev
            merged[-1] = Detection(
                entity_type=winner.entity_type,
                start=new_start,
                end=new_end,
                score=max(prev.score, det.score),
                source=winner.source,
            )
        else:
            merged.append(det)

    return merged


class DetectionPipeline:
    """Composes multiple TextDetector instances with merge/dedup."""

    last_errors: dict[str, str]

    def __init__(self, detectors: list[TextDetector]) -> None:
        self._detectors = list(detectors)
        self.last_errors = {}

    def detect(self, text: str) -> list[Detection]:
        """Run all detectors and return merged, non-overlapping detections."""
        self.last_errors = {}

        # Short-circuit: no meaningful entity fits in < 4 chars
        if len(text) < 4:
            return []

        # Normalize
        text = normalize_text(text)

        all_detections: list[Detection] = []
        failures = 0

        for detector in self._detectors:
            name = type(detector).__name__
            try:
                results = detector.detect(text)
                all_detections.extend(results)
            except Exception as exc:
                failures += 1
                sanitized = _sanitize_error(exc, text)
                self.last_errors[name] = sanitized
                logger.warning("Detector %s failed: %s", name, sanitized)
                logger.debug(
                    "Detector %s full traceback:", name, exc_info=True
                )

        # All-detectors-fail guard
        if failures == len(self._detectors) and not all_detections:
            raise AllDetectorsFailedError(
                f"All {failures} detectors failed. "
                f"Errors: {self.last_errors}"
            )

        return _merge_detections(all_detections)


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------


class Anonymizer:
    """Replaces detected spans with <ENTITY_TYPE> tags."""

    def anonymize(self, text: str, detections: list[Detection]) -> str:
        if not detections:
            return text

        # Validate offsets and filter invalid ones
        valid: list[Detection] = []
        for det in detections:
            if 0 <= det.start < det.end <= len(text):
                valid.append(det)
            else:
                logger.warning(
                    "Skipping detection with out-of-bounds offsets: "
                    "start=%d, end=%d, text_len=%d",
                    det.start,
                    det.end,
                    len(text),
                )

        if not valid:
            return text

        # Safety net: merge overlapping detections
        merged = _merge_detections(valid)
        if len(merged) != len(valid):
            logger.warning(
                "Anonymizer received overlapping detections — "
                "merged %d → %d as safety net",
                len(valid),
                len(merged),
            )

        # Process right-to-left to preserve offsets
        for det in sorted(merged, key=lambda d: d.start, reverse=True):
            tag = f"<{det.entity_type}>"
            text = text[: det.start] + tag + text[det.end :]

        return text


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_default_pipeline(
    pii_engine: str | None = None,
) -> DetectionPipeline:
    """Create a pipeline with all available detectors.

    Args:
        pii_engine: PII backend to use. One of "presidio", "datafog", or
            None (auto-detect: tries Presidio first, falls back to DataFog).

    Call ONCE per scrub session — detector constructors load NLP models
    (~200-500ms for Presidio/spaCy). Reuse the returned pipeline for all
    text chunks.

    Raises ImportError with an actionable message if privacy deps are missing.
    """
    detectors: list[TextDetector] = []

    # RegexDetector has no external deps — always available
    from screencap.privacy.regex import RegexDetector

    detectors.append(RegexDetector())

    try:
        from screencap.privacy.secrets import DetectSecretsDetector

        detectors.append(DetectSecretsDetector())
    except ImportError:
        logger.warning(
            "detect-secrets not installed — secrets detection disabled. "
            "Install with: pip install 'screencap[privacy]'"
        )

    # PII engine selection
    pii_loaded = False
    if pii_engine in (None, "presidio"):
        try:
            from screencap.privacy.pii import PiiDetector

            detectors.append(PiiDetector())
            pii_loaded = True
        except ImportError:
            if pii_engine == "presidio":
                raise ImportError(
                    "Presidio not installed. "
                    "Install with: pip install presidio-analyzer"
                )
            logger.info("Presidio not available, trying DataFog...")

    if not pii_loaded and pii_engine in (None, "datafog"):
        try:
            from screencap.privacy.pii_datafog import DataFogPiiDetector

            detectors.append(DataFogPiiDetector())
            pii_loaded = True
        except ImportError:
            if pii_engine == "datafog":
                raise ImportError(
                    "DataFog not installed. "
                    "Install with: pip install datafog"
                )

    if not pii_loaded:
        logger.warning(
            "No PII engine installed — PII detection disabled. "
            "Install with: pip install presidio-analyzer  (or: pip install datafog)"
        )

    if len(detectors) < 2:
        raise ImportError(
            "Privacy detection requires at least detect-secrets or a PII engine. "
            "Install with: pip install 'screencap[privacy]'"
        )

    return DetectionPipeline(detectors)
