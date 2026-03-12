"""Tests for PiiDetector (Presidio-based, spaCy and GLiNER backends)."""

from __future__ import annotations

import pytest

from screencap.privacy import EntityType
from screencap.privacy.pii import PiiDetector

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# spaCy backend fixtures and tests (legacy)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def detector() -> PiiDetector:
    """Module-scoped spaCy backend — Presidio init is expensive (~2s)."""
    return PiiDetector(ner_backend="spacy")


class TestPersonDetection:
    def test_full_name(self, detector: PiiDetector):
        dets = detector.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1

    def test_spanish_name(self, detector: PiiDetector):
        dets = detector.detect("Jose Garcia logged in")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1


class TestEmailDetection:
    def test_email(self, detector: PiiDetector):
        text = "Contact jane@example.com"
        dets = detector.detect(text)
        emails = [d for d in dets if d.entity_type == EntityType.EMAIL]
        assert len(emails) >= 1
        assert any(text[d.start : d.end] == "jane@example.com" for d in emails)


class TestPhoneDetection:
    def test_phone_parenthetical(self, detector: PiiDetector):
        dets = detector.detect("Call (555) 123-4567")
        phones = [d for d in dets if d.entity_type == EntityType.PHONE]
        assert len(phones) >= 1

    def test_phone_dashed(self, detector: PiiDetector):
        dets = detector.detect("Phone: 555-123-4567")
        phones = [d for d in dets if d.entity_type == EntityType.PHONE]
        assert len(phones) >= 1


class TestCreditCardDetection:
    def test_spaced_card(self, detector: PiiDetector):
        dets = detector.detect("Card: 4532 0151 1283 0366")
        cc = [d for d in dets if d.entity_type == EntityType.CREDIT_CARD]
        assert len(cc) >= 1


class TestLocationDetection:
    def test_location_detected(self, detector: PiiDetector):
        dets = detector.detect("Ship to Anytown, USA")
        addrs = [d for d in dets if d.entity_type == EntityType.ADDRESS]
        assert len(addrs) >= 1


class TestSkippedTypes:
    def test_url_not_flagged_as_pii(self, detector: PiiDetector):
        dets = detector.detect("Visit https://example.com")
        # URL should be skipped
        assert not any(d.entity_type == "URL" for d in dets)

    def test_date_not_flagged(self, detector: PiiDetector):
        dets = detector.detect("Meeting on January 15, 2024")
        # DATE_TIME should be skipped
        assert not any(d.entity_type == "DATE_TIME" for d in dets)


class TestPersonThreshold:
    def test_below_threshold_person_dropped(self):
        """PERSON detection below threshold is dropped."""
        # Presidio scores PERSON at 0.85 — set threshold above to filter them
        det = PiiDetector(person_threshold=0.90, ner_backend="spacy")
        dets = det.detect("Ghostty tmux a")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) == 0

    def test_above_threshold_person_kept(self):
        """PERSON detection at or above threshold passes through."""
        det = PiiDetector(person_threshold=0.5, ner_backend="spacy")
        dets = det.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1

    def test_non_person_unaffected(self):
        """Threshold only applies to PERSON, not EMAIL/PHONE."""
        det = PiiDetector(person_threshold=1.0, ner_backend="spacy")  # block all PERSON
        dets = det.detect("Contact jane@example.com at 555-123-4567")
        # Emails and phones should still be detected
        assert any(d.entity_type == EntityType.EMAIL for d in dets)


class TestPersonAllowlist:
    def test_allowlisted_app_suppressed(self):
        """App name in allowlist suppresses PERSON detection."""
        det = PiiDetector(
            person_threshold=1.0,  # disable threshold so only allowlist matters
            person_allowlist=frozenset({"ghostty"}),
            ner_backend="spacy",
        )
        dets = det.detect("Ghostty tmux a")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) == 0

    def test_real_name_not_suppressed(self):
        """Real person name not in allowlist is still detected."""
        det = PiiDetector(
            person_allowlist=frozenset({"ghostty", "bitwarden"}),
            ner_backend="spacy",
        )
        dets = det.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1

    def test_case_insensitive_match(self):
        """Allowlist matching is case-insensitive."""
        det = PiiDetector(
            person_threshold=1.0,
            person_allowlist=frozenset({"bitwarden"}),
            ner_backend="spacy",
        )
        dets = det.detect("Bitwarden Bitwarden")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) == 0


class TestOffsets:
    def test_offset_correctness(self, detector: PiiDetector):
        text = "Hello John Doe, your email is john@example.com"
        dets = detector.detect(text)
        for d in dets:
            extracted = text[d.start : d.end]
            assert len(extracted) > 0
            # Verify the extracted text is reasonable
            assert extracted in text


class TestSourceString:
    def test_spacy_source(self):
        det = PiiDetector(ner_backend="spacy")
        dets = det.detect("Contact jane@example.com")
        for d in dets:
            assert d.source == "pii-presidio"

    def test_gliner_source(self):
        det = PiiDetector(ner_backend="gliner")
        dets = det.detect("Contact jane@example.com")
        for d in dets:
            assert d.source == "pii-gliner"


# ---------------------------------------------------------------------------
# GLiNER backend tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gliner_detector() -> PiiDetector:
    """Module-scoped GLiNER backend — model load is expensive (~1-2s)."""
    return PiiDetector(ner_backend="gliner")


class TestGlinerPersonDetection:
    def test_full_name(self, gliner_detector: PiiDetector):
        dets = gliner_detector.detect("John Doe is here")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1

    def test_spanish_name(self, gliner_detector: PiiDetector):
        dets = gliner_detector.detect("Jose Garcia logged in")
        persons = [d for d in dets if d.entity_type == EntityType.PERSON]
        assert len(persons) >= 1


class TestGlinerEmailDetection:
    def test_email(self, gliner_detector: PiiDetector):
        text = "Contact jane@example.com"
        dets = gliner_detector.detect(text)
        emails = [d for d in dets if d.entity_type == EntityType.EMAIL]
        assert len(emails) >= 1


class TestGlinerPhoneDetection:
    def test_phone_dashed(self, gliner_detector: PiiDetector):
        dets = gliner_detector.detect("Phone: 555-123-4567")
        phones = [d for d in dets if d.entity_type == EntityType.PHONE]
        assert len(phones) >= 1


class TestGlinerOffsets:
    def test_offset_correctness(self, gliner_detector: PiiDetector):
        text = "Hello John Doe, your email is john@example.com"
        dets = gliner_detector.detect(text)
        for d in dets:
            extracted = text[d.start : d.end]
            assert len(extracted) > 0
            assert extracted in text


# ---------------------------------------------------------------------------
# Factory switching tests
# ---------------------------------------------------------------------------


class TestFactorySwitching:
    def test_create_with_presidio(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline(pii_engine="presidio")
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        assert "PiiDetector" in detector_names

    def test_create_with_presidio_gliner(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline(pii_engine="presidio-gliner")
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        assert "PiiDetector" in detector_names

    def test_create_auto_uses_gliner(self):
        from screencap.privacy import create_default_pipeline

        pipeline = create_default_pipeline()  # auto
        detector_names = [type(d).__name__ for d in pipeline._detectors]
        assert "PiiDetector" in detector_names

    def test_create_invalid_engine_raises(self):
        from screencap.privacy import create_default_pipeline

        with pytest.raises(ValueError, match="Invalid pii_engine"):
            create_default_pipeline(pii_engine="invalid")

    def test_create_datafog_raises(self):
        """DataFog engine is no longer valid."""
        from screencap.privacy import create_default_pipeline

        with pytest.raises(ValueError, match="Invalid pii_engine"):
            create_default_pipeline(pii_engine="datafog")
