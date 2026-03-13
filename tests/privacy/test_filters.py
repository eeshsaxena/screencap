"""Tests for HeuristicFilter — each test documents the real FP it prevents."""

from __future__ import annotations

import pytest

from screencap.privacy import Detection, EntityType
from screencap.privacy.filters import HeuristicFilter

pytestmark = pytest.mark.privacy


@pytest.fixture()
def hf() -> HeuristicFilter:
    return HeuristicFilter()


def _det(entity_type: str, start: int, end: int) -> Detection:
    """Shorthand for creating a Detection in filter tests."""
    return Detection(entity_type=entity_type, start=start, end=end, score=0.85, source="pii-gliner")


# ---------------------------------------------------------------------------
# PERSON rules
# ---------------------------------------------------------------------------


class TestRejectShortPerson:
    @pytest.mark.parametrize("span", ["Jr", "Al", "Ed"], ids=["suffix", "abbreviation", "nickname"])
    def test_reject_short_person(self, hf: HeuristicFilter, span: str):
        """Spans < 4 chars are abbreviations, suffixes, or UI labels — not people."""
        det = _det(EntityType.PERSON, 0, len(span))
        assert hf.filter(span, [det]) == []

    def test_keep_four_char_name(self, hf: HeuristicFilter):
        """'Jose' is exactly 4 chars — boundary: threshold is < 4."""
        text = "Jose"
        det = _det(EntityType.PERSON, 0, 4)
        assert len(hf.filter(text, [det])) == 1


class TestRejectMonthPerson:
    def test_reject_month_in_ls_output(self, hf: HeuristicFilter):
        """'Mar' at non-zero offset in real ls -la output."""
        text = "drwxr-xr-x 5 user staff Mar 12 10:30 Documents"
        idx = text.index("Mar")
        det = _det(EntityType.PERSON, idx, idx + 3)
        assert hf.filter(text, [det]) == []

    def test_reject_full_month_name(self, hf: HeuristicFilter):
        """'January' from calendar UI — full month names also rejected."""
        text = "January"
        det = _det(EntityType.PERSON, 0, 7)
        assert hf.filter(text, [det]) == []


class TestRejectUiKeywordPerson:
    @pytest.mark.parametrize("keyword", ["Tab", "Inbox", "Starred"],
                             ids=["menu-item", "sidebar-label", "email-label"])
    def test_reject_ui_keywords(self, hf: HeuristicFilter, keyword: str):
        """UI/CLI keywords from menus and sidebars are not people."""
        det = _det(EntityType.PERSON, 0, len(keyword))
        assert hf.filter(keyword, [det]) == []


class TestRejectAppNamePerson:
    @pytest.mark.parametrize("name", [
        "Homebrew", "Docker", "Ghostty", "Safari", "Figma",
        "Terraform", "Kubernetes", "Python", "Elasticsearch", "Bitwarden",
    ], ids=[
        "cli-tool", "container", "terminal", "browser", "design",
        "infra", "orchestrator", "language", "database", "password-mgr",
    ])
    def test_reject_software_names(self, hf: HeuristicFilter, name: str):
        """Known software names flagged as PERSON are rejected."""
        det = _det(EntityType.PERSON, 0, len(name))
        assert hf.filter(name, [det]) == []

    def test_real_person_survives(self, hf: HeuristicFilter):
        """Real person names are NOT rejected by the software name filter."""
        for name in ("Alice", "Sarah", "Robert", "James"):
            det = _det(EntityType.PERSON, 0, len(name))
            assert len(hf.filter(name, [det])) == 1, f"{name} should not be rejected"

    def test_case_insensitive(self, hf: HeuristicFilter):
        """Matching is case-insensitive: HOMEBREW, homebrew, Homebrew all rejected."""
        for variant in ("HOMEBREW", "homebrew", "Homebrew"):
            det = _det(EntityType.PERSON, 0, len(variant))
            assert hf.filter(variant, [det]) == [], f"{variant} should be rejected"


class TestKeepRealPerson:
    def test_keep_short_real_name(self, hf: HeuristicFilter):
        """'Li Wei' — 6 chars, passes short-person filter and is not a keyword/month."""
        text = "Li Wei"
        det = _det(EntityType.PERSON, 0, 6)
        assert len(hf.filter(text, [det])) == 1


# ---------------------------------------------------------------------------
# ADDRESS rules
# ---------------------------------------------------------------------------


class TestRejectBoxDrawingAddress:
    def test_reject_box_border(self, hf: HeuristicFilter):
        """TUI box border characters detected as ADDRESS."""
        text = "┌─────┐"
        det = _det(EntityType.ADDRESS, 0, len(text))
        assert hf.filter(text, [det]) == []

    def test_keep_real_address(self, hf: HeuristicFilter):
        """'123 Main St' — real address should pass."""
        text = "123 Main St"
        det = _det(EntityType.ADDRESS, 0, len(text))
        assert len(hf.filter(text, [det])) == 1


# ---------------------------------------------------------------------------
# SSN rules
# ---------------------------------------------------------------------------


class TestRejectMalformedSsn:
    def test_reject_numeric_without_dashes(self, hf: HeuristicFilter):
        """'12345' from ps aux — no NNN-NN-NNNN pattern."""
        text = "12345"
        det = _det(EntityType.SSN, 0, 5)
        assert hf.filter(text, [det]) == []

    def test_keep_real_ssn(self, hf: HeuristicFilter):
        """'123-45-6789' — proper SSN format should pass."""
        text = "123-45-6789"
        det = _det(EntityType.SSN, 0, 11)
        assert len(hf.filter(text, [det])) == 1

    def test_keep_ssn_with_prefix(self, hf: HeuristicFilter):
        """'SSN: 123-45-6789' — NER includes context prefix, SSN pattern still present."""
        text = "SSN: 123-45-6789"
        det = _det(EntityType.SSN, 0, 16)
        assert len(hf.filter(text, [det])) == 1


# ---------------------------------------------------------------------------
# PHONE rules
# ---------------------------------------------------------------------------


class TestRejectNoSeparatorPhone:
    def test_reject_digits_only(self, hf: HeuristicFilter):
        """'12345678901' — no separators, likely a numeric ID."""
        text = "12345678901"
        det = _det(EntityType.PHONE, 0, 11)
        assert hf.filter(text, [det]) == []

    def test_keep_real_phone_with_formatting(self, hf: HeuristicFilter):
        """'(555) 123-4567' — has parens, space, and dash."""
        text = "(555) 123-4567"
        det = _det(EntityType.PHONE, 0, len(text))
        assert len(hf.filter(text, [det])) == 1


# ---------------------------------------------------------------------------
# Multi-detection filter
# ---------------------------------------------------------------------------


class TestMultipleDetections:
    def test_mixed_keep_and_reject(self, hf: HeuristicFilter):
        """Filter keeps real detections and rejects FPs in one pass."""
        text = "John Doe called from (555) 123-4567 and Mar was there"
        dets = [
            _det(EntityType.PERSON, 0, 8),     # "John Doe" — keep
            _det(EntityType.PHONE, 21, 35),     # "(555) 123-4567" — keep
            _det(EntityType.PERSON, 40, 43),    # "Mar" — reject (month + short)
        ]
        result = hf.filter(text, dets)
        assert len(result) == 2
        assert result[0].entity_type == EntityType.PERSON
        assert result[0].start == 0
        assert result[1].entity_type == EntityType.PHONE

    def test_non_targeted_types_pass_through(self, hf: HeuristicFilter):
        """Types without rules (EMAIL, API_KEY, etc.) are not filtered."""
        text = "test@example.com has key sk-abc123"
        dets = [
            _det(EntityType.EMAIL, 0, 16),
            _det(EntityType.API_KEY, 25, 34),
        ]
        result = hf.filter(text, dets)
        assert len(result) == 2
