"""Tests for the decoupled scoring core (``score_predictions``).

``score_predictions`` — extracted from ``run_benchmark`` — grades pre-computed
detections against gold cases. Tested directly with synthetic ``Detection``s and
via a model-free equivalence check that pins ``run_benchmark`` to it. All tests
are model-free and fast.
"""

from __future__ import annotations

import pytest

from screencap.redaction import Detection, DetectionResult, normalize_text
from tests.redaction.fixtures.test_corpus import (
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
)
from tests.redaction.test_benchmark import AggregateResult, run_benchmark, score_predictions

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
        # Missing-case fallback: no detections -> nothing redacted, so every gold
        # PII substring survives anonymization (the empty-detections invariant).
        assert agg.redaction_total == 1
        assert agg.redaction_survivals == agg.redaction_total

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

