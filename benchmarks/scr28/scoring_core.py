"""Model-free PII benchmark scoring core (non-test module).

Extracted from ``tests/privacy/test_benchmark.py`` so non-test callers
(``benchmarks/scr28/scorer.py``, the spike runners) can reuse the scoring logic
without importing a pytest-collected module. ``test_benchmark.py`` re-exports
these names, so its ``run_benchmark`` wrapper and existing tests keep working;
the corpus fixtures (``CorpusCase``, ``ExpectedEntity``, ``TRUE_POSITIVE_CASES``,
``FALSE_POSITIVE_CASES``) stay in ``tests/privacy/fixtures/test_corpus.py`` —
they are test data, only the scoring logic moved here.

Measures: exact-span recall, partial-overlap recall, span coverage ratio,
document-level leak rate, FP count by entity type and detector source,
precision/recall/F1, and end-to-end redaction survival.

This module is pure-Python (``screencap.privacy`` + the corpus dataclasses) with
no pytest dependency, so it imports cleanly in the repo env where the scorer runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from screencap.privacy import Anonymizer, Detection, DetectionResult
from tests.privacy.fixtures.test_corpus import (
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
    ExpectedEntity,
    Frequency,
)

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
        """Exact-span recall = exact_hits / total_expected.

        Denominator includes ALL expected entities (exact + partial-only + missed),
        not just exact + missed. This avoids inflating the metric when many
        detections are partial-but-not-exact.
        """
        total_expected = self.partial_overlap_hits + self.false_negatives
        return self.exact_span_hits / total_expected if total_expected else 0.0

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
        """Exact-span recall = exact_hits / total_expected."""
        total_expected = self.total_partial_overlap + self.total_fn
        return self.total_exact_span / total_expected if total_expected else 0.0

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
# Scoring core
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

    A detection must match on BOTH entity_type AND span overlap to count.
    Optional source filter further restricts which detectors are considered.
    """
    exp_len = len(expected.substring)
    if not exp_len:
        return False, False, 0.0

    # Map expected entity_type to the set of compatible detection types.
    # Some entity types are detected under related names depending on the
    # detector (e.g., SECRET vs API_KEY), so we allow the expected type
    # itself as the only match — extend this set if aliases are needed.
    exp_type = expected.entity_type

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
            if det.entity_type != exp_type:
                continue
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

    A detection matches if:
    1. Same entity_type AND overlapping span, OR
    2. Detection span is fully contained within an expected span
       (handles nested components like PASSWORD inside CONNECTION_STRING)
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
                # Same type: direct match
                if det.entity_type == exp.entity_type:
                    return True
                # Different type but fully contained: nested component
                # (e.g., PASSWORD inside CONNECTION_STRING)
                if det.start >= exp_start and det.end <= exp_end:
                    return True
            offset = idx + 1
    return False


def score_predictions(
    true_positive_cases: list[CorpusCase],
    false_positive_cases: list[CorpusCase],
    predictions_by_case: dict[str, DetectionResult],
    *,
    anonymizer: Anonymizer | None = None,
) -> AggregateResult:
    """Grade pre-computed detections against gold cases.

    This is the scoring core of :func:`run_benchmark`, decoupled from the model
    that produced the detections. Any source of predicted spans — a live
    ``DetectionPipeline``, a JSON file emitted by an out-of-process model runner,
    etc. — can be scored identically by handing it ``predictions_by_case``.

    ``predictions_by_case`` maps a ``CorpusCase.id`` to the ``DetectionResult``
    (normalized text + detections) for that case. A case absent from the mapping
    is treated as having produced no detections over its own (un-normalized) text,
    so every gold entity in it becomes a false negative rather than crashing.

    Behavior is identical to the original inline scoring in ``run_benchmark``:
    span-overlap matching with exact/partial distinction and coverage ratios,
    extra detections on TP cases counted as FPs, redaction-survival check, and
    document-leak counting; FP cases contribute frequency-weighted FP impact.
    """
    agg = AggregateResult()
    anonymizer = anonymizer or Anonymizer()

    def _result_for(tc: CorpusCase) -> DetectionResult:
        return predictions_by_case.get(
            tc.id, DetectionResult(normalized_text=tc.text, detections=[]),
        )

    # --- True positive evaluation ---
    for tc in true_positive_cases:
        result = _result_for(tc)
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
    for tc in false_positive_cases:
        result = _result_for(tc)
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
