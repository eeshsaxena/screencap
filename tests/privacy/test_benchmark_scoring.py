"""Tests for the decoupled scoring core (``score_predictions``) and the SCR-28
spike scorer built on top of it.

Two layers under test:

* ``score_predictions`` — extracted from ``run_benchmark``; grades pre-computed
  detections against gold cases. Tested directly with synthetic ``Detection``s
  and via a model-free equivalence check that pins ``run_benchmark`` to it.
* ``benchmarks/scr28/scorer.py`` — maps native model labels to ``EntityType``,
  merges adjacent same-type spans, then calls ``score_predictions``. Tested
  end-to-end from a ``PredictionFile`` of native labels.

All tests are model-free and fast.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from screencap.privacy import Detection, DetectionResult, normalize_text
from tests.privacy.fixtures.test_corpus import (
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
)
from tests.privacy.test_benchmark import AggregateResult, run_benchmark, score_predictions

# Make the spike modules importable (flat scripts under benchmarks/scr28/).
_SCR28_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "scr28"
if str(_SCR28_DIR) not in sys.path:
    sys.path.insert(0, str(_SCR28_DIR))

import scorer  # noqa: E402
from schema import CasePrediction, PredictedSpan, PredictionFile  # noqa: E402

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _case(
    cid: str,
    text: str,
    expected: list[ExpectedEntity],
    *,
    is_fp: bool = False,
    freq: str | None = None,
) -> CorpusCase:
    return CorpusCase(
        id=cid,
        description=cid,
        text=text,
        expected=expected,
        is_false_positive=is_fp,
        frequency=freq,
    )


def _pred_file(
    model: str,
    cid: str,
    spans: list[tuple[int, int, str]],
) -> PredictionFile:
    """One-case PredictionFile from ``(start, end, native_label)`` tuples."""
    return PredictionFile(
        model=model,
        tier="smoke",
        predictions=[
            CasePrediction(
                cid, [PredictedSpan(s, e, label) for s, e, label in spans]
            )
        ],
    )


def _span(text: str, sub: str) -> tuple[int, int]:
    start = text.index(sub)
    return start, start + len(sub)


def _agg_key(agg: AggregateResult) -> dict:
    """Comparable snapshot of an AggregateResult's counts."""
    return {
        "per_type": {
            k: (
                r.true_positives,
                r.false_positives,
                r.false_negatives,
                r.exact_span_hits,
                r.partial_overlap_hits,
                round(r.coverage_sum, 6),
                r.coverage_count,
            )
            for k, r in sorted(agg.per_type.items())
        },
        "document_leaks": agg.document_leaks,
        "document_total": agg.document_total,
        "fp_by_source": dict(sorted(agg.fp_by_source.items())),
        "weighted_fp_impact": agg.weighted_fp_impact,
        "redaction_survivals": agg.redaction_survivals,
        "redaction_total": agg.redaction_total,
    }


# ---------------------------------------------------------------------------
# score_predictions core — synthetic Detection inputs
# ---------------------------------------------------------------------------


class TestScorePredictionsCore:
    def test_exact_match_is_tp_with_correct_support(self) -> None:
        text = "Contact John Doe today"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John Doe")])
        start, end = _span(text, "John Doe")
        preds = {
            "c1": DetectionResult(
                normalize_text(text),
                [Detection("PERSON", start, end, 0.99, "pii-x")],
            )
        }
        agg = score_predictions([case], [], preds)
        r = agg.per_type["PERSON"]
        assert (r.true_positives, r.false_positives, r.false_negatives) == (1, 0, 0)
        assert r.exact_span_hits == 1
        assert r.precision == 1.0
        assert r.recall_exact == 1.0
        assert r.recall_partial == 1.0
        # support n = partial + fn
        assert r.partial_overlap_hits + r.false_negatives == 1

    def test_partial_overlap_is_partial_not_exact(self) -> None:
        text = "Contact John Doe today"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John Doe")])
        start, _ = _span(text, "John Doe")
        # Detect only "John" — overlaps but not exact.
        preds = {
            "c1": DetectionResult(
                normalize_text(text),
                [Detection("PERSON", start, start + 4, 0.9, "pii-x")],
            )
        }
        agg = score_predictions([case], [], preds)
        r = agg.per_type["PERSON"]
        assert r.partial_overlap_hits == 1
        assert r.exact_span_hits == 0
        assert r.recall_partial == 1.0
        assert r.recall_exact == 0.0

    def test_unmatched_gold_is_fn_unmatched_prediction_is_fp(self) -> None:
        text = "Reach Maria Lopez via email"
        case = _case("c1", text, [ExpectedEntity("PERSON", "Maria Lopez")])
        # No PERSON detected; instead an unrelated EMAIL detection elsewhere.
        es, ee = _span(text, "email")
        preds = {
            "c1": DetectionResult(
                normalize_text(text),
                [Detection("EMAIL", es, ee, 0.8, "pii-x")],
            )
        }
        agg = score_predictions([case], [], preds)
        assert agg.per_type["PERSON"].false_negatives == 1
        assert agg.per_type["PERSON"].true_positives == 0
        assert agg.per_type["EMAIL"].false_positives == 1

    def test_empty_predictions_make_all_gold_fn(self) -> None:
        text = "Contact John Doe today"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John Doe")])
        agg = score_predictions([case], [], {})  # missing case -> empty detections
        assert agg.per_type["PERSON"].false_negatives == 1
        assert agg.total_tp == 0

    def test_predictions_on_fp_case_are_all_fp(self) -> None:
        text = "build output: compiling module foo"
        case = _case("fp1", text, [], is_fp=True, freq="high")
        es, ee = _span(text, "foo")
        preds = {
            "fp1": DetectionResult(
                normalize_text(text),
                [Detection("PERSON", es, ee, 0.5, "pii-x")],
            )
        }
        agg = score_predictions([], [case], preds)
        assert agg.per_type["PERSON"].false_positives == 1
        # frequency=high -> weighted impact multiplier
        assert agg.weighted_fp_impact == 100

    def test_both_empty_no_counts(self) -> None:
        text = "nothing to see"
        case = _case("fp1", text, [], is_fp=True)
        agg = score_predictions([], [case], {})
        assert agg.total_tp == agg.total_fp == agg.total_fn == 0


# ---------------------------------------------------------------------------
# run_benchmark <-> score_predictions equivalence (model-free characterization)
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Deterministic stand-in for a DetectionPipeline (no model)."""

    def __init__(self, fn) -> None:
        self._fn = fn

    def detect(self, text: str) -> DetectionResult:
        return self._fn(text)


class TestRunBenchmarkEquivalence:
    """``run_benchmark`` must be exactly ``score_predictions`` over the same
    detections — the refactor's behavior-preservation guarantee, proven without
    the real model so it never drifts."""

    def _assert_equivalent(self, fn) -> AggregateResult:
        fake = _FakePipeline(fn)
        via_wrapper = run_benchmark(fake)
        preds = {
            tc.id: fake.detect(tc.text)
            for tc in (*TRUE_POSITIVE_CASES, *FALSE_POSITIVE_CASES)
        }
        via_core = score_predictions(TRUE_POSITIVE_CASES, FALSE_POSITIVE_CASES, preds)
        assert _agg_key(via_wrapper) == _agg_key(via_core)
        return via_wrapper

    def test_empty_detector_equivalence_and_invariants(self) -> None:
        agg = self._assert_equivalent(
            lambda text: DetectionResult(normalize_text(text), [])
        )
        # Nothing detected: every gold entity is a miss, no FPs.
        assert agg.total_tp == 0
        assert agg.total_fp == 0
        expected_total = sum(len(tc.expected) for tc in TRUE_POSITIVE_CASES)
        assert agg.total_fn == expected_total
        assert agg.document_leaks == len(TRUE_POSITIVE_CASES)
        # Nothing redacted -> every PII substring "survives".
        assert agg.redaction_survivals == agg.redaction_total == expected_total

    def test_nontrivial_detector_equivalence(self) -> None:
        # A whole-text PERSON span on every case — exercises TP + FP paths.
        def fn(text: str) -> DetectionResult:
            norm = normalize_text(text)
            return DetectionResult(norm, [Detection("PERSON", 0, len(norm), 1.0, "pii-x")])

        self._assert_equivalent(fn)


# ---------------------------------------------------------------------------
# Spike scorer — native-label mapping + adjacent-span merge
# ---------------------------------------------------------------------------


class TestSpikeScorer:
    def test_adjacent_same_type_spans_merge_to_one(self) -> None:
        # "first name" + "last name" emitted as two PERSON-ish spans separated by
        # a space must merge into one and match a single gold PERSON span — not
        # score as one TP + one FP.
        text = "Contact John Doe today"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John Doe")])
        js, je = _span(text, "John")
        ds, de = _span(text, "Doe")
        pf = _pred_file(
            "privacy-filter", "c1", [(js, je, "private_person"), (ds, de, "private_person")]
        )
        agg = scorer.score_prediction_file(pf, [case])
        r = agg.per_type["PERSON"]
        assert (r.true_positives, r.false_positives) == (1, 0)
        assert r.exact_span_hits == 1

    def test_out_of_scope_labels_are_dropped_not_errors(self) -> None:
        # private_url maps to OUT_OF_SCOPE -> excluded from scoring entirely.
        text = "see https://example.com/path for details"
        case = _case("fp1", text, [], is_fp=True)
        us, ue = _span(text, "https://example.com/path")
        pf = _pred_file("privacy-filter", "fp1", [(us, ue, "private_url")])
        agg = scorer.score_prediction_file(pf, [case])
        assert agg.total_fp == 0
        assert agg.total_tp == 0
        assert agg.total_fn == 0

    def test_wrong_type_overlap_is_fp_for_predicted_and_fn_for_gold(self) -> None:
        # A wrong-type prediction that overlaps but is NOT fully contained in the
        # gold span counts as an FP for the predicted type and an FN for the gold
        # type. (Fully-contained wrong-type spans are absorbed as nested
        # components by the reused core — see the dedicated test below.)
        text = "Name John Doe here"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John")])
        js, _ = _span(text, "John")
        _, de = _span(text, "Doe")
        # EMAIL span covering "John Doe" — wider than the gold "John" span.
        pf = _pred_file("privacy-filter", "c1", [(js, de, "private_email")])
        agg = scorer.score_prediction_file(pf, [case])
        assert agg.per_type["PERSON"].false_negatives == 1
        assert agg.per_type["EMAIL"].false_positives == 1

    def test_fully_contained_wrong_type_is_absorbed_as_nested(self) -> None:
        # Documented property of the reused core: a wrong-type detection fully
        # contained within a gold span is treated as a nested component (e.g.
        # PASSWORD inside CONNECTION_STRING), not an FP. The gold PERSON still
        # registers as an FN because no PERSON span was detected.
        text = "Contact John Doe today"
        case = _case("c1", text, [ExpectedEntity("PERSON", "John Doe")])
        s, e = _span(text, "John Doe")
        pf = _pred_file("privacy-filter", "c1", [(s, e, "private_email")])
        agg = scorer.score_prediction_file(pf, [case])
        assert agg.per_type["PERSON"].false_negatives == 1
        assert agg.per_type.get("EMAIL") is None or agg.per_type["EMAIL"].false_positives == 0

    def test_shared_labels_map_to_correct_entity_types(self) -> None:
        text = "John Doe, john@x.com, +1 415 555 1212, 1 Main St"
        case = _case(
            "c1",
            text,
            [
                ExpectedEntity("PERSON", "John Doe"),
                ExpectedEntity("EMAIL", "john@x.com"),
                ExpectedEntity("PHONE", "+1 415 555 1212"),
                ExpectedEntity("ADDRESS", "1 Main St"),
            ],
        )
        spans = [
            (*_span(text, "John Doe"), "private_person"),
            (*_span(text, "john@x.com"), "private_email"),
            (*_span(text, "+1 415 555 1212"), "private_phone"),
            (*_span(text, "1 Main St"), "private_address"),
        ]
        pf = _pred_file("privacy-filter", "c1", spans)
        agg = scorer.score_prediction_file(pf, [case])
        for etype in ("PERSON", "EMAIL", "PHONE", "ADDRESS"):
            assert agg.per_type[etype].true_positives == 1, etype
