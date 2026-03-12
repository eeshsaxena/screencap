"""PiiDetector — wraps Presidio Analyzer for PII detection.

Supports two NER backends:
- 'gliner' (default): GLiNER model via Presidio's GLiNERRecognizer
- 'spacy' (legacy): spaCy NER via Presidio's default SpacyRecognizer
"""

from __future__ import annotations

import logging
from typing import Literal

from screencap.privacy import Detection, EntityType
from screencap.privacy.entity_mapping import GLINER_ENTITY_MAPPING, PRESIDIO_MAP

logger = logging.getLogger(__name__)


class PiiDetector:
    """PII detection via Presidio with configurable NER backend.

    Constructor loads the NER model (~200-500ms for spaCy, ~1-2s for GLiNER).
    Create once, reuse for all text chunks.
    """

    def __init__(
        self,
        person_threshold: float = 0.5,
        person_allowlist: frozenset[str] = frozenset(),
        ner_backend: Literal["gliner", "spacy"] = "gliner",
        gliner_model: str = "knowledgator/gliner-pii-base-v1.0",
    ) -> None:
        self._person_threshold = person_threshold
        self._person_allowlist = person_allowlist

        if ner_backend == "gliner":
            self._source = "pii-gliner"
            self._init_gliner(gliner_model)
        elif ner_backend == "spacy":
            self._source = "pii-presidio"
            self._init_spacy()
        else:
            raise ValueError(
                f"Invalid ner_backend={ner_backend!r}. Must be 'gliner' or 'spacy'."
            )

    def _init_gliner(self, model: str) -> None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        from presidio_analyzer.predefined_recognizers import GLiNERRecognizer

        # spaCy still needed for tokenization/sentence segmentation,
        # even though we replace its NER with GLiNER.
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()

        gliner_recognizer = GLiNERRecognizer(
            model_name=model,
            entity_mapping=GLINER_ENTITY_MAPPING,
            flat_ner=False,
            multi_label=True,
            map_location="cpu",
        )

        self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        self._analyzer.registry.add_recognizer(gliner_recognizer)
        # Remove spaCy NER to prevent double-detection on PERSON/LOCATION
        self._analyzer.registry.remove_recognizer("SpacyRecognizer")

        logger.info("PiiDetector initialized with GLiNER backend (%s)", model)

    def _init_spacy(self) -> None:
        from presidio_analyzer import AnalyzerEngine

        self._analyzer = AnalyzerEngine()
        logger.info("PiiDetector initialized with spaCy backend")

    def detect(self, text: str) -> list[Detection]:
        results = self._analyzer.analyze(text=text, language="en")

        detections: list[Detection] = []
        for result in results:
            entity_type = PRESIDIO_MAP.get(result.entity_type)
            if entity_type is None:
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
                    source=self._source,
                )
            )

        return detections
