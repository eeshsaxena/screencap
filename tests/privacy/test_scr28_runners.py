"""Tests for the SCR-28 per-model runners' pure logic: label maps and span
decoding. Model-free — no transformers, torch, or GLiNER load. The runners'
heavy imports are deferred, so importing the modules here is cheap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from screencap.privacy import Detection

_SCR28_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "scr28"
if str(_SCR28_DIR) not in sys.path:
    sys.path.insert(0, str(_SCR28_DIR))

import run_gliner  # noqa: E402
import run_privacy_filter as run_pf  # noqa: E402
import scorer  # noqa: E402
from label_maps import OUT_OF_SCOPE, map_label  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Label maps
# ---------------------------------------------------------------------------


class TestLabelMaps:
    @pytest.mark.parametrize(
        "native,expected",
        [
            ("private_person", "PERSON"),
            ("private_email", "EMAIL"),
            ("private_phone", "PHONE"),
            ("private_address", "ADDRESS"),
        ],
    )
    def test_privacy_filter_shared_labels(self, native: str, expected: str) -> None:
        assert map_label("privacy-filter", native) == expected

    @pytest.mark.parametrize(
        "native", ["private_url", "private_date", "account_number", "secret"]
    )
    def test_privacy_filter_extras_are_out_of_scope(self, native: str) -> None:
        # Known-but-unscored: visible to coverage reporting, excluded from the
        # shared-type head-to-head. Distinct from None (unknown).
        assert map_label("privacy-filter", native) == OUT_OF_SCOPE

    def test_privacy_filter_unknown_label_is_none(self) -> None:
        assert map_label("privacy-filter", "totally_unknown") is None

    def test_gliner_already_canonical_label_maps_to_itself(self) -> None:
        assert map_label("gliner", "PERSON") == "PERSON"
        assert map_label("gliner", "CREDIT_CARD") == "CREDIT_CARD"

    def test_gliner_raw_request_label_two_hop(self) -> None:
        # "first name" -> Presidio PERSON -> EntityType.PERSON
        assert map_label("gliner", "first name") == "PERSON"
        assert map_label("gliner", "email address") == "EMAIL"

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(ValueError):
            map_label("spacy", "PERSON")


# ---------------------------------------------------------------------------
# privacy-filter decode (HF pipeline + opf)
# ---------------------------------------------------------------------------


class TestPrivacyFilterDecode:
    def test_multitoken_entity_yields_one_span_with_half_open_offsets(self) -> None:
        # aggregation_strategy="first" groups a multi-token entity into ONE
        # entity dict with char start/end.
        entities = [
            {"entity_group": "private_person", "start": 0, "end": 8, "score": 0.99,
             "word": "John Doe"}
        ]
        spans = run_pf.entities_to_spans(entities)
        assert len(spans) == 1
        assert (spans[0].start, spans[0].end, spans[0].label) == (0, 8, "private_person")

    def test_two_adjacent_same_type_entities_not_merged_by_runner(self) -> None:
        # Two back-to-back emails decode as TWO spans — merging is the scorer's
        # job, not the runner's.
        entities = [
            {"entity_group": "private_email", "start": 0, "end": 6, "score": 0.9},
            {"entity_group": "private_email", "start": 7, "end": 13, "score": 0.9},
        ]
        spans = run_pf.entities_to_spans(entities)
        assert len(spans) == 2
        assert all(s.label == "private_email" for s in spans)

    def test_extras_emitted_with_native_label_not_dropped(self) -> None:
        # private_url etc. survive decode (visible); mapping to OUT_OF_SCOPE is
        # the scorer's job.
        entities = [
            {"entity_group": "private_url", "start": 4, "end": 28, "score": 0.8},
            {"entity_group": "secret", "start": 40, "end": 60, "score": 0.7},
        ]
        spans = run_pf.entities_to_spans(entities)
        labels = {s.label for s in spans}
        assert labels == {"private_url", "secret"}

    def test_zero_width_and_offsetless_groups_dropped(self) -> None:
        entities = [
            {"entity_group": "private_person", "start": 5, "end": 5, "score": 0.9},
            {"entity_group": "private_person", "start": None, "end": None, "score": 0.9},
            {"entity_group": "private_email", "start": 0, "end": 6, "score": 0.9},
        ]
        spans = run_pf.entities_to_spans(entities)
        assert len(spans) == 1
        assert spans[0].label == "private_email"

    def test_empty_entities_yield_empty_spans(self) -> None:
        assert run_pf.entities_to_spans([]) == []

    def test_opf_spans_decode(self) -> None:
        opf_spans = [
            {"label": "private_person", "start": 0, "end": 8, "text": "John Doe"},
            {"label": "private_email", "start": 10, "end": 20, "text": "j@x.com"},
        ]
        spans = run_pf.opf_spans_to_spans(opf_spans)
        assert [(s.start, s.end, s.label) for s in spans] == [
            (0, 8, "private_person"),
            (10, 20, "private_email"),
        ]


# ---------------------------------------------------------------------------
# GLiNER emit logic
# ---------------------------------------------------------------------------


class TestGlinerEmit:
    def test_detections_to_spans_preserves_type_and_source(self) -> None:
        dets = [
            Detection("PERSON", 0, 8, 0.95, "pii-gliner"),
            Detection("API_KEY", 20, 40, 1.0, "secrets"),
        ]
        spans = run_gliner.detections_to_spans(dets)
        assert [(s.start, s.end, s.label, s.source) for s in spans] == [
            (0, 8, "PERSON", "pii-gliner"),
            (20, 40, "API_KEY", "secrets"),
        ]


# ---------------------------------------------------------------------------
# Cross-runner JSON compatibility + scorer ingestion (integration)
# ---------------------------------------------------------------------------


class TestRunnerJsonInterop:
    def test_both_runners_emit_scorer_ingestible_json(self, tmp_path: Path) -> None:
        text = "Contact John Doe at john@x.com"
        from screencap.privacy import normalize_text
        from tests.privacy.fixtures.test_corpus import CorpusCase, ExpectedEntity

        norm = normalize_text(text)
        js, je = norm.index("John Doe"), norm.index("John Doe") + len("John Doe")
        es, ee = norm.index("john@x.com"), norm.index("john@x.com") + len("john@x.com")

        gold = [
            CorpusCase(
                id="b1",
                description="interop",
                text=text,
                expected=[
                    ExpectedEntity("PERSON", "John Doe"),
                    ExpectedEntity("EMAIL", "john@x.com"),
                ],
            )
        ]

        # privacy-filter side (native labels)
        pf = PredictionFile(
            model="privacy-filter",
            tier="smoke",
            predictions=[
                CasePrediction(
                    "b1",
                    [
                        PredictedSpan(js, je, "private_person"),
                        PredictedSpan(es, ee, "private_email"),
                    ],
                )
            ],
        )
        # GLiNER side (EntityType labels, as the production path emits)
        gl = PredictionFile(
            model="gliner",
            tier="smoke",
            predictions=[
                CasePrediction(
                    "b1",
                    [
                        PredictedSpan(js, je, "PERSON", source="pii-gliner"),
                        PredictedSpan(es, ee, "EMAIL", source="pii-gliner"),
                    ],
                )
            ],
        )

        pf_path = tmp_path / "pf.json"
        gl_path = tmp_path / "gl.json"
        pf.save(pf_path)
        gl.save(gl_path)

        # Round-trips through JSON and both score to perfect on this case.
        for path in (pf_path, gl_path):
            loaded = PredictionFile.load(path)
            agg = scorer.score_prediction_file(loaded, gold)
            assert agg.per_type["PERSON"].true_positives == 1
            assert agg.per_type["EMAIL"].true_positives == 1
            assert agg.total_fp == 0
