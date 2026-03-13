"""Privacy detection and anonymization engine.

A pluggable three-stage pipeline (secrets, PII, regex) that operates purely
on strings. Zero imports from screencap.* — this subpackage is standalone.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from screencap.privacy.resolver import DetectionResolver

logger = logging.getLogger(__name__)

# Zero-width / invisible characters to strip during normalization.
# Covers Unicode "default ignorable" code points that can break regex matching.
_ZERO_WIDTH = re.compile(
    "["
    "\u00ad"   # Soft hyphen
    "\u034f"   # Combining grapheme joiner
    "\u061c"   # Arabic letter mark
    "\u115f"   # Hangul choseong filler
    "\u1160"   # Hangul jungseong filler
    "\u17b4"   # Khmer vowel inherent Aq
    "\u17b5"   # Khmer vowel inherent Aa
    "\u180b-\u180e"  # Mongolian free variation selectors + vowel separator
    "\u200b-\u200f"  # Zero-width space, ZWNJ, ZWJ, LRM, RLM
    "\u202a-\u202e"  # Bidi embedding controls
    "\u2060-\u2064"  # Word joiner, invisible times/separator/plus
    "\u2066-\u2069"  # Bidi isolate controls
    "\u206a-\u206f"  # Deprecated formatting chars
    "\ufe00-\ufe0f"  # Variation selectors
    "\ufeff"          # BOM / zero-width no-break space
    "\uffa0"          # Halfwidth Hangul filler
    "\ufff0-\ufff8"  # Specials
    "\U000e0001"     # Language tag
    "\U000e0020-\U000e007f"  # Tag components
    "\U000e0100-\U000e01ef"  # Variation selectors supplement
    "]"
)


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
    source: str  # detector name: "secrets", "pii-presidio", "pii-gliner", "regex"


class DetectionResult(NamedTuple):
    """Result from DetectionPipeline.detect().

    Contains the normalized text (which offsets refer to) alongside
    detections. Always pass ``normalized_text`` to Anonymizer.anonymize().
    """

    normalized_text: str
    detections: list[Detection]


class TextDetector(Protocol):
    """Protocol for pluggable text detection backends."""

    def detect(self, text: str) -> list[Detection]: ...


class DetectionFilter(Protocol):
    """Protocol for post-detection false positive filters."""

    def filter(self, text: str, detections: list[Detection]) -> list[Detection]:
        """Return detections that pass the filter (remove false positives)."""
        ...


class AllDetectorsFailedError(Exception):
    """Raised when every detector in the pipeline fails."""


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    """NFKC normalize and strip zero-width / invisible characters."""
    text = unicodedata.normalize("NFKC", text)
    return _ZERO_WIDTH.sub("", text)


def _sanitize_error(error: Exception, input_text: str) -> str:
    """Sanitize error message — never log input text."""
    class_name = type(error).__name__
    return f"{class_name} (message suppressed — may contain input text)"


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
    """Composes multiple TextDetector instances with resolve/filter/dedup."""

    last_errors: dict[str, str]

    def __init__(
        self,
        detectors: list[TextDetector],
        filters: list[DetectionFilter] | None = None,
        resolver: DetectionResolver | None = None,
    ) -> None:
        if not detectors:
            raise ValueError(
                "DetectionPipeline requires at least one detector. "
                "Use create_default_pipeline() to build a configured pipeline."
            )
        self._detectors = list(detectors)
        self._filters = list(filters) if filters else []
        self._resolver = resolver
        self.last_errors = {}

    def detect(self, text: str) -> DetectionResult:
        """Run all detectors and return normalized text + merged detections.

        Returns a DetectionResult(normalized_text, detections). Always use
        the normalized_text when passing results to Anonymizer.anonymize().
        """
        self.last_errors = {}

        # Short-circuit: no meaningful entity fits in < 4 chars
        if len(text) < 4:
            return DetectionResult(text, [])

        # Normalize — all offsets in detections refer to this text
        normalized = normalize_text(text)

        all_detections: list[Detection] = []
        failures = 0

        for detector in self._detectors:
            name = type(detector).__name__
            try:
                results = detector.detect(normalized)
                all_detections.extend(results)
            except Exception as exc:
                failures += 1
                sanitized = _sanitize_error(exc, normalized)
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

        # Resolve overlaps: use resolver if provided, else legacy merge
        if self._resolver is not None:
            resolved = self._resolver.resolve(all_detections)
        else:
            resolved = _merge_detections(all_detections)

        # Run filters sequentially
        filtered = resolved
        for f in self._filters:
            try:
                filtered = f.filter(normalized, filtered)
            except Exception as exc:
                filter_name = type(f).__name__
                sanitized = _sanitize_error(exc, normalized)
                logger.warning("Filter %s failed, skipping: %s", filter_name, sanitized)

        return DetectionResult(normalized, filtered)


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

_VALID_PII_ENGINES = frozenset({"presidio", "presidio-gliner", None})


def create_default_pipeline(
    pii_engine: str | None = None,
    person_threshold: float = 0.5,
    person_allowlist: frozenset[str] = frozenset(),
) -> DetectionPipeline:
    """Create a pipeline with all available detectors.

    Args:
        pii_engine: PII backend to use. One of "presidio" (spaCy NER),
            "presidio-gliner" (GLiNER NER), or None (auto: GLiNER first,
            falls back to spaCy).
        person_threshold: Drop PERSON detections with score <= this value.
        person_allowlist: Lowercased app names to suppress as PERSON hits.

    Call ONCE per scrub session — detector constructors load NLP models
    (~200-500ms for spaCy, ~1-2s for GLiNER). Reuse the returned pipeline
    for all text chunks.

    Raises ImportError with an actionable message if privacy deps are missing.
    """
    if pii_engine not in _VALID_PII_ENGINES:
        raise ValueError(
            f"Invalid pii_engine={pii_engine!r}. "
            f"Must be one of: 'presidio', 'presidio-gliner', or None (auto-detect)."
        )

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
            "Reinstall with: pip install screencap"
        )

    # PII engine selection: GLiNER (default) or spaCy (legacy)
    pii_loaded = False
    ner_backend = "spacy" if pii_engine == "presidio" else "gliner"

    try:
        from screencap.privacy.pii import PiiDetector

        detectors.append(PiiDetector(
            person_threshold=person_threshold,
            person_allowlist=person_allowlist,
            ner_backend=ner_backend,
        ))
        pii_loaded = True
    except ImportError as exc:
        if pii_engine == "presidio-gliner":
            raise ImportError(
                "GLiNER not installed. "
                "Install with: pip install 'presidio-analyzer[gliner]'"
            ) from exc
        if pii_engine == "presidio":
            raise ImportError(
                "Presidio not installed. "
                "Install with: pip install presidio-analyzer"
            ) from exc
        # Auto-detect: GLiNER failed, try spaCy fallback
        if ner_backend == "gliner":
            logger.info("GLiNER not available, falling back to spaCy...")
            try:
                detectors.append(PiiDetector(
                    person_threshold=person_threshold,
                    person_allowlist=person_allowlist,
                    ner_backend="spacy",
                ))
                pii_loaded = True
            except ImportError:
                pass

    if not pii_loaded:
        logger.warning(
            "No PII engine installed — PII detection disabled. "
            "Reinstall with: pip install screencap"
        )

    if len(detectors) < 2:
        raise ImportError(
            "Privacy detection requires at least detect-secrets or a PII engine. "
            "Reinstall with: pip install screencap"
        )

    from screencap.privacy.filters import HeuristicFilter
    from screencap.privacy.resolver import DetectionResolver as _Resolver

    resolver = _Resolver()
    filters: list[DetectionFilter] = [HeuristicFilter()]

    return DetectionPipeline(detectors, filters=filters, resolver=resolver)
