"""U4a — window_geometry recording + durable coverage-failure signal.

Characterization-first: pins that today the capture path records
``window_geometry`` for a recording (including a would-be-cloud recording),
then asserts the U4a additive behaviour — a simulated geometry-insert failure
produces a durable, queryable, timestamped failure marker AND logs at warning,
while the happy path is unchanged.

U4a is the ADDITIVE half of U4: capture-time blocking / PUBLIC-forcing in
``screen_recorder.py`` is NOT touched here (verified by tests that do not
exercise it). The signal lets the post-hoc video masker (U6) implement its
three-way coverage gate: distinguish "geometry proves no sensitive window"
(case a) from "geometry capture failed / was sparse" (case b, fail closed).
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from screencap.engine.db import create_db, crud, get_session_for_path

# recorder.Event namedtuple: (timestamp, type, data, extra). ``extra`` carries
# the window-geometry payload for a "screen" event.
from screencap.engine.recorder import Event, write_screen_event
from screencap.redaction.geometry import (
    geometry_capture_failures_in_span,
    list_geometry_sample_timestamps,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_recording(db_path: Path, *, base_timestamp: float | None = None):
    """Create a fresh recording.db and return (engine, Session, recording)."""
    t = base_timestamp or time.time()
    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(
        session,
        {
            "timestamp": t,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        },
    )
    return engine, session, recording


class _FakeImage:
    """Minimal stand-in for a PIL image — write_screen_event only needs save()."""

    def save(self, *args, **kwargs) -> None:
        return None


def _screen_event(ts: float, geometry: dict | None) -> Event:
    return Event(timestamp=ts, type="screen", data=_FakeImage(), extra=geometry)


_SENSITIVE_GEOMETRY = {
    "windows": [
        {"app_bundle_id": "com.apple.mail", "left": 0, "top": 0, "width": 800, "height": 600},
    ],
    "display_bounds": [0, 0, 1920, 1080],
}


# ---------------------------------------------------------------------------
# Characterization — window_geometry IS recorded (the U6 input)
# ---------------------------------------------------------------------------


def test_window_geometry_recorded_happy_path(tmp_path, monkeypatch):
    """A screen event with geometry writes a window_geometry row; no failure marker."""
    # RECORD_IMAGES off so write_screen_event does not need a real screenshot dir.
    monkeypatch.setattr("screencap.engine.config.config.RECORD_IMAGES", False)
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp

    write_screen_event(
        session, recording, _screen_event(t + 1.0, _SENSITIVE_GEOMETRY), perf_q=None
    )
    crud.flush_buffers(session)
    session.close()
    engine.dispose()

    with sqlite3.connect(str(db_path)) as conn:
        geoms = conn.execute(
            "SELECT screenshot_timestamp FROM window_geometry"
        ).fetchall()
        failures = conn.execute(
            "SELECT COUNT(*) FROM window_geometry_capture_failure"
        ).fetchone()[0]
    assert [round(r[0], 4) for r in geoms] == [round(t + 1.0, 4)]
    assert failures == 0


def test_window_geometry_recorded_regardless_of_destination(tmp_path, monkeypatch):
    """Geometry capture is always-on — it is NOT gated by cloud_intent.

    ``write_screen_event`` takes no destination argument; the geometry write
    path is identical for a would-be-cloud recording and a local one. This
    pins that U4a needs no capture-path change to keep geometry for cloud.
    """
    monkeypatch.setattr("screencap.engine.config.config.RECORD_IMAGES", False)
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp

    for i in range(3):
        write_screen_event(
            session, recording, _screen_event(t + i, _SENSITIVE_GEOMETRY), perf_q=None
        )
    crud.flush_buffers(session)
    session.close()
    engine.dispose()

    ts = list_geometry_sample_timestamps(db_path, t - 1, t + 10)
    assert len(ts) == 3


def test_no_geometry_payload_writes_nothing(tmp_path, monkeypatch):
    """A screen event with extra=None records neither a geometry row nor a marker."""
    monkeypatch.setattr("screencap.engine.config.config.RECORD_IMAGES", False)
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp

    write_screen_event(session, recording, _screen_event(t + 1.0, None), perf_q=None)
    crud.flush_buffers(session)
    session.close()
    engine.dispose()

    with sqlite3.connect(str(db_path)) as conn:
        geoms = conn.execute("SELECT COUNT(*) FROM window_geometry").fetchone()[0]
        failures = conn.execute(
            "SELECT COUNT(*) FROM window_geometry_capture_failure"
        ).fetchone()[0]
    assert geoms == 0
    assert failures == 0


# ---------------------------------------------------------------------------
# New behaviour — a geometry-insert failure is loud + durable, not silent
# ---------------------------------------------------------------------------


def test_geometry_insert_failure_writes_durable_marker_and_warns(
    tmp_path, monkeypatch
):
    """On insert failure: log at WARNING and write a durable timestamped marker.

    The recording itself must not abort — write_screen_event returns normally.
    """
    monkeypatch.setattr("screencap.engine.config.config.RECORD_IMAGES", False)
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp

    # Force the geometry insert to raise, leaving the failure-marker path
    # (a separate immediate-commit insert) intact.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated geometry insert failure")

    monkeypatch.setattr(crud, "insert_window_geometry", _boom)

    # recorder.py uses loguru, which does NOT propagate to stdlib caplog.
    # Attach a dedicated loguru sink and assert on the captured records.
    from loguru import logger as _loguru_logger

    captured: list[tuple[str, str]] = []
    sink_id = _loguru_logger.add(
        lambda msg: captured.append(
            (msg.record["level"].name, msg.record["message"])
        ),
        level="WARNING",
    )
    try:
        # Does not raise — recording survives a geometry hiccup.
        write_screen_event(
            session, recording, _screen_event(t + 2.5, _SENSITIVE_GEOMETRY), perf_q=None
        )
    finally:
        _loguru_logger.remove(sink_id)

    session.close()
    engine.dispose()

    # Durable marker present and timestamped at the failed screenshot ts.
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT screenshot_timestamp, detail FROM window_geometry_capture_failure"
        ).fetchall()
    assert len(rows) == 1
    assert round(rows[0][0], 4) == round(t + 2.5, 4)
    assert "RuntimeError" in (rows[0][1] or "")

    # Logged loudly (WARNING), not swallowed at debug.
    assert any(
        level == "WARNING" and "window geometry" in message.lower()
        for level, message in captured
    )


def test_geometry_failure_marker_survives_process_restart(tmp_path, monkeypatch):
    """The marker is durable: a fresh open of the same recording.db still sees it.

    Uses the immediate-commit CRUD insert directly (the recorder calls the
    same path) so the test does not depend on monkeypatching the insert.
    """
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp
    crud.insert_window_geometry_capture_failure(
        session, recording, t + 3.0, detail="RuntimeError: boom"
    )
    session.close()
    engine.dispose()

    # Fresh process-equivalent open via the canonical read API.
    failures = geometry_capture_failures_in_span(db_path, t - 1, t + 10)
    assert [round(f, 4) for f in failures] == [round(t + 3.0, 4)]


# ---------------------------------------------------------------------------
# U6 read API — span queries for the three-way coverage gate
# ---------------------------------------------------------------------------


def test_read_api_sample_timestamps_span_filtering(tmp_path):
    """list_geometry_sample_timestamps returns sorted ts inside [start, end] only."""
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp
    for offset in (1.0, 2.0, 3.0, 50.0):  # 50.0 is outside the queried span
        crud.insert_window_geometry(
            session, recording, t + offset, '{"windows": [], "display_bounds": null}'
        )
    crud.flush_buffers(session)
    session.close()
    engine.dispose()

    ts = list_geometry_sample_timestamps(db_path, t + 0.5, t + 10.0)
    assert [round(x - t, 4) for x in ts] == [1.0, 2.0, 3.0]


def test_read_api_failures_in_span(tmp_path):
    """geometry_capture_failures_in_span returns only failures inside the span."""
    db_path = tmp_path / "recording.db"
    engine, session, recording = _new_recording(db_path)
    t = recording.timestamp
    crud.insert_window_geometry_capture_failure(session, recording, t + 5.0)
    crud.insert_window_geometry_capture_failure(session, recording, t + 99.0)
    session.close()
    engine.dispose()

    in_span = geometry_capture_failures_in_span(db_path, t, t + 10.0)
    assert [round(x - t, 4) for x in in_span] == [5.0]


def test_read_api_empty_and_legacy_db_safe(tmp_path):
    """Both read helpers return [] (no raise) when the tables are absent (legacy DB).

    U6 treats absence as unprovable coverage (fail closed), so a degraded
    read returning [] is conservatively safe.
    """
    # A legacy recording.db with NEITHER window_geometry NOR the failure table.
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording (id, timestamp) VALUES (1, 100.0)")
    conn.commit()
    conn.close()

    assert list_geometry_sample_timestamps(legacy, 0, 1e12) == []
    assert geometry_capture_failures_in_span(legacy, 0, 1e12) == []

    # A nonexistent path also degrades to [] rather than raising.
    missing = tmp_path / "nope.db"
    assert list_geometry_sample_timestamps(missing, 0, 1e12) == []
    assert geometry_capture_failures_in_span(missing, 0, 1e12) == []


# ---------------------------------------------------------------------------
# Migration — the marker table lands on a pre-existing recording.db (U1 precedent)
# ---------------------------------------------------------------------------


def test_migration_creates_marker_table_on_existing_db(tmp_path):
    """_migrate_schema (via get_session_for_path) creates the table on a legacy DB.

    Follows the U1 ``PipelineChunkState.__table__.create(engine, checkfirst=True)``
    precedent: a recording.db that pre-dates U4a gains the
    ``window_geometry_capture_failure`` table on the next open, so the marker
    insert does not crash on old recordings.
    """
    # Pre-create a recording.db WITHOUT the failure table by hand.
    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording (id, timestamp) VALUES (1, 100.0)")
    conn.commit()
    conn.close()

    def _has_table(name: str) -> bool:
        with sqlite3.connect(str(db_path)) as c:
            return (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()
                is not None
            )

    assert not _has_table("window_geometry_capture_failure")

    # Opening through the canonical session seam runs _migrate_schema.
    session = get_session_for_path(str(db_path))
    session.close()

    assert _has_table("window_geometry_capture_failure")
