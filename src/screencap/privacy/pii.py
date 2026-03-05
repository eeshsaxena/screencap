"""PiiDetector — wraps Presidio Analyzer for PII detection."""

from __future__ import annotations

from presidio_analyzer import AnalyzerEngine

from screencap.privacy import Detection, EntityType

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

    def __init__(self) -> None:
        self._analyzer = AnalyzerEngine()

    def detect(self, text: str) -> list[Detection]:
        results = self._analyzer.analyze(text=text, language="en")

        detections: list[Detection] = []
        for result in results:
            entity_type = _TYPE_MAP.get(result.entity_type)
            if entity_type is None:
                # Unmapped PII type — skip (PII types should not use
                # SECRET catch-all per plan)
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
