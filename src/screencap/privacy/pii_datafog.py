"""PiiDetector backed by DataFog — NER-capable PII engine using spaCy."""

from __future__ import annotations

from datafog.engine import scan as datafog_scan

from screencap.privacy import Detection, EntityType

# Map DataFog canonical entity types to our EntityType constants.
# ORGANIZATION dropped — no EntityType constant and high false positive rate
# on app names (Terminal.app, Homebrew, Bitwarden) in window titles.
# LOCATION dropped — too coarse (cities/countries); ADDRESS covers street addresses.
_TYPE_MAP: dict[str, str] = {
    "EMAIL": EntityType.EMAIL,
    "PHONE": EntityType.PHONE,
    "SSN": EntityType.SSN,
    "CREDIT_CARD": EntityType.CREDIT_CARD,
    "PERSON": EntityType.PERSON,
    "ADDRESS": EntityType.ADDRESS,
}


class DataFogPiiDetector:
    """Wraps DataFog engine for PII detection using spaCy NER.

    Constructor loads the spaCy NLP model eagerly (~1-2s). Create once,
    reuse for all text chunks.
    """

    def __init__(self) -> None:
        # Eager warm-up: load the spaCy model now so it's covered by the
        # "Loading privacy detection engine..." spinner in scrubber.py.
        # Also validates that datafog[nlp] is properly installed.
        datafog_scan(" ", engine="spacy")

    def detect(self, text: str) -> list[Detection]:
        result = datafog_scan(text, engine="spacy")

        detections: list[Detection] = []
        for entity in result.entities:
            entity_type = _TYPE_MAP.get(entity.type)
            if entity_type is None:
                continue

            detections.append(
                Detection(
                    entity_type=entity_type,
                    start=entity.start,
                    end=entity.end,
                    score=entity.confidence,
                    source="pii-datafog",
                )
            )

        return detections
