"""Tests for privacy core: DetectionPipeline, Anonymizer, normalize_text, _merge_detections."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from screencap.privacy import (
    AllDetectorsFailedError,
    Anonymizer,
    Detection,
    DetectionPipeline,
    DetectionResult,
    _merge_detections,
    are_nlp_models_cached,
    create_default_pipeline,
    normalize_text,
)
from screencap.privacy.filters import HeuristicFilter
from screencap.privacy.resolver import DetectionResolver

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------


class TestNormalizeText:
    @pytest.mark.parametrize(
        "input_text, expected",
        [
            ("\uff21BC", "ABC"),                # NFKC fullwidth → ASCII
            ("p\u200bassword", "password"),      # Zero-width space
            ("te\u200cst", "test"),              # Zero-width non-joiner
            ("te\u200dst", "test"),              # Zero-width joiner
            ("te\ufeffst", "test"),              # BOM
            ("te\u2060st", "test"),              # Word joiner
            ("te\u00adst", "test"),              # Soft hyphen
        ],
        ids=["nfkc", "zwsp", "zwnj", "zwj", "bom", "word-joiner", "soft-hyphen"],
    )
    def test_normalization(self, input_text: str, expected: str):
        assert normalize_text(input_text) == expected

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
            Detection("PHONE", 20, 30, 0.8, "pii-presidio"),
        ]
        result = _merge_detections(dets)
        assert len(result) == 2

    def test_dedup_identical_spans(self):
        d1 = Detection("EMAIL", 5, 25, 0.8, "regex")
        d2 = Detection("EMAIL", 5, 25, 0.95, "pii-presidio")
        result = _merge_detections([d1, d2])
        assert len(result) == 1
        assert result[0].score == 0.95
        assert result[0].source == "pii-presidio"

    def test_nested_broader_wins(self):
        outer = Detection("CONNECTION_STRING", 0, 50, 0.9, "regex")
        inner = Detection("PASSWORD", 15, 30, 0.95, "secrets")
        result = _merge_detections([outer, inner])
        assert len(result) == 1
        assert result[0].start == 0
        assert result[0].end == 50

    def test_partial_overlap_union(self):
        d1 = Detection("EMAIL", 5, 25, 0.9, "pii-presidio")
        d2 = Detection("API_KEY", 20, 45, 0.85, "secrets")
        result = _merge_detections([d1, d2])
        assert len(result) == 1
        assert result[0].start == 5
        assert result[0].end == 45
        # Higher score wins entity type
        assert result[0].entity_type == "EMAIL"

    def test_adjacent_not_merged(self):
        d1 = Detection("EMAIL", 0, 10, 0.9, "regex")
        d2 = Detection("PHONE", 10, 20, 0.8, "pii-presidio")
        result = _merge_detections([d1, d2])
        assert len(result) == 2

    def test_unsorted_input(self):
        d1 = Detection("PHONE", 20, 30, 0.8, "pii-presidio")
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
    def test_no_detectors_raises(self):
        with pytest.raises(ValueError, match="requires at least one detector"):
            DetectionPipeline([])

    @pytest.mark.parametrize("text", ["abc", "", "hi"], ids=["3-chars", "empty", "2-chars"])
    def test_short_text_skips_detection(self, text: str):
        det = _FakeDetector([Detection("EMAIL", 0, 3, 0.9, "fake")])
        pipeline = DetectionPipeline([det])
        result = pipeline.detect(text)
        assert result.detections == []

    def test_normalized_text_returned(self):
        """Pipeline returns the normalized text that offsets refer to."""

        class CapturingDetector:
            def detect(self, text: str) -> list[Detection]:
                return []

        pipeline = DetectionPipeline([CapturingDetector()])
        result = pipeline.detect("p\u200bassword test")
        assert result.normalized_text == "password test"

    def test_multiple_detectors_merged(self):
        d1 = _FakeDetector([Detection("EMAIL", 0, 16, 0.9, "d1")])
        d2 = _FakeDetector([Detection("PERSON", 20, 28, 0.8, "d2")])
        pipeline = DetectionPipeline([d1, d2])
        result = pipeline.detect("test@example.com    John Doe rest of text")
        assert len(result.detections) == 2

    def test_detector_failure_continues(self):
        failing = _FakeDetector(error=RuntimeError("boom"))
        working = _FakeDetector([Detection("EMAIL", 0, 16, 0.9, "ok")])
        pipeline = DetectionPipeline([failing, working])
        result = pipeline.detect("test@example.com text")
        assert len(result.detections) == 1
        assert "_FakeDetector" in pipeline.last_errors
        assert "RuntimeError" in pipeline.last_errors["_FakeDetector"]

    def test_all_detectors_fail_raises(self):
        f1 = _FakeDetector(error=RuntimeError("boom1"))
        f2 = _FakeDetector(error=ValueError("boom2"))
        pipeline = DetectionPipeline([f1, f2])
        with pytest.raises(AllDetectorsFailedError):
            pipeline.detect("some text that is long enough")

    def test_pipeline_with_resolver(self):
        """Pipeline uses resolver instead of _merge_detections when provided."""
        d1 = _FakeDetector([Detection("PERSON", 0, 5, 0.8, "pii-gliner")])
        d2 = _FakeDetector([Detection("EMAIL", 3, 20, 0.9, "regex")])
        resolver = DetectionResolver()
        pipeline = DetectionPipeline([d1, d2], resolver=resolver)
        result = pipeline.detect("test text that is long enough for detection")
        # Partial overlap from different sources → both kept (resolver behavior)
        assert len(result.detections) == 2

    def test_pipeline_with_filter(self):
        """Pipeline applies filters after detection."""
        # "Mar" as PERSON should be rejected by HeuristicFilter
        d1 = _FakeDetector([Detection("PERSON", 0, 3, 0.8, "pii-gliner")])
        hf = HeuristicFilter()
        pipeline = DetectionPipeline([d1], filters=[hf])
        result = pipeline.detect("Mar 12 10:30 text")
        # "Mar" is < 4 chars AND a month name → filtered out
        assert len(result.detections) == 0

    def test_pipeline_with_resolver_and_filter(self):
        """Full pipeline: detectors → resolver → filter."""
        # "Mar" PERSON should be rejected, real EMAIL should survive
        d1 = _FakeDetector([
            Detection("PERSON", 0, 3, 0.8, "pii-gliner"),
            Detection("EMAIL", 4, 24, 0.95, "regex"),
        ])
        resolver = DetectionResolver()
        hf = HeuristicFilter()
        pipeline = DetectionPipeline([d1], filters=[hf], resolver=resolver)
        result = pipeline.detect("Mar test@example.com more text")
        assert len(result.detections) == 1
        assert result.detections[0].entity_type == "EMAIL"

    def test_pipeline_filter_failure_graceful(self):
        """If a filter raises, pipeline skips it and returns unfiltered."""

        class BrokenFilter:
            def filter(self, text: str, detections: list[Detection]) -> list[Detection]:
                raise RuntimeError("filter broke")

        d1 = _FakeDetector([Detection("EMAIL", 0, 20, 0.9, "regex")])
        pipeline = DetectionPipeline([d1], filters=[BrokenFilter()])
        result = pipeline.detect("test@example.com text")
        # Filter failed → detections pass through unfiltered
        assert len(result.detections) == 1

    def test_pipeline_without_resolver_uses_legacy_merge(self):
        """Without resolver, pipeline falls back to _merge_detections."""
        d1 = _FakeDetector([
            Detection("PERSON", 0, 5, 0.8, "pii-gliner"),
            Detection("EMAIL", 3, 20, 0.9, "regex"),
        ])
        pipeline = DetectionPipeline([d1])  # no resolver
        result = pipeline.detect("test text that is long enough for detection")
        # Legacy merge unions overlapping spans
        assert len(result.detections) == 1


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------


class TestAnonymizer:
    def test_no_detections(self, anonymizer: Anonymizer):
        assert anonymizer.anonymize("hello world", []) == "hello world"
        assert anonymizer.anonymize("", []) == ""

    def test_single_replacement(self, anonymizer: Anonymizer):
        text = "my email is test@example.com ok"
        dets = [Detection("EMAIL", 12, 28, 0.9, "regex")]
        result = anonymizer.anonymize(text, dets)
        assert result == "my email is <EMAIL> ok"

    def test_multiple_replacements(self, anonymizer: Anonymizer):
        text = "John Doe test@example.com"
        dets = [
            Detection("PERSON", 0, 8, 0.9, "pii-presidio"),
            Detection("EMAIL", 9, 25, 0.9, "regex"),
        ]
        result = anonymizer.anonymize(text, dets)
        assert result == "<PERSON> <EMAIL>"

    def test_adjacent_replacements(self, anonymizer: Anonymizer):
        """Adjacent entities with no gap — offsets must not corrupt each other."""
        text = "John Doejane@example.com"
        dets = [
            Detection("PERSON", 0, 8, 0.9, "pii-presidio"),
            Detection("EMAIL", 8, 24, 0.9, "regex"),
        ]
        result = anonymizer.anonymize(text, dets)
        assert result == "<PERSON><EMAIL>"

    def test_entire_text_is_entity(self, anonymizer: Anonymizer):
        text = "sk-abc123"
        dets = [Detection("API_KEY", 0, 9, 0.95, "secrets")]
        result = anonymizer.anonymize(text, dets)
        assert result == "<API_KEY>"

    def test_out_of_bounds_skipped(self, anonymizer: Anonymizer):
        text = "short"
        dets = [Detection("EMAIL", 0, 100, 0.9, "regex")]
        result = anonymizer.anonymize(text, dets)
        assert result == "short"  # skipped

    def test_overlapping_detections_merged(self, anonymizer: Anonymizer):
        text = "x" * 50
        dets = [
            Detection("EMAIL", 5, 25, 0.9, "pii-presidio"),
            Detection("API_KEY", 20, 45, 0.85, "secrets"),
        ]
        result = anonymizer.anonymize(text, dets)
        # Should merge and produce single replacement
        assert "<EMAIL>" in result or "<API_KEY>" in result
        assert result.count("<") == 1  # only one tag


# ---------------------------------------------------------------------------
# NLP model availability gating
# ---------------------------------------------------------------------------


class TestNlpModelAvailabilityGating:
    """Comprehensive test for are_nlp_models_cached() and require_pii parameter.

    Uses real directory structures in tmp_path instead of excessive mocking.
    Walks through all cache states in a single test, then verifies
    create_default_pipeline(require_pii=...) behavior.
    """

    def test_nlp_model_availability_gating(self, tmp_path: Path, monkeypatch):
        hub_dir = tmp_path / "hub"
        hub_dir.mkdir()
        model_dir = hub_dir / "models--knowledgator--gliner-pii-base-v1.0"
        monkeypatch.setenv("HF_HOME", str(tmp_path))

        # 1. Empty cache dir → False
        assert are_nlp_models_cached() is False

        # 2. GLiNER dir with blobs/ but no snapshots/ → False
        model_dir.mkdir()
        blobs = model_dir / "blobs"
        blobs.mkdir()
        assert are_nlp_models_cached() is False

        # 3. Add snapshots/ but put a .incomplete file in blobs/ → False
        (model_dir / "snapshots").mkdir()
        (blobs / "abc123.incomplete").touch()
        assert are_nlp_models_cached() is False

        # 4. Remove .incomplete but blobs/ is empty → False
        (blobs / "abc123.incomplete").unlink()
        assert are_nlp_models_cached() is False

        # 5. Add a real blob file, but spaCy missing → False
        (blobs / "abc123").write_bytes(b"model data")
        with patch("importlib.util.find_spec", return_value=None):
            assert are_nlp_models_cached() is False

        # 6. Both present → True
        with patch("importlib.util.find_spec", return_value=object()):  # any truthy value
            assert are_nlp_models_cached() is True

        # --- require_pii on create_default_pipeline ---

        # 6. PiiDetector unavailable: require_pii=False succeeds, True raises
        with patch("screencap.privacy.pii.PiiDetector", side_effect=ImportError("no model")):
            # Default (False) — backward-compatible, returns pipeline with regex+secrets
            pipeline = create_default_pipeline()
            assert pipeline is not None

            # Explicit False — same behavior
            pipeline = create_default_pipeline(require_pii=False)
            assert pipeline is not None

            # require_pii=True — must raise
            with pytest.raises(ImportError, match="PII detection required"):
                create_default_pipeline(require_pii=True)
