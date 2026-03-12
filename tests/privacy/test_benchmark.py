"""PII detection benchmark with strict metrics.

Measures: exact-span recall, partial-overlap recall, span coverage ratio,
document-level leak rate, FP count by entity type and detector source,
precision/recall/F1, and end-to-end redaction survival.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from screencap.privacy import Anonymizer, Detection, DetectionPipeline
from screencap.privacy.pii import PiiDetector
from screencap.privacy.regex import RegexDetector
from screencap.privacy.secrets import DetectSecretsDetector
from tests.privacy.fixtures.test_corpus import (
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
    Frequency,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

# Frequency multipliers for estimating real-world impact.
# A HIGH-frequency FP pattern (e.g., shell prompts) fires ~100x/hour,
# MEDIUM ~10x/hour, LOW ~1x/hour.
_FREQ_WEIGHT = {Frequency.HIGH: 100, Frequency.MEDIUM: 10, Frequency.LOW: 1}


@dataclass
class BenchmarkResult:
    """Per-entity-type metrics."""

    entity_type: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    exact_span_hits: int = 0
    partial_overlap_hits: int = 0
    coverage_sum: float = 0.0  # Sum of coverage ratios for partial hits
    coverage_count: int = 0  # Number of partial hits (for averaging)

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
    def avg_coverage(self) -> float:
        """Average span coverage ratio for partial hits (0.0-1.0)."""
        return self.coverage_sum / self.coverage_count if self.coverage_count else 0.0

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

    # Per-detector source FP counts (e.g., {"pii-presidio": 5, "regex": 1})
    fp_by_source: dict[str, int] = field(default_factory=dict)
    # Weighted FP impact estimate (FP count * frequency multiplier)
    weighted_fp_impact: float = 0.0
    # Redaction survival: cases where PII substring survived anonymization
    redaction_survivals: int = 0
    redaction_total: int = 0

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
    def avg_coverage(self) -> float:
        total_sum = sum(r.coverage_sum for r in self.per_type.values())
        total_count = sum(r.coverage_count for r in self.per_type.values())
        return total_sum / total_count if total_count else 0.0

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

    @property
    def redaction_survival_rate(self) -> float:
        return self.redaction_survivals / self.redaction_total if self.redaction_total else 0.0


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------


def _find_detection(
    normalized: str,
    detections: list[Detection],
    expected: ExpectedEntity,
) -> tuple[bool, bool, float]:
    """Check if an expected entity was detected.

    Returns (exact_match, partial_match, coverage_ratio).
    Coverage ratio = fraction of the expected span covered by the best detection.
    Checks all occurrences of the substring, not just the first.
    """
    exp_len = len(expected.substring)
    if not exp_len:
        return False, False, 0.0

    best_exact = False
    best_coverage = 0.0
    offset = 0

    while True:
        idx = normalized.find(expected.substring, offset)
        if idx == -1:
            break

        exp_start = idx
        exp_end = idx + exp_len

        for det in detections:
            if expected.source and det.source != expected.source:
                continue

            if det.start < exp_end and det.end > exp_start:
                overlap_start = max(det.start, exp_start)
                overlap_end = min(det.end, exp_end)
                coverage = (overlap_end - overlap_start) / exp_len
                best_coverage = max(best_coverage, coverage)

                if det.start == exp_start and det.end == exp_end:
                    best_exact = True

        # If we found an exact match, no need to check further occurrences
        if best_exact:
            break

        offset = idx + 1

    partial = best_coverage > 0
    return best_exact, partial, best_coverage


def _detection_matches_expected(
    normalized: str,
    det: Detection,
    expected_list: list[ExpectedEntity],
) -> bool:
    """Check if a detection matches any expected entity in the case.

    Checks all occurrences of each expected substring, not just the first.
    """
    for exp in expected_list:
        offset = 0
        while True:
            idx = normalized.find(exp.substring, offset)
            if idx == -1:
                break
            exp_start = idx
            exp_end = idx + len(exp.substring)
            if det.start < exp_end and det.end > exp_start:
                return True
            offset = idx + 1
    return False


def run_benchmark(pipeline: DetectionPipeline) -> AggregateResult:
    """Run the full benchmark and return aggregate results."""
    agg = AggregateResult()
    anonymizer = Anonymizer()

    # --- True positive evaluation ---
    for tc in TRUE_POSITIVE_CASES:
        result = pipeline.detect(tc.text)
        normalized = result.normalized_text
        any_missed = False

        for exp in tc.expected:
            etype = exp.entity_type
            if etype not in agg.per_type:
                agg.per_type[etype] = BenchmarkResult(entity_type=etype)

            exact, partial, coverage = _find_detection(
                normalized, result.detections, exp,
            )

            if partial:
                agg.per_type[etype].true_positives += 1
                agg.per_type[etype].partial_overlap_hits += 1
                agg.per_type[etype].coverage_sum += coverage
                agg.per_type[etype].coverage_count += 1
                if exact:
                    agg.per_type[etype].exact_span_hits += 1
            else:
                agg.per_type[etype].false_negatives += 1
                any_missed = True

        # Count extra (unmatched) detections on TP cases as FPs
        for det in result.detections:
            if not _detection_matches_expected(normalized, det, tc.expected):
                etype = det.entity_type
                if etype not in agg.per_type:
                    agg.per_type[etype] = BenchmarkResult(entity_type=etype)
                agg.per_type[etype].false_positives += 1
                agg.fp_by_source[det.source] = agg.fp_by_source.get(det.source, 0) + 1

        # Redaction survival check: does PII text survive anonymization?
        redacted = anonymizer.anonymize(normalized, result.detections)
        for exp in tc.expected:
            agg.redaction_total += 1
            if exp.substring in redacted:
                agg.redaction_survivals += 1

        if any_missed:
            agg.document_leaks += 1
        agg.document_total += 1

    # --- False positive evaluation ---
    for tc in FALSE_POSITIVE_CASES:
        result = pipeline.detect(tc.text)
        freq_weight = _FREQ_WEIGHT.get(tc.frequency, 1) if tc.frequency else 1

        for det in result.detections:
            etype = det.entity_type
            if etype not in agg.per_type:
                agg.per_type[etype] = BenchmarkResult(entity_type=etype)
            agg.per_type[etype].false_positives += 1
            agg.fp_by_source[det.source] = agg.fp_by_source.get(det.source, 0) + 1
            agg.weighted_fp_impact += freq_weight

    return agg


def print_benchmark_table(agg: AggregateResult) -> None:
    """Print a markdown-formatted benchmark table."""
    print("\n## PII Benchmark Results\n")
    print("| Entity Type | TP | FP | FN | Exact Recall | Partial Recall | Avg Coverage | Precision | F1 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for etype in sorted(agg.per_type):
        r = agg.per_type[etype]
        print(
            f"| {etype} | {r.true_positives} | {r.false_positives} | "
            f"{r.false_negatives} | {r.recall_exact:.1%} | {r.recall_partial:.1%} | "
            f"{r.avg_coverage:.1%} | {r.precision:.1%} | {r.f1:.1%} |"
        )
    print(f"\n**Totals:** TP={agg.total_tp} FP={agg.total_fp} FN={agg.total_fn}")
    print(f"**Precision:** {agg.precision:.1%}  **Partial Recall:** {agg.recall_partial:.1%}  **F1:** {agg.f1:.1%}")
    print(f"**Avg Span Coverage:** {agg.avg_coverage:.1%}")
    print(f"**Document Leak Rate:** {agg.document_leaks}/{agg.document_total} = {agg.document_leak_rate:.1%}")
    print(f"**Redaction Survival Rate:** {agg.redaction_survivals}/{agg.redaction_total} = {agg.redaction_survival_rate:.1%}")
    print(f"**Weighted FP Impact:** {agg.weighted_fp_impact:.0f} (estimated FPs/hour in real recording)")
    print(f"\n**FP by detector source:** {dict(sorted(agg.fp_by_source.items()))}")


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
                "avg_coverage": r.avg_coverage,
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
            "avg_coverage": agg.avg_coverage,
            "f1": agg.f1,
            "document_leak_rate": agg.document_leak_rate,
            "document_leaks": agg.document_leaks,
            "document_total": agg.document_total,
            "redaction_survival_rate": agg.redaction_survival_rate,
            "redaction_survivals": agg.redaction_survivals,
            "redaction_total": agg.redaction_total,
            "weighted_fp_impact": agg.weighted_fp_impact,
        },
        "fp_by_source": dict(sorted(agg.fp_by_source.items())),
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
        """Hard gate: document-level leak rate < 5%."""
        leak_rate = benchmark_results.document_leak_rate
        assert leak_rate < 0.05, (
            f"Document leak rate {leak_rate:.1%} >= 5%. "
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
        """Verify JSON output can be saved and re-read."""
        out = tmp_path / "benchmark.json"
        save_benchmark_json(benchmark_results, out)

        loaded = json.loads(out.read_text())
        assert "per_type" in loaded
        assert "totals" in loaded
        assert "fp_by_source" in loaded
        assert loaded["totals"]["true_positives"] == benchmark_results.total_tp
        assert "avg_coverage" in loaded["totals"]
        assert "redaction_survival_rate" in loaded["totals"]
        assert "weighted_fp_impact" in loaded["totals"]
