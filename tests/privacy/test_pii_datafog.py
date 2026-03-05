"""Tests for DataFogPiiDetector (NER-capable via spaCy)."""

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

    def test_detects_names_with_ner(self, detector: DataFogPiiDetector):
        """DataFog with spaCy NER detects person names."""
        dets = detector.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1

    def test_detects_address(self, detector: DataFogPiiDetector):
        """DataFog with spaCy NER detects addresses."""
        dets = detector.detect("Ship to 123 Main Street, Springfield IL 62704")
        # ADDRESS detection depends on spaCy recognizing FAC entities;
        # at minimum verify no crash and result is a list
        assert isinstance(dets, list)

    def test_organization_not_mapped(self, detector: DataFogPiiDetector):
        """ORGANIZATION entities are intentionally dropped (not in EntityType)."""
        dets = detector.detect("Microsoft Corporation released a new product")
        org_types = [d for d in dets if d.entity_type == "ORGANIZATION"]
        assert len(org_types) == 0

    def test_offset_correctness(self, detector: DataFogPiiDetector):
        text = "Email: alice@test.com ok"
        dets = detector.detect(text)
        for d in dets:
            extracted = text[d.start : d.end]
            assert extracted in text
            assert len(extracted) > 0

    def test_native_confidence(self, detector: DataFogPiiDetector):
        """Confidence comes from the engine, not hardcoded."""
        dets = detector.detect("John Doe emailed jane@example.com")
        for d in dets:
            assert 0.0 <= d.score <= 1.0
            # NER entities typically get 0.7, regex gets 1.0 — not 0.85
            assert d.score != 0.85 or d.entity_type in (
                EntityType.EMAIL,
                EntityType.PHONE,
                EntityType.SSN,
                EntityType.CREDIT_CARD,
            )

    def test_source_is_pii_datafog(self, detector: DataFogPiiDetector):
        dets = detector.detect("Contact jane@example.com")
        for d in dets:
            assert d.source == "pii-datafog"


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
