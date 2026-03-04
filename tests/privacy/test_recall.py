"""End-to-end recall/precision benchmark for the privacy pipeline.

Runs the full pipeline against the curated test corpus and asserts:
- Recall >= 95% (of known entities, how many are detected?)
- Precision >= 80% (of detections produced, how many are actual entities?)
"""

from __future__ import annotations

import pytest

from screencap.privacy import Anonymizer, DetectionPipeline, normalize_text
from screencap.privacy.pii import PiiDetector
from screencap.privacy.regex import RegexDetector
from screencap.privacy.secrets import DetectSecretsDetector
from tests.privacy.fixtures.test_corpus import (
    ALL_TEST_CASES,
    FALSE_POSITIVE_CASES,
    TRUE_POSITIVE_CASES,
    CorpusCase,
)

pytestmark = pytest.mark.privacy


@pytest.fixture(scope="module")
def pipeline() -> DetectionPipeline:
    """Full pipeline with all three detectors."""
    return DetectionPipeline([
        RegexDetector(),
        DetectSecretsDetector(),
        PiiDetector(),
    ])


@pytest.fixture(scope="module")
def anonymizer() -> Anonymizer:
    return Anonymizer()


def _entity_found(
    text: str,
    detections: list,
    expected_type: str,
    expected_substring: str,
    *,
    strict_type: bool = False,
) -> bool:
    """Check if an expected entity was detected (fuzzy match on overlap).

    For privacy purposes, detecting the span matters more than the exact
    entity type label. By default (strict_type=False), any overlapping
    detection counts as a hit. Set strict_type=True to require matching
    entity types.
    """
    normalized = normalize_text(text)
    # Find where the expected substring appears in the normalized text
    idx = normalized.find(expected_substring)
    if idx == -1:
        # Try original text
        idx = text.find(expected_substring)
        if idx == -1:
            return False

    expected_start = idx
    expected_end = idx + len(expected_substring)

    for det in detections:
        # Check entity type match if strict
        if strict_type and det.entity_type != expected_type:
            continue
        # Check overlap: detection should overlap with expected span
        if det.start < expected_end and det.end > expected_start:
            return True

    return False


class TestRecallBenchmark:
    def test_recall_above_threshold(self, pipeline: DetectionPipeline):
        """Recall >= 95% on curated true positive test cases."""
        total_expected = 0
        total_found = 0
        misses: list[tuple[str, str, str]] = []

        for tc in TRUE_POSITIVE_CASES:
            text = tc.text
            detections = pipeline.detect(text)

            for exp in tc.expected:
                total_expected += 1
                if _entity_found(text, detections, exp.entity_type, exp.substring):
                    total_found += 1
                else:
                    misses.append((tc.id, exp.entity_type, exp.substring))

        recall = total_found / total_expected if total_expected > 0 else 0
        print(f"\nRecall: {total_found}/{total_expected} = {recall:.1%}")
        if misses:
            print(f"Misses ({len(misses)}):")
            for tc_id, etype, substr in misses:
                print(f"  {tc_id}: {etype} '{substr}'")

        assert recall >= 0.95, (
            f"Recall {recall:.1%} < 95%. "
            f"Missed: {misses}"
        )


class TestPrecisionBenchmark:
    def test_precision_above_threshold(self, pipeline: DetectionPipeline):
        """Precision >= 80% on false positive test cases."""
        total_detections = 0
        false_positives: list[tuple[str, str, int, int]] = []

        for tc in FALSE_POSITIVE_CASES:
            text = tc.text
            detections = pipeline.detect(text)
            for det in detections:
                total_detections += 1
                false_positives.append((
                    tc.id,
                    det.entity_type,
                    det.start,
                    det.end,
                ))

        # Precision for false positive cases: 0 detections = 100% precision
        if false_positives:
            print(f"\nFalse positives ({len(false_positives)}):")
            for tc_id, etype, s, e in false_positives:
                tc = next(t for t in FALSE_POSITIVE_CASES if t.id == tc_id)
                print(f"  {tc_id}: {etype} [{s}:{e}] text={tc.text[s:e]!r}")

        # For a proper precision metric, we need to count true positives too
        total_tp = 0
        for tc in TRUE_POSITIVE_CASES:
            text = tc.text
            detections = pipeline.detect(text)
            for det in detections:
                # Check if this detection matches any expected entity
                for exp in tc.expected:
                    if _entity_found(text, [det], exp.entity_type, exp.substring):
                        total_tp += 1
                        break

        all_detections = total_tp + len(false_positives)
        precision = total_tp / all_detections if all_detections > 0 else 1.0
        print(f"\nPrecision: {total_tp}/{all_detections} = {precision:.1%}")

        assert precision >= 0.80, (
            f"Precision {precision:.1%} < 80%. "
            f"False positives: {false_positives}"
        )


class TestAnonymizerEndToEnd:
    def test_anonymize_mixed(self, pipeline: DetectionPipeline, anonymizer: Anonymizer):
        """End-to-end: detect + anonymize on mixed PII+secrets text."""
        text = "Hi John, your key is AKIAIOSFODNN7EXAMPLE. Call 555-123-4567."
        normalized = normalize_text(text)
        detections = pipeline.detect(text)
        result = anonymizer.anonymize(normalized, detections)

        # Should not contain the original sensitive values
        assert "John" not in result or "<PERSON>" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        # Should contain redaction tags
        assert "<" in result and ">" in result
