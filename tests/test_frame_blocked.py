"""SCR-186 U6 — blocked-frame predicate (recording.db, fail-closed).

Covers the wiring of derive_skip_intervals + find_blocked_interval into an
``is_blocked(ts)`` predicate and the fail-closed behaviour. The heavy
re-derivation itself is exercised by the backfill skip-interval tests; here we
pin that the predicate (a) reflects derived intervals via the half-open
membership test and (b) flags every frame when geometry is indeterminate.
"""

from __future__ import annotations

import pytest

from screencap import frame_blocked
from screencap.privacy.actions import PrivacyAction
from screencap.scrubber import BlockedInterval


def test_empty_frames_returns_allow_all(tmp_path):
    is_blocked = frame_blocked.build_is_blocked(tmp_path / "rec", [])
    assert is_blocked(123.0) is False


def test_predicate_reflects_derived_intervals(tmp_path, monkeypatch):
    # Isolate the wiring: stub the re-derivation to a known interval and confirm
    # the predicate applies find_blocked_interval's half-open [start, end) test.
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (None, None),
    )
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.derive_skip_intervals",
        lambda *a, **k: [
            BlockedInterval(
                start=100.0, end=200.0,
                action=PrivacyAction.EXCLUDE, reason="test",
            )
        ],
    )
    is_blocked = frame_blocked.build_is_blocked(tmp_path / "rec", [100.0, 150.0, 250.0])
    assert is_blocked(150.0) is True   # inside
    assert is_blocked(100.0) is True   # inclusive start
    assert is_blocked(200.0) is False  # exclusive end (half-open)
    assert is_blocked(50.0) is False   # before
    assert is_blocked(250.0) is False  # after


@pytest.mark.privacy
def test_missing_recording_db_fails_closed(tmp_path, monkeypatch):
    # No recording.db: the real derive_skip_intervals gap pass has no window
    # events, so every flat frame is an uncovered gap -> all blocked -> miss.
    # Stub only the classifier builder (unused when the DB is absent) so the test
    # does not depend on the host's privacy config.
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (None, None),
    )
    rec = tmp_path / "rec"
    rec.mkdir()
    assert not (rec / "recording.db").exists()
    is_blocked = frame_blocked.build_is_blocked(rec, [1_719_400_000.0, 1_719_400_010.0])
    assert is_blocked(1_719_400_000.0) is True
    assert is_blocked(1_719_400_010.0) is True


@pytest.mark.privacy
def test_fails_closed_when_machinery_raises(tmp_path, monkeypatch):
    # Any failure building the privacy machinery -> all-blocked sentinel.
    def _boom(*a, **k):
        raise RuntimeError("privacy config unavailable")

    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator", _boom
    )
    is_blocked = frame_blocked.build_is_blocked(tmp_path / "rec", [1.0, 2.0])
    assert is_blocked(1.0) is True
    assert is_blocked(999.0) is True
