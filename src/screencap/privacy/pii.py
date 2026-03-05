"""PiiDetector — wraps Presidio Analyzer for PII detection."""

from __future__ import annotations

import logging

from presidio_analyzer import AnalyzerEngine

from screencap.privacy import Detection, EntityType

logger = logging.getLogger(__name__)

# Map Presidio entity types to our EntityType constants
_TYPE_MAP: dict[str, str] = {
    "PERSON": EntityType.PERSON,
    "EMAIL_ADDRESS": EntityType.EMAIL,
    "PHONE_NUMBER": EntityType.PHONE,
    "US_SSN": EntityType.SSN,
    "CREDIT_CARD": EntityType.CREDIT_CARD,
    "LOCATION": EntityType.ADDRESS,
    # Additional Presidio types we want to capture
    "NRP": EntityType.PERSON,  # Nationality, religious, political group
}


class PiiDetector:
    """Wraps Presidio Analyzer for PII detection.

    Constructor loads the spaCy NLP model (~200-500ms). Create once,
    reuse for all text chunks.
    """

    def __init__(
        self,
        person_threshold: float = 0.5,
        person_allowlist: frozenset[str] = frozenset(),
    ) -> None:
        self._analyzer = AnalyzerEngine()
        self._person_threshold = person_threshold
        self._person_allowlist = person_allowlist

    def detect(self, text: str) -> list[Detection]:
        results = self._analyzer.analyze(text=text, language="en")

        detections: list[Detection] = []
        for result in results:
            entity_type = _TYPE_MAP.get(result.entity_type)
            if entity_type is None:
                # Unmapped PII type — skip (PII types should not use
                # SECRET catch-all per plan)
                continue

            if entity_type == EntityType.PERSON:
                if result.score < self._person_threshold:
                    continue
                span = text[result.start : result.end].lower()
                if span in self._person_allowlist:
                    logger.debug("Allowlist suppressed PERSON: %r", span)
                    continue

            detections.append(
                Detection(
                    entity_type=entity_type,
                    start=result.start,
                    end=result.end,
                    score=result.score,
                    source="pii-presidio",
                )
            )

        return detections
