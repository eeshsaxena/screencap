"""PII detection benchmark with strict metrics.

Measures: exact-span recall, exact-type recall, partial-overlap recall,
document-level leak rate, FP count by entity type, precision/recall/F1.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

from screencap.privacy import DetectionPipeline, normalize_text
from screencap.privacy.pii import PiiDetector
from screencap.privacy.regex import RegexDetector
from screencap.privacy.secrets import DetectSecretsDetector
from tests.privacy.fixtures.test_corpus import (
    ALL_TEST_CASES,
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class BenchmarkResult:
    """Per-entity-type metrics."""

    entity_type: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    exact_span_hits: int = 0
    partial_overlap_hits: int = 0

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 0.0

    @property
    def recall_exact(self) -> float:
        denom = self.exact_span_hits + self.false_negatives
        return self.exact_span_hits / denom if denom else 0.0

    @property
    def recall_partial(self) -> float:
        denom = self.partial_overlap_hits + self.false_negatives
        return self.partial_overlap_hits / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall_partial
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class AggregateResult:
    """Aggregated metrics across all entity types."""

    per_type: dict[str, BenchmarkResult] = field(default_factory=dict)
    document_leaks: int = 0
    document_total: int = 0

    @property
    def total_tp(self) -> int:
        return sum(r.true_positives for r in self.per_type.values())

    @property
    def total_fp(self) -> int:
        return sum(r.false_positives for r in self.per_type.values())

    @property
    def total_fn(self) -> int:
        return sum(r.false_negatives for r in self.per_type.values())

    @property
    def total_exact_span(self) -> int:
        return sum(r.exact_span_hits for r in self.per_type.values())

    @property
    def total_partial_overlap(self) -> int:
        return sum(r.partial_overlap_hits for r in self.per_type.values())

    @property
    def precision(self) -> float:
        denom = self.total_tp + self.total_fp
        return self.total_tp / denom if denom else 0.0

    @property
    def recall_partial(self) -> float:
        denom = self.total_partial_overlap + self.total_fn
        return self.total_partial_overlap / denom if denom else 0.0

    @property
    def recall_exact(self) -> float:
        denom = self.total_exact_span + self.total_fn
        return self.total_exact_span / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall_partial
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def document_leak_rate(self) -> float:
        return self.document_leaks / self.document_total if self.document_total else 0.0


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def _find_detection(
    normalized: str,
    detections: list,
    expected: ExpectedEntity,
) -> tuple[bool, bool]:
    """Check if an expected entity was detected.

    Returns (exact_match, partial_match).
    """
    idx = normalized.find(expected.substring)
    if idx == -1:
        return False, False

    exp_start = idx
    exp_end = idx + len(expected.substring)

    exact = False
    partial = False

    for det in detections:
        # Check source constraint if specified
        if expected.source and det.source != expected.source:
            continue

        # Partial overlap
        if det.start < exp_end and det.end > exp_start:
            partial = True
            # Exact span match
            if det.start == exp_start and det.end == exp_end:
                exact = True

    return exact, partial


def _classify_fp_detections(
    fp_cases: list[CorpusCase],
    pipeline: DetectionPipeline,
) -> dict[str, int]:
    """Count false positive detections by entity type."""
    fp_by_type: dict[str, int] = {}
    for tc in fp_cases:
        result = pipeline.detect(tc.text)
        for det in result.detections:
            fp_by_type[det.entity_type] = fp_by_type.get(det.entity_type, 0) + 1
    return fp_by_type


def run_benchmark(pipeline: DetectionPipeline) -> AggregateResult:
    """Run the full benchmark and return aggregate results."""
    agg = AggregateResult()

    # True positive evaluation
    for tc in TRUE_POSITIVE_CASES:
        result = pipeline.detect(tc.text)
        normalized = result.normalized_text
        any_missed = False

        for exp in tc.expected:
            etype = exp.entity_type
            if etype not in agg.per_type:
                agg.per_type[etype] = BenchmarkResult(entity_type=etype)

            exact, partial = _find_detection(normalized, result.detections, exp)

            if partial:
                agg.per_type[etype].true_positives += 1
                agg.per_type[etype].partial_overlap_hits += 1
                if exact:
                    agg.per_type[etype].exact_span_hits += 1
            else:
                agg.per_type[etype].false_negatives += 1
                any_missed = True

        if any_missed:
            agg.document_leaks += 1
        agg.document_total += 1

    # False positive evaluation
    fp_by_type = _classify_fp_detections(FALSE_POSITIVE_CASES, pipeline)
    for etype, count in fp_by_type.items():
        if etype not in agg.per_type:
            agg.per_type[etype] = BenchmarkResult(entity_type=etype)
        agg.per_type[etype].false_positives += count

    return agg


def print_benchmark_table(agg: AggregateResult) -> None:
    """Print a markdown-formatted benchmark table."""
    print("\n## PII Benchmark Results\n")
    print("| Entity Type | TP | FP | FN | Exact Recall | Partial Recall | Precision | F1 |")
    print("|---|---|---|---|---|---|---|---|")
    for etype in sorted(agg.per_type):
        r = agg.per_type[etype]
        print(
            f"| {etype} | {r.true_positives} | {r.false_positives} | "
            f"{r.false_negatives} | {r.recall_exact:.1%} | {r.recall_partial:.1%} | "
            f"{r.precision:.1%} | {r.f1:.1%} |"
        )
    print(f"\n**Totals:** TP={agg.total_tp} FP={agg.total_fp} FN={agg.total_fn}")
    print(f"**Precision:** {agg.precision:.1%}  **Partial Recall:** {agg.recall_partial:.1%}  **F1:** {agg.f1:.1%}")
    print(f"**Document Leak Rate:** {agg.document_leaks}/{agg.document_total} = {agg.document_leak_rate:.1%}")
    print(f"\n**FP breakdown:** {dict(sorted(_classify_fp_detections(FALSE_POSITIVE_CASES, _build_pipeline()).items()))}")


def save_benchmark_json(agg: AggregateResult, path: Path) -> None:
    """Save benchmark results to JSON for comparison."""
    data = {
        "per_type": {
            etype: {
                "true_positives": r.true_positives,
                "false_positives": r.false_positives,
                "false_negatives": r.false_negatives,
                "exact_span_hits": r.exact_span_hits,
                "partial_overlap_hits": r.partial_overlap_hits,
                "precision": r.precision,
                "recall_exact": r.recall_exact,
                "recall_partial": r.recall_partial,
                "f1": r.f1,
            }
            for etype, r in sorted(agg.per_type.items())
        },
        "totals": {
            "true_positives": agg.total_tp,
            "false_positives": agg.total_fp,
            "false_negatives": agg.total_fn,
            "precision": agg.precision,
            "recall_partial": agg.recall_partial,
            "recall_exact": agg.recall_exact,
            "f1": agg.f1,
            "document_leak_rate": agg.document_leak_rate,
            "document_leaks": agg.document_leaks,
            "document_total": agg.document_total,
        },
        "fp_cases_count": len(FALSE_POSITIVE_CASES),
        "tp_cases_count": len(TRUE_POSITIVE_CASES),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _build_pipeline() -> DetectionPipeline:
    return DetectionPipeline([
        RegexDetector(),
        DetectSecretsDetector(),
        PiiDetector(),
    ])


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pipeline() -> DetectionPipeline:
    """Full pipeline with all three detectors."""
    return _build_pipeline()


# ---------------------------------------------------------------------------
# Test classes
# ---------------------------------------------------------------------------


class TestPipelineBenchmark:
    """Comprehensive benchmark against the full corpus."""

    def test_benchmark_report(self, pipeline: DetectionPipeline) -> None:
        """Generate full benchmark report. Always passes — metrics are informational."""
        results = run_benchmark(pipeline)
        print_benchmark_table(results)

    def test_recall_minimum(self, pipeline: DetectionPipeline) -> None:
        """Hard gate: partial-overlap recall >= 95%."""
        results = run_benchmark(pipeline)
        recall = results.recall_partial
        assert recall >= 0.95, (
            f"Partial-overlap recall {recall:.1%} < 95%. "
            f"FN={results.total_fn}, TP={results.total_tp}"
        )

    def test_precision_minimum(self, pipeline: DetectionPipeline) -> None:
        """Hard gate: precision >= 80%."""
        results = run_benchmark(pipeline)
        precision = results.precision
        assert precision >= 0.80, (
            f"Precision {precision:.1%} < 80%. "
            f"FP={results.total_fp}, TP={results.total_tp}"
        )

    def test_document_leak_rate(self, pipeline: DetectionPipeline) -> None:
        """Hard gate: document-level leak rate < 5%."""
        results = run_benchmark(pipeline)
        leak_rate = results.document_leak_rate
        assert leak_rate < 0.05, (
            f"Document leak rate {leak_rate:.1%} >= 5%. "
            f"Leaks={results.document_leaks}/{results.document_total}"
        )

    def test_save_results_json(self, pipeline: DetectionPipeline, tmp_path: Path) -> None:
        """Verify JSON output can be saved and re-read."""
        results = run_benchmark(pipeline)
        out = tmp_path / "benchmark.json"
        save_benchmark_json(results, out)

        loaded = json.loads(out.read_text())
        assert "per_type" in loaded
        assert "totals" in loaded
        assert loaded["totals"]["true_positives"] == results.total_tp
