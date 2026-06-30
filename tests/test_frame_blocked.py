"""SCR-186 U6 — blocked-frame predicate (recording.db, fail-closed).

Covers the wiring of derive_skip_intervals + find_blocked_interval into an
``is_blocked(ts)`` predicate and the fail-closed behaviour. The heavy
re-derivation itself is exercised by the backfill skip-interval tests; here we
pin that the predicate (a) reflects derived intervals via the half-open
membership test and (b) flags every frame when geometry is indeterminate.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

import pytest

from screencap import frame_blocked
from screencap.privacy.actions import PrivacyAction
from screencap.privacy.policy import PrivacyMode
from screencap.scrubber import BlockedInterval, ScrubContext


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


def _make_window_db(path: Path, *, ts: float, bundle: str, title: str) -> None:
    """Minimal recording.db with one covering ``window_event`` row.

    Enough for ``derive_skip_intervals``' raw-SQL ambiguity read to yield a
    ``window_start`` (so a later frame is "covered" and the uncovered-gap pass
    adds nothing) — isolating the canonical-fail path as the only reason a frame
    could end up blocked in the SCR-198 test below.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, pixel_ratio REAL)"
        )
        db.execute("INSERT INTO recording VALUES (1, 0.0, 2.0)")
        db.execute(
            "CREATE TABLE window_event ("
            "id INTEGER PRIMARY KEY, recording_id INTEGER, timestamp REAL, "
            "app_bundle_id TEXT, window_id TEXT, title TEXT, state TEXT, "
            "app_name TEXT, browser_url TEXT)"
        )
        db.execute(
            "INSERT INTO window_event "
            "(id, recording_id, timestamp, app_bundle_id, window_id, title) "
            "VALUES (1, 1, ?, ?, 'w1', ?)",
            (ts, bundle, title),
        )
        db.commit()


@pytest.mark.privacy
def test_partial_canonical_read_fails_closed(tmp_path, monkeypatch):
    """SCR-198: a partial recording.db read → frame.nearest miss (all frames blocked).

    A masked 1password window covers the frame at 150. The canonical pass is
    simulated to fail *gracefully* — ``build_scrub_context`` returns an empty set
    with ``canonical_ok=False`` (no raise) — exactly the silent-empty shape that
    used to under-block. The covering window keeps the uncovered-gap pass quiet,
    so the ONLY thing that can block the frame is the new fail-closed canonical
    signal. With it, every frame is treated as blocked → the verb resolves a miss.
    """
    rec = tmp_path / "rec"
    _make_window_db(
        rec / "recording.db", ts=100.0,
        bundle="com.1password.1password", title="Vault",
    )
    # Real PUBLIC classifier/evaluator (deterministic, host-config-independent) so
    # the ambiguity read classifies the window and yields its window_start.
    from screencap.backfill.skip_intervals import build_classifier_evaluator

    classifier, evaluator = build_classifier_evaluator(mode=PrivacyMode.PUBLIC)
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_classifier_evaluator",
        lambda *a, **k: (classifier, evaluator),
    )
    # Simulate the partial read: canonical empties, signalled via canonical_ok.
    monkeypatch.setattr(
        "screencap.backfill.skip_intervals.build_scrub_context",
        lambda *a, **k: ScrubContext(canonical_ok=False),
    )

    is_blocked = frame_blocked.build_is_blocked(rec, [150.0])
    # Fail-closed: the masked frame (and every frame) is blocked → miss.
    assert is_blocked(150.0) is True
    assert is_blocked(999.0) is True
