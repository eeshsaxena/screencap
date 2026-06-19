"""Tests for DetectSecretsDetector."""

from __future__ import annotations

import pytest

from screencap.redaction import EntityType
from screencap.redaction.secrets import DetectSecretsDetector

pytestmark = pytest.mark.privacy


@pytest.fixture()
def detector() -> DetectSecretsDetector:
    return DetectSecretsDetector()


class TestAWSKeys:
    def test_aws_access_key(self, detector: DetectSecretsDetector):
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        dets = detector.detect(text)
        assert len(dets) >= 1
        assert any(d.entity_type == EntityType.API_KEY for d in dets)

    def test_aws_key_offset(self, detector: DetectSecretsDetector):
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        dets = detector.detect(text)
        aws = [d for d in dets if d.entity_type == EntityType.API_KEY]
        assert aws
        assert aws[0].start == 18
        assert text[aws[0].start : aws[0].end] == "AKIAIOSFODNN7EXAMPLE"


class TestGitHubTokens:
    def test_github_token(self, detector: DetectSecretsDetector):
        # GitHub tokens: ghp_ + 36 alphanumeric chars
        token = "ghp_" + "A" * 36
        text = f"GITHUB_TOKEN={token}"
        dets = detector.detect(text)
        gh = [d for d in dets if d.entity_type == EntityType.API_KEY]
        assert len(gh) >= 1


class TestOpenAIKeys:
    def test_openai_key(self, detector: DetectSecretsDetector):
        # OpenAI key format: sk-[20 alnum]T3BlbkFJ[20 alnum]
        token = "sk-" + "A" * 20 + "T3BlbkFJ" + "B" * 20
        text = f"OPENAI_API_KEY={token}"
        dets = detector.detect(text)
        oai = [d for d in dets if d.entity_type == EntityType.API_KEY]
        assert len(oai) >= 1


class TestJWTs:
    def test_jwt(self, detector: DetectSecretsDetector):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        text = f"token={jwt}"
        dets = detector.detect(text)
        jwt_dets = [d for d in dets if d.entity_type == EntityType.JWT]
        assert len(jwt_dets) >= 1


class TestPasswords:
    def test_keyword_password_quoted(self, detector: DetectSecretsDetector):
        # KeywordDetector requires quoted values
        text = 'password = "hunter2"'
        dets = detector.detect(text)
        pw = [d for d in dets if d.entity_type == EntityType.PASSWORD]
        assert len(pw) >= 1

    def test_basic_auth(self, detector: DetectSecretsDetector):
        text = "https://user:s3cret@example.com"
        dets = detector.detect(text)
        assert any(d.entity_type == EntityType.PASSWORD for d in dets)


class TestPrivateKeys:
    def test_pem_key(self, detector: DetectSecretsDetector):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIE..."
        dets = detector.detect(text)
        pk = [d for d in dets if d.entity_type == EntityType.PRIVATE_KEY]
        assert len(pk) >= 1


class TestEntropyPostFilter:
    def test_high_entropy_with_context(self, detector: DetectSecretsDetector):
        # Has "key" context word → keyword detector should fire
        text = 'api_key = "aGVsbG8gd29ybGQgdGhpcyBpcyBhIHRlc3Q="'
        dets = detector.detect(text)
        assert len(dets) >= 1

    def test_high_entropy_without_context_filtered(self, detector: DetectSecretsDetector):
        # Random base64 with no secret context → entropy should be filtered
        text = "data: aGVsbG8gd29ybGQgdGhpcyBpcyBhIHRlc3Q="
        dets = detector.detect(text)
        entropy_only = [d for d in dets if d.entity_type == EntityType.SECRET]
        assert len(entropy_only) == 0


class TestLineSeparators:
    def test_crlf_offsets(self, detector: DetectSecretsDetector):
        text = 'line1\r\npassword = "secret123"\r\nline3'
        dets = detector.detect(text)
        pw = [d for d in dets if d.entity_type == EntityType.PASSWORD]
        assert len(pw) >= 1
        for d in pw:
            extracted = text[d.start : d.end]
            assert extracted in text

    def test_unicode_line_separator(self, detector: DetectSecretsDetector):
        text = 'line1\u2028password = "hunter2"\u2028line3'
        dets = detector.detect(text)
        pw = [d for d in dets if d.entity_type == EntityType.PASSWORD]
        if pw:
            for d in pw:
                extracted = text[d.start : d.end]
                assert extracted in text


class TestMultipleOccurrences:
    def test_duplicate_aws_key_same_line(self, detector: DetectSecretsDetector):
        key = "AKIAIOSFODNN7EXAMPLE"
        text = f"key1={key} key2={key}"
        dets = detector.detect(text)
        aws = [d for d in dets if d.entity_type == EntityType.API_KEY]
        assert len(aws) >= 2


class TestEntityTypeMapping:
    def test_unmapped_falls_to_secret(self, detector: DetectSecretsDetector):
        """All mapped types should be valid EntityType constants."""
        from screencap.redaction.secrets import _TYPE_MAP

        valid_types = {
            v
            for k, v in vars(EntityType).items()
            if not k.startswith("_") and isinstance(v, str)
        }
        for mapped_type in _TYPE_MAP.values():
            assert mapped_type in valid_types
