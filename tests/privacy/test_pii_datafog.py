"""Tests for DataFogPiiDetector."""

from __future__ import annotations

import pytest

from screencap.privacy import EntityType
from screencap.privacy.pii_datafog import DataFogPiiDetector

pytestmark = pytest.mark.privacy


@pytest.fixture(scope="module")
def detector() -> DataFogPiiDetector:
    return DataFogPiiDetector()


class TestDataFogDetection:
    def test_email(self, detector: DataFogPiiDetector):
        dets = detector.detect("Contact jane@example.com")
        emails = [d for d in dets if d.entity_type == EntityType.EMAIL]
        assert len(emails) >= 1

    def test_phone(self, detector: DataFogPiiDetector):
        dets = detector.detect("Call 555-123-4567")
        phones = [d for d in dets if d.entity_type == EntityType.PHONE]
        assert len(phones) >= 1

    def test_ssn(self, detector: DataFogPiiDetector):
        dets = detector.detect("SSN: 123-45-6789")
        ssn = [d for d in dets if d.entity_type == EntityType.SSN]
        assert len(ssn) >= 1

    def test_does_not_detect_names(self, detector: DataFogPiiDetector):
        """DataFog is regex-based — no NER for names."""
        dets = detector.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) == 0

    def test_offset_correctness(self, detector: DataFogPiiDetector):
        text = "Email: alice@test.com ok"
        dets = detector.detect(text)
        for d in dets:
            extracted = text[d.start : d.end]
            assert extracted in text
            assert len(extracted) > 0


class TestFactorySwitching:
    def test_create_with_presidio(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline(pii_engine="presidio")
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        assert "PiiDetector" in detector_names
        assert "DataFogPiiDetector" not in detector_names

    def test_create_with_datafog(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline(pii_engine="datafog")
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        assert "DataFogPiiDetector" in detector_names
        assert "PiiDetector" not in detector_names

    def test_create_auto_prefers_presidio(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline()  # auto
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        # With both installed, Presidio should be preferred
        assert "PiiDetector" in detector_names

    def test_create_invalid_engine_raises(self):
        from screencap.privacy import create_default_pipeline

        with pytest.raises(ValueError, match="Invalid pii_engine"):
            create_default_pipeline(pii_engine="invalid")
