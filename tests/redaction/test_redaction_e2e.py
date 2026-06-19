"""End-to-end redaction tests with realistic screen recording content.

These tests exercise the full detection → anonymization pipeline against
text that actually appears during macOS screen recordings: window titles,
keystroke captures, terminal output, and mixed PII/code.

No mocks — runs the real pipeline with all detectors (regex, secrets, PII).
"""

from __future__ import annotations

import pytest

from screencap.redaction import Anonymizer, EntityType, create_default_pipeline

pytestmark = pytest.mark.privacy


@pytest.fixture(scope="module")
def pipeline():
    """Module-scoped pipeline — model load is expensive (~1-2s)."""
    return create_default_pipeline()


@pytest.fixture(scope="module")
def anonymizer():
    return Anonymizer()


def _scrub(pipeline, anonymizer, text: str) -> tuple[str, list]:
    """Run detection + anonymization, return (scrubbed_text, detections)."""
    result = pipeline.detect(text)
    scrubbed = anonymizer.anonymize(result.normalized_text, result.detections)
    return scrubbed, result.detections


def _entity_types(detections) -> set[str]:
    return {d.entity_type for d in detections}


def _detected_text(text: str, detections, entity_type: str) -> list[str]:
    """Extract the text spans detected as a given entity type."""
    return [text[d.start : d.end] for d in detections if d.entity_type == entity_type]


# ---------------------------------------------------------------------------
# Scenario 1: Window title and keystroke scrubbing
#
# Window titles with names/emails and keystroke text with PII are the
# highest-frequency redaction targets in a real recording session.
# ---------------------------------------------------------------------------


class TestWindowTitleAndKeystrokeScrubbing:
    def test_person_name_in_chrome_title(self, pipeline, anonymizer):
        """Chrome window title 'John Smith - Gmail' → name redacted."""
        text = "John Smith - Gmail - Google Chrome"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "<PERSON>" in scrubbed
        assert "John" not in scrubbed

    def test_email_in_keystroke_text(self, pipeline, anonymizer):
        """Typed email address is redacted from keystroke capture."""
        text = "Hi Sarah, my email is john.smith@example.com"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "john.smith@example.com" not in scrubbed
        assert "<EMAIL>" in scrubbed

    def test_ssn_in_keystroke_text(self, pipeline, anonymizer):
        """SSN typed into a form field is redacted."""
        text = "SSN: 123-45-6789"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "123-45-6789" not in scrubbed

    def test_connection_string_in_terminal(self, pipeline, anonymizer):
        """Database connection string with embedded password is redacted."""
        text = "psql postgres://admin:s3cret@db.example.com/prod"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "s3cret" not in scrubbed

    def test_password_export_in_terminal(self, pipeline, anonymizer):
        """Shell export of a password variable is redacted."""
        text = "export DB_PASSWORD=hunter2"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "hunter2" not in scrubbed


# ---------------------------------------------------------------------------
# Scenario 2: Mixed PII and code
#
# Real recordings mix sensitive PII with code/terminal output.
# We need to redact the PII without false-positiving on code.
# ---------------------------------------------------------------------------


class TestMixedPiiAndCode:
    def test_name_in_git_commit(self, pipeline, anonymizer):
        """Person name inside a git commit message is redacted."""
        text = 'git commit -m "Fix login for Michael Johnson"'
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        persons = _detected_text(text, dets, EntityType.PERSON)
        assert any("Michael" in p or "Johnson" in p for p in persons)

    def test_jwt_bearer_token(self, pipeline, anonymizer):
        """Bearer token with JWT is redacted."""
        text = 'curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"'
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "eyJhbGci" not in scrubbed

    def test_stripe_api_key(self, pipeline, anonymizer):
        """Stripe secret key is redacted."""
        text = "STRIPE_SECRET_KEY=sk_live_4eC39HqLyjWDarjtT1zdp7dc"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "sk_live_" not in scrubbed

    def test_credit_card_in_message(self, pipeline, anonymizer):
        """Credit card number in a message is redacted."""
        text = "Dear Mr. Thompson, your CC is 4532 0151 1283 0366"
        scrubbed, dets = _scrub(pipeline, anonymizer, text)

        assert "4532 0151 1283 0366" not in scrubbed

    def test_file_paths_not_redacted(self, pipeline, anonymizer):
        """Unix file paths should not trigger false positives."""
        text = "$ ls -la /Users/developer/Documents/projects"
        _, dets = _scrub(pipeline, anonymizer, text)

        # No PII entities expected — paths are not PII
        pii_types = {EntityType.PERSON, EntityType.EMAIL, EntityType.SSN, EntityType.CREDIT_CARD}
        assert not pii_types & _entity_types(dets)

    def test_pytest_output_not_redacted(self, pipeline, anonymizer):
        """Test runner output should not trigger false positives."""
        text = "Running pytest tests/test_cli.py::test_version"
        _, dets = _scrub(pipeline, anonymizer, text)

        pii_types = {EntityType.PERSON, EntityType.EMAIL, EntityType.SSN, EntityType.CREDIT_CARD}
        assert not pii_types & _entity_types(dets)

    def test_connection_error_not_redacted(self, pipeline, anonymizer):
        """Error messages should not trigger false positives."""
        text = "error: connection refused on port 5432"
        _, dets = _scrub(pipeline, anonymizer, text)

        pii_types = {EntityType.PERSON, EntityType.EMAIL, EntityType.SSN, EntityType.CREDIT_CARD}
        assert not pii_types & _entity_types(dets)


# ---------------------------------------------------------------------------
# Scenario 3: Score calibration — threshold separates real names from noise
#
# The person_threshold=0.5 must correctly separate true positive names
# from false positive app names / UI text with the fast-gliner backend.
# ---------------------------------------------------------------------------


class TestScoreCalibration:
    def test_real_names_survive_threshold(self, pipeline):
        """Real person names should be detected (score > threshold) in the full pipeline."""
        texts_with_names = [
            ("John Doe - Google Chrome", "John"),
            ("Meeting with Sarah Connor at 3pm", "Sarah"),
            ("Jose Garcia logged in from terminal", "Jose"),
        ]
        for text, expected_fragment in texts_with_names:
            result = pipeline.detect(text)
            persons = [d for d in result.detections if d.entity_type == EntityType.PERSON]
            matched = [d for d in persons if expected_fragment in text[d.start : d.end]]
            assert matched, (
                f"Expected PERSON containing {expected_fragment!r} in {text!r}, "
                f"got: {[(text[d.start:d.end], d.score) for d in persons]}"
            )

    def test_docker_command_no_false_positive(self, pipeline):
        """Docker commands should not trigger PERSON detection."""
        result = pipeline.detect("docker run -it ubuntu bash")
        persons = [d for d in result.detections if d.entity_type == EntityType.PERSON]
        assert not persons

    def test_vim_config_no_false_positive(self, pipeline):
        """Editor config paths should not trigger PERSON detection."""
        result = pipeline.detect("vim ~/.config/nvim/init.lua")
        persons = [d for d in result.detections if d.entity_type == EntityType.PERSON]
        assert not persons

    def test_app_bundle_id_no_false_positive(self, pipeline):
        """macOS bundle IDs in window titles should not trigger PERSON detection."""
        result = pipeline.detect("com.apple.Terminal - zsh")
        persons = [d for d in result.detections if d.entity_type == EntityType.PERSON]
        assert not persons
