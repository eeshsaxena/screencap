"""Tests for DetectionResolver — priority-based span resolution."""

from __future__ import annotations

import pytest

from screencap.privacy import Detection
from screencap.privacy.resolver import DetectionResolver

pytestmark = pytest.mark.privacy


class TestDetectionResolver:
    @pytest.fixture()
    def resolver(self) -> DetectionResolver:
        return DetectionResolver()

    def test_no_detections_returns_empty(self, resolver: DetectionResolver):
        assert resolver.resolve([]) == []

    def test_single_detection_passthrough(self, resolver: DetectionResolver):
        det = Detection("EMAIL", 0, 20, 0.9, "regex")
        result = resolver.resolve([det])
        assert result == [det]

    def test_exact_dedup_keeps_higher_priority(self, resolver: DetectionResolver):
        """Same (start, end) — secrets (pri=40) beats pii-gliner (pri=20)."""
        low = Detection("EMAIL", 5, 25, 0.95, "pii-gliner")
        high = Detection("EMAIL", 5, 25, 0.80, "secrets")
        result = resolver.resolve([low, high])
        assert len(result) == 1
        assert result[0].source == "secrets"

    def test_exact_dedup_same_priority_uses_score(self, resolver: DetectionResolver):
        """Same (start, end), same source — keep higher score."""
        d1 = Detection("EMAIL", 5, 25, 0.80, "pii-gliner")
        d2 = Detection("EMAIL", 5, 25, 0.95, "pii-gliner")
        result = resolver.resolve([d1, d2])
        assert len(result) == 1
        assert result[0].score == 0.95

    def test_identical_span_different_type_uses_priority(self, resolver: DetectionResolver):
        """Same span, different types — higher priority source wins."""
        low = Detection("PERSON", 0, 10, 0.9, "pii-presidio")
        high = Detection("SECRET", 0, 10, 0.7, "secrets")
        result = resolver.resolve([low, high])
        assert len(result) == 1
        assert result[0].entity_type == "SECRET"
        assert result[0].source == "secrets"

    def test_nested_compatible_types_kept(self, resolver: DetectionResolver):
        """EMAIL [10:30] nested in CONNECTION_STRING [0:50] — both kept."""
        outer = Detection("CONNECTION_STRING", 0, 50, 0.9, "regex")
        inner = Detection("EMAIL", 10, 30, 0.85, "regex")
        result = resolver.resolve([outer, inner])
        assert len(result) == 2
        types = {d.entity_type for d in result}
        assert types == {"CONNECTION_STRING", "EMAIL"}

    def test_nested_incompatible_keeps_higher_priority(self, resolver: DetectionResolver):
        """PERSON [5:10] nested in EMAIL [0:20] — incompatible, higher pri wins."""
        outer = Detection("EMAIL", 0, 20, 0.9, "regex")  # pri=30
        inner = Detection("PERSON", 5, 10, 0.8, "pii-gliner")  # pri=20
        result = resolver.resolve([outer, inner])
        assert len(result) == 1
        assert result[0].entity_type == "EMAIL"

    def test_partial_overlap_different_sources_kept_separate(self, resolver: DetectionResolver):
        """PERSON [0:5] overlaps EMAIL [3:20] from different sources — keep both."""
        d1 = Detection("PERSON", 0, 5, 0.8, "pii-gliner")
        d2 = Detection("EMAIL", 3, 20, 0.9, "regex")
        result = resolver.resolve([d1, d2])
        assert len(result) == 2

    def test_partial_overlap_same_source_unions(self, resolver: DetectionResolver):
        """Two detections from same source with partial overlap — union."""
        d1 = Detection("PERSON", 0, 10, 0.8, "pii-gliner")
        d2 = Detection("PERSON", 5, 15, 0.9, "pii-gliner")
        result = resolver.resolve([d1, d2])
        assert len(result) == 1
        assert result[0].start == 0
        assert result[0].end == 15
        assert result[0].score == 0.9

    def test_adjacent_spans_not_merged(self, resolver: DetectionResolver):
        """Adjacent (non-overlapping) spans are never merged."""
        d1 = Detection("EMAIL", 0, 10, 0.9, "regex")
        d2 = Detection("PHONE", 10, 20, 0.8, "pii-presidio")
        result = resolver.resolve([d1, d2])
        assert len(result) == 2

    def test_complex_overlapping_chain(self, resolver: DetectionResolver):
        """A [0:10] overlaps B [5:15] overlaps C [12:20] — no giant union.

        A and B from different sources: kept separate.
        B and C from different sources: kept separate.
        Result: all three should be present, not one giant span.
        """
        a = Detection("PERSON", 0, 10, 0.8, "pii-gliner")
        b = Detection("EMAIL", 5, 15, 0.9, "regex")
        c = Detection("PHONE", 12, 20, 0.7, "pii-presidio")
        result = resolver.resolve([a, b, c])
        assert len(result) == 3

    def test_password_inside_connection_string_compatible(self, resolver: DetectionResolver):
        """PASSWORD nested in CONNECTION_STRING — compatible, both kept."""
        outer = Detection("CONNECTION_STRING", 0, 60, 0.9, "regex")
        inner = Detection("PASSWORD", 20, 35, 0.95, "secrets")
        result = resolver.resolve([outer, inner])
        assert len(result) == 2

    def test_api_key_inside_connection_string_compatible(self, resolver: DetectionResolver):
        """API_KEY nested in CONNECTION_STRING — compatible, both kept."""
        outer = Detection("CONNECTION_STRING", 0, 60, 0.9, "regex")
        inner = Detection("API_KEY", 25, 50, 0.9, "secrets")
        result = resolver.resolve([outer, inner])
        assert len(result) == 2
        types = {d.entity_type for d in result}
        assert types == {"CONNECTION_STRING", "API_KEY"}

    def test_unsorted_input_handled(self, resolver: DetectionResolver):
        """Resolver handles unsorted input correctly."""
        d1 = Detection("PHONE", 20, 30, 0.8, "regex")
        d2 = Detection("EMAIL", 0, 10, 0.9, "regex")
        result = resolver.resolve([d1, d2])
        assert result[0].start == 0
        assert result[1].start == 20

    def test_same_source_overlap_not_shadowed_by_cross_source_break(
        self, resolver: DetectionResolver
    ):
        """Same-source overlapping spans must union even with a cross-source span between them.

        Regression: backward-walk used to break on B (different source) before
        seeing C (same source as A), leaving A and C un-merged.
        A[0:10] regex, B[5:12] pii-gliner, C[8:15] regex
        Expected: A+C union [0:15] regex + B[5:12] pii-gliner kept separate.
        """
        a = Detection("EMAIL", 0, 10, 0.9, "regex")
        b = Detection("PERSON", 5, 12, 0.7, "pii-gliner")
        c = Detection("EMAIL", 8, 15, 0.85, "regex")
        result = resolver.resolve([a, b, c])
        # A and C (same source, overlapping) should be unioned to [0:15]
        regex_dets = [d for d in result if d.source == "regex"]
        assert len(regex_dets) == 1
        assert regex_dets[0].start == 0
        assert regex_dets[0].end == 15
        # B (pii-gliner PERSON) is nested inside the union [0:15] EMAIL (regex).
        # PERSON/EMAIL is not compatible nesting, and regex (pri=30) > pii-gliner (pri=20),
        # so B is correctly dropped.
        assert len(result) == 1
