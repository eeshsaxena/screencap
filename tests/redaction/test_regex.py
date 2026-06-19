"""Tests for RegexDetector."""

from __future__ import annotations

import pytest

from screencap.redaction import EntityType
from screencap.redaction.regex import RegexDetector

pytestmark = pytest.mark.privacy


@pytest.fixture()
def detector() -> RegexDetector:
    return RegexDetector()


class TestPasswordAssignments:
    def test_password_equals(self, detector: RegexDetector):
        dets = detector.detect("password=hunter2")
        assert len(dets) == 1
        assert dets[0].entity_type == EntityType.PASSWORD

    def test_pwd_colon(self, detector: RegexDetector):
        dets = detector.detect("pwd: secret123")
        assert len(dets) == 1
        assert dets[0].entity_type == EntityType.PASSWORD

    def test_pass_equals(self, detector: RegexDetector):
        dets = detector.detect("PASS=mypassword")
        assert len(dets) == 1
        assert dets[0].entity_type == EntityType.PASSWORD

    def test_password_in_export(self, detector: RegexDetector):
        dets = detector.detect("export DB_PASSWORD=s3cret")
        assert len(dets) == 1
        assert dets[0].entity_type == EntityType.PASSWORD


class TestConnectionStrings:
    def test_postgresql(self, detector: RegexDetector):
        text = "DATABASE_URL=postgresql://admin:s3cret@db.example.com:5432/myapp"
        dets = detector.detect(text)
        conn = [d for d in dets if d.entity_type == EntityType.CONNECTION_STRING]
        assert len(conn) == 1

    def test_mongodb(self, detector: RegexDetector):
        text = "mongodb://user:pass@host:27017/db"
        dets = detector.detect(text)
        conn = [d for d in dets if d.entity_type == EntityType.CONNECTION_STRING]
        assert len(conn) == 1

    def test_redis(self, detector: RegexDetector):
        text = "redis://default:password@redis.example.com:6379"
        dets = detector.detect(text)
        conn = [d for d in dets if d.entity_type == EntityType.CONNECTION_STRING]
        assert len(conn) == 1

    def test_mongodb_srv(self, detector: RegexDetector):
        text = "mongodb+srv://user:pass@cluster.mongodb.net/db"
        dets = detector.detect(text)
        conn = [d for d in dets if d.entity_type == EntityType.CONNECTION_STRING]
        assert len(conn) == 1


class TestJWT:
    def test_jwt_token(self, detector: RegexDetector):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        dets = detector.detect(jwt)
        assert len(dets) >= 1
        assert any(d.entity_type == EntityType.JWT for d in dets)


class TestCreditCards:
    def test_valid_visa_spaces(self, detector: RegexDetector):
        # Valid Luhn: 4532015112830366
        dets = detector.detect("card: 4532 0151 1283 0366")
        cc = [d for d in dets if d.entity_type == EntityType.CREDIT_CARD]
        assert len(cc) == 1

    def test_valid_visa_dashes(self, detector: RegexDetector):
        dets = detector.detect("card: 4532-0151-1283-0366")
        cc = [d for d in dets if d.entity_type == EntityType.CREDIT_CARD]
        assert len(cc) == 1

    def test_invalid_luhn_rejected(self, detector: RegexDetector):
        # Invalid Luhn
        dets = detector.detect("card: 1234 5678 9012 3456")
        cc = [d for d in dets if d.entity_type == EntityType.CREDIT_CARD]
        assert len(cc) == 0


class TestBasicAuthURLs:
    def test_basic_auth(self, detector: RegexDetector):
        text = "https://user:password@host.com/path"
        dets = detector.detect(text)
        assert any(d.entity_type == EntityType.PASSWORD for d in dets)


class TestBearerTokens:
    def test_bearer_token(self, detector: RegexDetector):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0IjoiMTIzIn0.abc123def456"
        dets = detector.detect(text)
        assert any(d.entity_type == EntityType.API_KEY for d in dets)

    def test_bearer_only(self, detector: RegexDetector):
        text = "Bearer some-token-value-here"
        dets = detector.detect(text)
        assert any(d.entity_type == EntityType.API_KEY for d in dets)


class TestSSN:
    def test_ssn_with_prefix(self, detector: RegexDetector):
        dets = detector.detect("SSN: 123-45-6789")
        ssn = [d for d in dets if d.entity_type == EntityType.SSN]
        assert len(ssn) == 1

    def test_ssn_social_security(self, detector: RegexDetector):
        dets = detector.detect("Social Security Number: 123-45-6789")
        ssn = [d for d in dets if d.entity_type == EntityType.SSN]
        assert len(ssn) == 1

    def test_bare_ssn_not_detected(self, detector: RegexDetector):
        # Without context, should not flag random dashed numbers
        dets = detector.detect("Reference: 123-45-6789")
        ssn = [d for d in dets if d.entity_type == EntityType.SSN]
        assert len(ssn) == 0


class TestFalsePositives:
    def test_uuid_not_detected(self, detector: RegexDetector):
        text = "id: 550e8400-e29b-41d4-a716-446655440000"
        dets = detector.detect(text)
        # UUID should not be flagged as credit card or anything
        assert len(dets) == 0

    def test_hex_color_not_detected(self, detector: RegexDetector):
        text = "color: #FF5733"
        dets = detector.detect(text)
        assert len(dets) == 0

    def test_plain_text_not_detected(self, detector: RegexDetector):
        text = "The quick brown fox jumps over the lazy dog"
        dets = detector.detect(text)
        assert len(dets) == 0
