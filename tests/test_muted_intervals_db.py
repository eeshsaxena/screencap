"""Tests for muted-interval persistence in recording.db (SCR-218 U3).

Each mid-recording mute writes a durable ``muted_intervals`` row: opened at the
mute command (over-covering the stop latency), closed at unmute. The table lives
in ``recording.db``, which is local-only by rule (R8) — never uploaded — so the
record of *when* the user muted never leaves the machine.
"""

from __future__ import annotations

from screencap.engine.db import crud
from screencap.engine.db.models import MutedInterval
from screencap import upload


def _open_intervals(session, recording):
    return (
        session.query(MutedInterval)
        .filter(MutedInterval.recording_id == recording.id)
        .filter(MutedInterval.end_ts.is_(None))
        .all()
    )


def test_open_creates_an_open_interval(recording_db):
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)

    rows = _open_intervals(recording_db.session, recording_db.recording)
    assert len(rows) == 1
    assert rows[0].start_ts == 1010.0
    assert rows[0].end_ts is None


def test_close_sets_end_ts(recording_db):
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    closed = crud.close_muted_interval(recording_db.session, recording_db.recording, 1025.0)

    assert closed == 1
    assert _open_intervals(recording_db.session, recording_db.recording) == []
    row = session_all(recording_db)[0]
    assert row.start_ts == 1010.0
    assert row.end_ts == 1025.0
    assert row.end_ts >= row.start_ts


def test_multiple_intervals_are_independent(recording_db):
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    crud.close_muted_interval(recording_db.session, recording_db.recording, 1020.0)
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1030.0)
    crud.close_muted_interval(recording_db.session, recording_db.recording, 1040.0)

    spans = [(r.start_ts, r.end_ts) for r in session_all(recording_db)]
    assert (1010.0, 1020.0) in spans
    assert (1030.0, 1040.0) in spans


def test_close_with_no_open_interval_is_noop(recording_db):
    closed = crud.close_muted_interval(recording_db.session, recording_db.recording, 1025.0)
    assert closed == 0


def test_teardown_while_muted_leaves_open_interval_closeable(recording_db):
    # Simulates a crash/teardown mid-mute: the interval stays open (end NULL),
    # which downstream (U6) treats as muted-to-chunk-end.
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    assert len(_open_intervals(recording_db.session, recording_db.recording)) == 1
    # A later teardown close still closes it.
    assert crud.close_muted_interval(recording_db.session, recording_db.recording, 1099.0) == 1


def test_muted_intervals_stay_local_only(recording_db):
    # The table lives in recording.db, which is R8 local-only — never uploaded.
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    assert upload._is_raw_artifact("recording.db") is True
    assert upload._is_raw_artifact("some/nested/recording.db") is True


def test_span_reader_returns_overlapping_intervals(recording_db):
    from screencap.redaction.geometry import list_muted_intervals_in_span

    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    crud.close_muted_interval(recording_db.session, recording_db.recording, 1020.0)
    crud.open_muted_interval(recording_db.session, recording_db.recording, 1030.0)  # open

    got = list_muted_intervals_in_span(recording_db.db_path, 1005.0, 1035.0)
    assert (1010.0, 1020.0) in got
    assert (1030.0, None) in got  # open interval surfaced with end=None


def test_span_reader_excludes_non_overlapping(recording_db):
    from screencap.redaction.geometry import list_muted_intervals_in_span

    crud.open_muted_interval(recording_db.session, recording_db.recording, 1010.0)
    crud.close_muted_interval(recording_db.session, recording_db.recording, 1020.0)

    assert list_muted_intervals_in_span(recording_db.db_path, 1050.0, 1060.0) == []


def session_all(recording_db):
    return (
        recording_db.session.query(MutedInterval)
        .filter(MutedInterval.recording_id == recording_db.recording.id)
        .order_by(MutedInterval.start_ts)
        .all()
    )
