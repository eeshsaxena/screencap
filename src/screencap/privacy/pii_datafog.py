"""PiiDetector backed by DataFog — lightweight regex-based PII engine."""

from __future__ import annotations

from datafog import DataFog

from screencap.privacy import Detection, EntityType

# Map DataFog entity types to our EntityType constants
_TYPE_MAP: dict[str, str] = {
    "EMAIL": EntityType.EMAIL,
    "PHONE": EntityType.PHONE,
    "SSN": EntityType.SSN,
    "CREDIT_CARD": EntityType.CREDIT_CARD,
    # DataFog types we skip (not in our EntityType set or too noisy)
    # "IP_ADDRESS", "DOB", "ZIP" — not mapped
}


class DataFogPiiDetector:
    """Wraps DataFog for PII detection.

    Lightweight alternative to Presidio — regex-based, no NLP model.
    Detects emails, phones, SSNs, credit cards. Does NOT detect names
    or addresses (no NER capability).
    """

    def __init__(self) -> None:
        self._engine = DataFog()

    def detect(self, text: str) -> list[Detection]:
        results = self._engine.scan_text(text)

        detections: list[Detection] = []
        for datafog_type, values in results.items():
            entity_type = _TYPE_MAP.get(datafog_type)
            if entity_type is None:
                continue

            for value in values:
                # Locate all occurrences of the matched value in text
                search_start = 0
                while True:
                    idx = text.find(value, search_start)
                    if idx == -1:
                        break
                    detections.append(
                        Detection(
                            entity_type=entity_type,
                            start=idx,
                            end=idx + len(value),
                            score=0.85,
                            source="pii",
                        )
                    )
                    search_start = idx + 1

        return detections
