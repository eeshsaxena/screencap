"""PII detection benchmark with strict metrics.

Measures: exact-span recall, partial-overlap recall, span coverage ratio,
document-level leak rate, FP count by entity type and detector source,
precision/recall/F1, and end-to-end redaction survival.

The scoring logic itself lives in the sibling module
``tests/privacy/scoring_core.py`` and is re-exported here so ``run_benchmark``
and the existing tests keep working unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from screencap.redaction import DetectionPipeline
from tests.redaction.fixtures.test_corpus import (
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
)
from tests.redaction.scoring_core import (
    _FREQ_WEIGHT,
    AggregateResult,
    BenchmarkResult,
    _detection_matches_expected,
    _find_detection,
    print_benchmark_table,
    save_benchmark_json,
    score_predictions,
)

pytestmark = pytest.mark.privacy

__all__ = [
    "_FREQ_WEIGHT",
    "AggregateResult",
    "BenchmarkResult",
    "_detection_matches_expected",
    "_find_detection",
    "print_benchmark_table",
    "run_benchmark",
    "save_benchmark_json",
    "score_predictions",
]


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def run_benchmark(pipeline: DetectionPipeline) -> AggregateResult:
    """Run the full benchmark and return aggregate results.

    Thin wrapper: run ``pipeline`` over the corpus to produce detections, then
    grade them via :func:`score_predictions`. Kept behavior-identical so existing
    callers (``benchmark_pii.py``, the privacy test suite) see unchanged numbers.
    """
    predictions_by_case = {
        tc.id: pipeline.detect(tc.text)
        for tc in (*TRUE_POSITIVE_CASES, *FALSE_POSITIVE_CASES)
    }
    return score_predictions(
        TRUE_POSITIVE_CASES, FALSE_POSITIVE_CASES, predictions_by_case,
    )


def _build_pipeline() -> DetectionPipeline:
    from screencap.redaction import create_default_pipeline

    return create_default_pipeline()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline() -> DetectionPipeline:
    """Full pipeline with all three detectors."""
    return _build_pipeline()


@pytest.fixture(scope="module")
def benchmark_results(pipeline: DetectionPipeline) -> AggregateResult:
    """Cached benchmark results — run_benchmark() called once per session."""
    return run_benchmark(pipeline)


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPipelineBenchmark:
    """Comprehensive benchmark against the full corpus."""

    def test_benchmark_report(self, benchmark_results: AggregateResult) -> None:
        """Generate full benchmark report. Always passes — metrics are informational."""
        print_benchmark_table(benchmark_results)

    def test_recall_minimum(self, benchmark_results: AggregateResult) -> None:
        """Hard gate: partial-overlap recall >= 95%."""
        recall = benchmark_results.recall_partial
        assert recall >= 0.95, (
            f"Partial-overlap recall {recall:.1%} < 95%. "
            f"FN={benchmark_results.total_fn}, TP={benchmark_results.total_tp}"
        )

    def test_precision_minimum(self, benchmark_results: AggregateResult) -> None:
        """Hard gate: precision >= 80%."""
        precision = benchmark_results.precision
        assert precision >= 0.80, (
            f"Precision {precision:.1%} < 80%. "
            f"FP={benchmark_results.total_fp}, TP={benchmark_results.total_tp}"
        )

    def test_document_leak_rate(self, benchmark_results: AggregateResult) -> None:
        """Hard gate: document-level leak rate <= 5%."""
        leak_rate = benchmark_results.document_leak_rate
        assert leak_rate <= 0.05, (
            f"Document leak rate {leak_rate:.1%} > 5%. "
            f"Leaks={benchmark_results.document_leaks}/{benchmark_results.document_total}"
        )

    def test_redaction_survival(self, benchmark_results: AggregateResult) -> None:
        """Hard gate: redaction survival rate < 5%.

        If PII substrings survive in the anonymized output, the whole
        pipeline is failing at its job regardless of detection metrics.
        """
        rate = benchmark_results.redaction_survival_rate
        assert rate < 0.05, (
            f"Redaction survival rate {rate:.1%} >= 5%. "
            f"{benchmark_results.redaction_survivals}/{benchmark_results.redaction_total} PII strings survived anonymization."
        )

    def test_save_results_json(self, benchmark_results: AggregateResult, tmp_path: Path) -> None:
        """Verify JSON round-trip preserves top-level structure and TP count."""
        out = tmp_path / "benchmark.json"
        save_benchmark_json(benchmark_results, out)

        loaded = json.loads(out.read_text())
        assert {"per_type", "totals", "fp_by_source"} <= loaded.keys()
        assert loaded["totals"]["true_positives"] == benchmark_results.total_tp
