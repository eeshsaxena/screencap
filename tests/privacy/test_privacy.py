"""Tests for privacy core: Detection, EntityType, DetectionPipeline, Anonymizer."""

from __future__ import annotations

import pytest

from screencap.privacy import (
    AllDetectorsFailedError,
    Anonymizer,
    Detection,
    DetectionPipeline,
    EntityType,
    _merge_detections,
    normalize_text,
)


# ---------------------------------------------------------------------------
# Detection dataclass
# ---------------------------------------------------------------------------


class TestDetection:
    def test_frozen(self):
        d = Detection("EMAIL", 0, 10, 0.9, "regex")
        with pytest.raises(AttributeError):
            d.start = 5  # type: ignore[misc]

    def test_hashable(self):
        d1 = Detection("EMAIL", 0, 10, 0.9, "regex")
        d2 = Detection("EMAIL", 0, 10, 0.9, "regex")
        assert d1 == d2
        assert len({d1, d2}) == 1

    def test_different_detections_not_equal(self):
        d1 = Detection("EMAIL", 0, 10, 0.9, "regex")
        d2 = Detection("EMAIL", 0, 11, 0.9, "regex")
        assert d1 != d2


# ---------------------------------------------------------------------------
# EntityType
# ---------------------------------------------------------------------------


class TestEntityType:
    def test_all_types_are_strings(self):
        pii = [
            EntityType.PERSON,
            EntityType.EMAIL,
            EntityType.PHONE,
            EntityType.SSN,
            EntityType.CREDIT_CARD,
            EntityType.ADDRESS,
        ]
        secrets = [
            EntityType.API_KEY,
            EntityType.PRIVATE_KEY,
            EntityType.PASSWORD,
            EntityType.JWT,
            EntityType.CONNECTION_STRING,
            EntityType.SECRET,
        ]
        for t in pii + secrets:
            assert isinstance(t, str)

    def test_twelve_types(self):
        types = {
            v
            for k, v in vars(EntityType).items()
            if not k.startswith("_") and isinstance(v, str)
        }
        assert len(types) == 12


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------


class TestNormalizeText:
    def test_nfkc(self):
        # Fullwidth A → A
        assert normalize_text("\uff21BC") == "ABC"

    def test_zero_width_stripped(self):
        assert normalize_text("p\u200bassword") == "password"
        assert normalize_text("te\u200cst") == "test"
        assert normalize_text("te\u200dst") == "test"
        assert normalize_text("te\ufeffst") == "test"

    def test_empty(self):
        assert normalize_text("") == ""


# ---------------------------------------------------------------------------
# _merge_detections
# ---------------------------------------------------------------------------


class TestMergeDetections:
    def test_empty(self):
        assert _merge_detections([]) == []

    def test_no_overlaps(self):
        dets = [
            Detection("EMAIL", 0, 10, 0.9, "regex"),
            Detection("PHONE", 20, 30, 0.8, "pii"),
        ]
        result = _merge_detections(dets)
        assert len(result) == 2

    def test_dedup_identical_spans(self):
        d1 = Detection("EMAIL", 5, 25, 0.8, "regex")
        d2 = Detection("EMAIL", 5, 25, 0.95, "pii")
        result = _merge_detections([d1, d2])
        assert len(result) == 1
        assert result[0].score == 0.95
        assert result[0].source == "pii"

    def test_nested_broader_wins(self):
        outer = Detection("CONNECTION_STRING", 0, 50, 0.9, "regex")
        inner = Detection("PASSWORD", 15, 30, 0.95, "secrets")
        result = _merge_detections([outer, inner])
        assert len(result) == 1
        assert result[0].start == 0
        assert result[0].end == 50

    def test_partial_overlap_union(self):
        d1 = Detection("EMAIL", 5, 25, 0.9, "pii")
        d2 = Detection("API_KEY", 20, 45, 0.85, "secrets")
        result = _merge_detections([d1, d2])
        assert len(result) == 1
        assert result[0].start == 5
        assert result[0].end == 45
        # Higher score wins entity type
        assert result[0].entity_type == "EMAIL"

    def test_adjacent_not_merged(self):
        d1 = Detection("EMAIL", 0, 10, 0.9, "regex")
        d2 = Detection("PHONE", 10, 20, 0.8, "pii")
        result = _merge_detections([d1, d2])
        assert len(result) == 2

    def test_unsorted_input(self):
        d1 = Detection("PHONE", 20, 30, 0.8, "pii")
        d2 = Detection("EMAIL", 0, 10, 0.9, "regex")
        result = _merge_detections([d2, d1])
        assert result[0].start == 0
        assert result[1].start == 20


# ---------------------------------------------------------------------------
# DetectionPipeline
# ---------------------------------------------------------------------------


class _FakeDetector:
    """Deterministic detector for testing."""

    def __init__(self, results: list[Detection] | None = None, error: Exception | None = None):
        self._results = results or []
        self._error = error

    def detect(self, text: str) -> list[Detection]:
        if self._error:
            raise self._error
        return self._results


class TestDetectionPipeline:
    def test_short_circuit(self):
        det = _FakeDetector([Detection("EMAIL", 0, 3, 0.9, "fake")])
        pipeline = DetectionPipeline([det])
        # Text < 4 chars → empty
        assert pipeline.detect("abc") == []

    def test_empty_text(self):
        det = _FakeDetector([Detection("EMAIL", 0, 3, 0.9, "fake")])
        pipeline = DetectionPipeline([det])
        assert pipeline.detect("") == []

    def test_normalization(self):
        """Pipeline normalizes text before passing to detectors."""
        captured_text = []

        class CapturingDetector:
            def detect(self, text: str) -> list[Detection]:
                captured_text.append(text)
                return []

        pipeline = DetectionPipeline([CapturingDetector()])
        pipeline.detect("p\u200bassword test")
        assert captured_text[0] == "password test"

    def test_single_detector(self):
        det = _FakeDetector([Detection("EMAIL", 0, 20, 0.9, "fake")])
        pipeline = DetectionPipeline([det])
        result = pipeline.detect("test@example.com text")
        assert len(result) == 1
        assert result[0].entity_type == "EMAIL"

    def test_multiple_detectors_merged(self):
        d1 = _FakeDetector([Detection("EMAIL", 0, 16, 0.9, "d1")])
        d2 = _FakeDetector([Detection("PERSON", 20, 28, 0.8, "d2")])
        pipeline = DetectionPipeline([d1, d2])
        result = pipeline.detect("test@example.com    John Doe rest of text")
        assert len(result) == 2

    def test_detector_failure_continues(self):
        failing = _FakeDetector(error=RuntimeError("boom"))
        working = _FakeDetector([Detection("EMAIL", 0, 16, 0.9, "ok")])
        pipeline = DetectionPipeline([failing, working])
        result = pipeline.detect("test@example.com text")
        assert len(result) == 1
        assert "_FakeDetector" in pipeline.last_errors
        assert "RuntimeError" in pipeline.last_errors["_FakeDetector"]

    def test_all_detectors_fail_raises(self):
        f1 = _FakeDetector(error=RuntimeError("boom1"))
        f2 = _FakeDetector(error=ValueError("boom2"))
        pipeline = DetectionPipeline([f1, f2])
        with pytest.raises(AllDetectorsFailedError):
            pipeline.detect("some text that is long enough")

    def test_last_errors_reset_each_call(self):
        failing = _FakeDetector(error=RuntimeError("boom"))
        working = _FakeDetector([Detection("EMAIL", 0, 10, 0.9, "ok")])
        pipeline = DetectionPipeline([failing, working])
        pipeline.detect("first call text here")
        assert pipeline.last_errors  # has error
        pipeline.detect("second call text here")
        assert pipeline.last_errors  # still has error from this call
        # But it was reset (same key, fresh dict)
        assert "_FakeDetector" in pipeline.last_errors

    def test_no_detectors_short_text(self):
        pipeline = DetectionPipeline([])
        # Short text → short-circuit before all-fail check
        assert pipeline.detect("ab") == []


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------


class TestAnonymizer:
    def test_no_detections(self, anonymizer: Anonymizer):
        assert anonymizer.anonymize("hello world", []) == "hello world"

    def test_single_replacement(self, anonymizer: Anonymizer):
        text = "my email is test@example.com ok"
        dets = [Detection("EMAIL", 12, 28, 0.9, "regex")]
        result = anonymizer.anonymize(text, dets)
        assert result == "my email is <EMAIL> ok"

    def test_multiple_replacements(self, anonymizer: Anonymizer):
        text = "John Doe test@example.com"
        dets = [
            Detection("PERSON", 0, 8, 0.9, "pii"),
            Detection("EMAIL", 9, 25, 0.9, "regex"),
        ]
        result = anonymizer.anonymize(text, dets)
        assert result == "<PERSON> <EMAIL>"

    def test_entire_text_is_entity(self, anonymizer: Anonymizer):
        text = "sk-abc123"
        dets = [Detection("API_KEY", 0, 9, 0.95, "secrets")]
        result = anonymizer.anonymize(text, dets)
        assert result == "<API_KEY>"

    def test_empty_text(self, anonymizer: Anonymizer):
        assert anonymizer.anonymize("", []) == ""

    def test_out_of_bounds_skipped(self, anonymizer: Anonymizer):
        text = "short"
        dets = [Detection("EMAIL", 0, 100, 0.9, "regex")]
        result = anonymizer.anonymize(text, dets)
        assert result == "short"  # skipped

    def test_overlapping_detections_merged(self, anonymizer: Anonymizer):
        text = "x" * 50
        dets = [
            Detection("EMAIL", 5, 25, 0.9, "pii"),
            Detection("API_KEY", 20, 45, 0.85, "secrets"),
        ]
        result = anonymizer.anonymize(text, dets)
        # Should merge and produce single replacement
        assert "<EMAIL>" in result or "<API_KEY>" in result
        assert result.count("<") == 1  # only one tag
