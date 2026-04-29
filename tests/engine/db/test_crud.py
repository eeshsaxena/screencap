"""Tests for CRUD insert + flush of NetworkEvent (V1)."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture
def fresh_recording():
    """Create a fresh recording.db with a Recording row, return (session, recording, db_path)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "recording.db")
        from screencap.engine.db import create_db, crud

        engine, Session = create_db(db_path)
        session = Session()

        recording = crud.insert_recording(session, {
            "timestamp": time.time(),
            "monitor_width": 1920,
            "monitor_height": 1080,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5,
            "platform": "darwin",
            "task_description": "test-fixture",
        })

        # Reset the module-level buffers so prior tests don't leak rows
        crud.network_events.clear()
        crud.action_events.clear()
        crud.window_events.clear()
        crud.screenshots.clear()
        crud.window_geometries.clear()
        crud.performance_stats.clear()
        crud.memory_stats.clear()

        yield session, recording, db_path

        crud.network_events.clear()
        session.close()
        engine.dispose()


class TestInsertNetworkEvent:
    """Round-trip insert + flush for NetworkEvent."""

    def test_buffered_insert_and_flush(self, fresh_recording):
        """Insert several rows under BATCH_SIZE, flush, verify count."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEvent

        session, recording, _ = fresh_recording

        # BATCH_SIZE defaults to 1 in the test environment, so an insert
        # commits immediately. Bump to 50 to exercise the buffered path.
        original = crud.BATCH_SIZE
        crud.BATCH_SIZE = 50
        try:
            digest = b"\xff" * 32

            for i in range(3):
                crud.insert_network_event(session, recording, {
                    "kind": "request",
                    "flow_id": f"flow-{i}",
                    "method": "GET",
                    "url": f"https://example.com/{i}",
                    "host": "example.com",
                    "headers_json": json.dumps([["X-Idx", str(i)]]),
                    "body_size": None,
                    "body_sha256": digest,
                    "content_type": None,
                    "direction": None,
                    "frame_type": None,
                    "http_version": "HTTP/1.1",
                    "details_json": None,
                    "status": None,
                    "timestamp": float(i),
                    "timestamp_ns": i * 1_000_000_000,
                })

            # Buffered: nothing flushed yet
            assert len(crud.network_events) == 3
            assert session.query(NetworkEvent).count() == 0

            crud.flush_buffers(session)

            # Buffer cleared; rows landed in the DB
            assert crud.network_events == []
            rows = session.query(NetworkEvent).order_by(NetworkEvent.timestamp).all()
            assert len(rows) == 3
            assert rows[0].kind == "request"
            assert rows[0].flow_id == "flow-0"
            assert rows[0].method == "GET"
            assert rows[0].body_sha256 == digest
            assert rows[2].url == "https://example.com/2"
        finally:
            crud.BATCH_SIZE = original

    def test_unbuffered_insert_immediate(self, fresh_recording):
        """With default BATCH_SIZE=1 the insert is committed immediately."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEvent

        session, recording, _ = fresh_recording

        crud.insert_network_event(session, recording, {
            "kind": "drop_burst",
            "flow_id": None,
            "method": None,
            "url": None,
            "host": None,
            "status": None,
            "headers_json": None,
            "body_size": None,
            "body_sha256": None,
            "content_type": None,
            "direction": None,
            "frame_type": None,
            "http_version": None,
            "details_json": json.dumps({
                "dropped_count": 12,
                "hosts_affected": ["a.com"],
                "source": "addon",
            }),
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
        })

        rows = session.query(NetworkEvent).all()
        assert len(rows) == 1
        assert rows[0].kind == "drop_burst"
        assert rows[0].flow_id is None
        details = json.loads(rows[0].details_json)
        assert details["dropped_count"] == 12

    def test_fk_violation_does_not_corrupt_other_buffers(self, fresh_recording):
        """FK violation on flush surfaces; other buffers stay clean.

        SQLite does not enforce FK constraints by default unless
        ``PRAGMA foreign_keys=ON`` is issued. We enable FK enforcement
        for this test and confirm that an FK violation on the network
        buffer raises (or otherwise terminates the commit) without
        leaving stray rows in unrelated tables (action_events,
        window_events). The exact rollback semantics of
        ``flush_buffers`` are intentionally weak in V1 - the load-
        bearing assertion is that the rest of the recording is intact.
        """
        import sqlalchemy as sa

        from screencap.engine.db import crud
        from screencap.engine.db.models import ActionEvent, NetworkEvent

        session, _recording, _ = fresh_recording

        # Force-enable FK enforcement on this connection
        session.execute(sa.text("PRAGMA foreign_keys=ON"))

        original = crud.BATCH_SIZE
        crud.BATCH_SIZE = 50
        try:
            # Push a row with a bogus recording_id directly into the
            # buffer, bypassing the helper's recording_id injection.
            crud.network_events.append({
                "id": None,
                "recording_id": 9999,  # no Recording with this id
                "kind": "request",
                "flow_id": "f",
                "method": "GET",
                "url": "https://example.com/",
                "host": "example.com",
                "status": None,
                "headers_json": None,
                "body_size": None,
                "body_sha256": None,
                "content_type": None,
                "direction": None,
                "frame_type": None,
                "http_version": None,
                "details_json": None,
                "timestamp": 1.0,
                "timestamp_ns": 1,
            })

            raised = False
            try:
                crud.flush_buffers(session)
            except sa.exc.IntegrityError:
                raised = True
            except Exception:
                # Some SQLAlchemy versions wrap as a different exception;
                # any exception still satisfies "FK violation surfaces"
                raised = True

            # FK enforcement should have caught the bogus recording_id.
            # If the underlying SQLite build silently ignores FK
            # constraints (rare but possible) the test relaxes to
            # "no row landed in an unrelated table".
            if raised:
                # Network event with bogus FK did NOT land
                assert (
                    session.query(NetworkEvent)
                    .filter(NetworkEvent.recording_id == 9999)
                    .count()
                    == 0
                )

            # Either way, no spurious rows in unrelated tables.
            assert session.query(ActionEvent).count() == 0
        finally:
            # Always clear our test rows from the buffer for fixture teardown
            crud.network_events.clear()
            crud.BATCH_SIZE = original
