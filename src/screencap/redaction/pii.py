"""PiiDetector — wraps Presidio Analyzer for PII detection.

Supports two NER backends:
- 'gliner' (default): GLiNER model via fast-gliner (Rust ONNX inference)
- 'spacy' (legacy): spaCy NER via Presidio's default SpacyRecognizer
"""

from __future__ import annotations

import logging
from typing import Literal

from screencap.redaction.engine import Detection, EntityType
from screencap.redaction.entity_mapping import GLINER_ENTITY_MAPPING, PRESIDIO_MAP

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
        # Token-level allowlist: split each multi-word entry and keep tokens >= 3 chars.
        # Whitespace-only split preserves "Terminal.app" as one token (no dot/hyphen split).
        self._person_allowlist_tokens: frozenset[str] = frozenset(
            token.lower()
            for entry in person_allowlist
            for token in entry.split()
            if len(token) >= 3
        )

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
        from presidio_analyzer import (
            AnalysisExplanation,
            AnalyzerEngine,
            LocalRecognizer,
            RecognizerResult,
        )
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        class _FastGLiNERRecognizer(LocalRecognizer):
            """Presidio recognizer wrapping fast_gliner for ONNX-based GLiNER inference."""

            def __init__(
                self,
                model_id: str,
                entity_mapping: dict[str, str],
                threshold: float = 0.3,
                supported_language: str = "en",
            ):
                self._model_id = model_id
                self._entity_mapping = entity_mapping
                self._gliner_labels = list(entity_mapping.keys())
                self._threshold = threshold

                super().__init__(
                    supported_entities=list(set(entity_mapping.values())),
                    name="FastGLiNERRecognizer",
                    supported_language=supported_language,
                )

            def load(self) -> None:
                from fast_gliner import FastGLiNER

                from screencap.redaction.engine import _GLINER_ONNX_VARIANT

                self._model = FastGLiNER.from_pretrained(
                    self._model_id, onnx_path=f"onnx/{_GLINER_ONNX_VARIANT}"
                )

            def analyze(self, text, entities, nlp_artifacts=None):
                predictions = self._model.predict_entities(text, self._gliner_labels)
                results = []
                for pred in predictions:
                    if pred["score"] < self._threshold:
                        continue
                    presidio_type = self._entity_mapping.get(pred["label"])
                    if presidio_type is None or (entities and presidio_type not in entities):
                        continue
                    results.append(RecognizerResult(
                        entity_type=presidio_type,
                        start=pred["start"],
                        end=pred["end"],
                        score=pred["score"],
                        analysis_explanation=AnalysisExplanation(
                            recognizer=self.name,
                            original_score=pred["score"],
                            textual_explanation=f"Identified as {presidio_type} by FastGLiNER",
                        ),
                    ))
                return results

        # spaCy still needed for tokenization/sentence segmentation,
        # even though we replace its NER with GLiNER.
        nlp_engine = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
            }
        ).create_engine()

        recognizer = _FastGLiNERRecognizer(
            model_id=model,
            entity_mapping=GLINER_ENTITY_MAPPING,
        )

        self._analyzer = AnalyzerEngine(nlp_engine=nlp_engine)
        self._analyzer.registry.add_recognizer(recognizer)
        # Remove spaCy NER to prevent double-detection on PERSON/LOCATION
        self._analyzer.registry.remove_recognizer("SpacyRecognizer")

        logger.info("PiiDetector initialized with fast-gliner backend (%s)", model)

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
                    logger.debug("Allowlist suppressed PERSON detection (len=%d)", len(span))
                    continue
                if span in self._person_allowlist_tokens:
                    logger.debug("Token-allowlist suppressed PERSON (len=%d)", len(span))
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
