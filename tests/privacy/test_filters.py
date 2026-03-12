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
    def test_reject_jr(self, hf: HeuristicFilter):
        """'Jr' from 'Robert Smith Jr.' — suffix is not a person."""
        text = "Jr"
        det = _det(EntityType.PERSON, 0, 2)
        assert hf.filter(text, [det]) == []

    def test_reject_al(self, hf: HeuristicFilter):
        """'Al' from terminal output — not a person."""
        text = "Al"
        det = _det(EntityType.PERSON, 0, 2)
        assert hf.filter(text, [det]) == []

    def test_keep_real_name(self, hf: HeuristicFilter):
        """'John Doe' is 8 chars — should pass."""
        text = "John Doe"
        det = _det(EntityType.PERSON, 0, 8)
        result = hf.filter(text, [det])
        assert len(result) == 1

    def test_keep_four_char_name(self, hf: HeuristicFilter):
        """'Jose' is exactly 4 chars — should pass (threshold is < 4)."""
        text = "Jose"
        det = _det(EntityType.PERSON, 0, 4)
        result = hf.filter(text, [det])
        assert len(result) == 1


class TestRejectMonthPerson:
    def test_reject_mar(self, hf: HeuristicFilter):
        """'Mar' from 'Mar 12 10:30' in ls -la output."""
        text = "drwxr-xr-x 5 user staff Mar 12 10:30 Documents"
        # "Mar" at position 30..33
        idx = text.index("Mar")
        det = _det(EntityType.PERSON, idx, idx + 3)
        assert hf.filter(text, [det]) == []

    def test_reject_january(self, hf: HeuristicFilter):
        """'January' from calendar UI."""
        text = "January"
        det = _det(EntityType.PERSON, 0, 7)
        assert hf.filter(text, [det]) == []

    def test_reject_may(self, hf: HeuristicFilter):
        """'May' — both a month and a name, but we err on rejecting."""
        text = "May"
        det = _det(EntityType.PERSON, 0, 3)
        assert hf.filter(text, [det]) == []


class TestRejectUiKeywordPerson:
    def test_reject_tab(self, hf: HeuristicFilter):
        """'Tab' from macOS Window menu accessibility text."""
        text = "Tab"
        det = _det(EntityType.PERSON, 0, 3)
        assert hf.filter(text, [det]) == []

    def test_reject_inbox(self, hf: HeuristicFilter):
        """'Inbox' from Gmail sidebar accessibility text."""
        text = "Inbox"
        det = _det(EntityType.PERSON, 0, 5)
        assert hf.filter(text, [det]) == []

    def test_reject_starred(self, hf: HeuristicFilter):
        """'Starred' from email client sidebar."""
        text = "Starred"
        det = _det(EntityType.PERSON, 0, 7)
        assert hf.filter(text, [det]) == []


class TestKeepRealPerson:
    def test_keep_li_wei(self, hf: HeuristicFilter):
        """'Li Wei' — 6 chars total, real name should pass."""
        text = "Li Wei"
        det = _det(EntityType.PERSON, 0, 6)
        result = hf.filter(text, [det])
        assert len(result) == 1

    def test_keep_john_doe(self, hf: HeuristicFilter):
        """Standard Western name passes all rules."""
        text = "John Doe"
        det = _det(EntityType.PERSON, 0, 8)
        result = hf.filter(text, [det])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# ADDRESS rules
# ---------------------------------------------------------------------------


class TestRejectBoxDrawingAddress:
    def test_reject_box_border(self, hf: HeuristicFilter):
        """TUI box border characters detected as ADDRESS."""
        text = "┌─────┐"
        det = _det(EntityType.ADDRESS, 0, len(text))
        assert hf.filter(text, [det]) == []

    def test_reject_mixed_box_chars(self, hf: HeuristicFilter):
        """Mixed box-drawing detected as ADDRESS."""
        text = "│ ├──┤ │"
        det = _det(EntityType.ADDRESS, 0, len(text))
        assert hf.filter(text, [det]) == []

    def test_keep_real_address(self, hf: HeuristicFilter):
        """'123 Main St' — real address should pass."""
        text = "123 Main St"
        det = _det(EntityType.ADDRESS, 0, len(text))
        result = hf.filter(text, [det])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# SSN rules
# ---------------------------------------------------------------------------


class TestRejectMalformedSsn:
    def test_reject_pid(self, hf: HeuristicFilter):
        """'12345' from ps aux output — no dashes, not SSN format."""
        text = "12345"
        det = _det(EntityType.SSN, 0, 5)
        assert hf.filter(text, [det]) == []

    def test_reject_git_hash(self, hf: HeuristicFilter):
        """'34d0845' — git short hash flagged as SSN by GLiNER."""
        text = "34d0845"
        det = _det(EntityType.SSN, 0, 7)
        assert hf.filter(text, [det]) == []

    def test_reject_memory_size(self, hf: HeuristicFilter):
        """'16384' — memory size from ps output."""
        text = "16384"
        det = _det(EntityType.SSN, 0, 5)
        assert hf.filter(text, [det]) == []

    def test_keep_real_ssn(self, hf: HeuristicFilter):
        """'123-45-6789' — proper SSN format should pass."""
        text = "123-45-6789"
        det = _det(EntityType.SSN, 0, 11)
        result = hf.filter(text, [det])
        assert len(result) == 1

    def test_keep_ssn_with_prefix(self, hf: HeuristicFilter):
        """'SSN: 123-45-6789' — NER models include context prefix, SSN pattern still present."""
        text = "SSN: 123-45-6789"
        det = _det(EntityType.SSN, 0, 16)
        result = hf.filter(text, [det])
        assert len(result) == 1


# ---------------------------------------------------------------------------
# PHONE rules
# ---------------------------------------------------------------------------


class TestRejectNoSeparatorPhone:
    def test_reject_digits_only(self, hf: HeuristicFilter):
        """'12345678901' — no separators, likely a numeric ID."""
        text = "12345678901"
        det = _det(EntityType.PHONE, 0, 11)
        assert hf.filter(text, [det]) == []

    def test_keep_real_phone_parens(self, hf: HeuristicFilter):
        """'(555) 123-4567' — has formatting, should pass."""
        text = "(555) 123-4567"
        det = _det(EntityType.PHONE, 0, len(text))
        result = hf.filter(text, [det])
        assert len(result) == 1

    def test_keep_real_phone_dashes(self, hf: HeuristicFilter):
        """'555-123-4567' — has dashes, should pass."""
        text = "555-123-4567"
        det = _det(EntityType.PHONE, 0, len(text))
        result = hf.filter(text, [det])
        assert len(result) == 1

    def test_keep_real_phone_dots(self, hf: HeuristicFilter):
        """'555.123.4567' — has dots, should pass."""
        text = "555.123.4567"
        det = _det(EntityType.PHONE, 0, len(text))
        result = hf.filter(text, [det])
        assert len(result) == 1


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
