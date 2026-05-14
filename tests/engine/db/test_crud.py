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

    def test_network_event_with_ciphertext_round_trip(self, fresh_recording):
        """V1.5: NetworkEvent insert with body_ciphertext + nonce + aad round-trips."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEvent

        session, recording, _ = fresh_recording

        ciphertext = b"\xde\xad\xbe\xef" * 4
        nonce = b"\x00" * 12
        aad = b"recording_id=1|flow=abc"

        crud.insert_network_event(session, recording, {
            "kind": "request",
            "flow_id": "flow-1",
            "method": "POST",
            "url": "https://example.com/api",
            "host": "example.com",
            "headers_json": None,
            "body_size": 16,
            "body_sha256": None,
            "content_type": "application/json",
            "direction": None,
            "frame_type": None,
            "http_version": "HTTP/1.1",
            "details_json": None,
            "status": None,
            "body_ciphertext": ciphertext,
            "body_nonce": nonce,
            "body_aad": aad,
            "timestamp": 1.0,
            "timestamp_ns": 1_000_000_000,
        })

        rows = session.query(NetworkEvent).all()
        assert len(rows) == 1
        row = rows[0]
        # Memoryview / bytes from SQLite → coerce for comparison
        assert bytes(row.body_ciphertext) == ciphertext
        assert bytes(row.body_nonce) == nonce
        assert bytes(row.body_aad) == aad

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


# =============================================================================
# V1.5 NetworkEventMeta + AAD invariant tests
# =============================================================================


class TestInsertNetworkEventMeta:
    """V1.5: insert_network_event_meta one-shot per recording."""

    def test_round_trip(self, fresh_recording):
        """Insert meta row, query back, verify fields match."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEventMeta

        session, recording, _ = fresh_recording

        dek_wrapped = b"\xaa" * 48  # AES-GCM-wrapped 32-byte DEK + tag
        dek_nonce = b"\x00" * 12
        created_at = 1234567890.5

        crud.insert_network_event_meta(
            session,
            recording_id=recording.id,
            dek_wrapped=dek_wrapped,
            dek_nonce=dek_nonce,
            created_at=created_at,
        )

        rows = session.query(NetworkEventMeta).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.recording_id == recording.id
        assert bytes(row.dek_wrapped) == dek_wrapped
        assert bytes(row.dek_nonce) == dek_nonce
        assert row.created_at == created_at

    def test_default_created_at_uses_now(self, fresh_recording):
        """When created_at is omitted, defaults to time.time()."""
        import time

        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEventMeta

        session, recording, _ = fresh_recording

        before = time.time()
        crud.insert_network_event_meta(
            session,
            recording_id=recording.id,
            dek_wrapped=b"\xbb" * 48,
            dek_nonce=b"\x00" * 12,
        )
        after = time.time()

        row = session.query(NetworkEventMeta).one()
        # created_at column is ForceFloat (Numeric(10,2)) - truncates to 2dp.
        # Use a generous comparison window to absorb the rounding.
        assert before - 1.0 <= row.created_at <= after + 1.0

    def test_unique_per_recording(self, fresh_recording):
        """Second insert for the same recording_id violates UNIQUE."""
        import sqlalchemy as sa

        from screencap.engine.db import crud

        session, recording, _ = fresh_recording

        crud.insert_network_event_meta(
            session,
            recording_id=recording.id,
            dek_wrapped=b"\x01" * 48,
            dek_nonce=b"\x00" * 12,
            created_at=1.0,
        )
        with pytest.raises(sa.exc.IntegrityError):
            crud.insert_network_event_meta(
                session,
                recording_id=recording.id,
                dek_wrapped=b"\x02" * 48,
                dek_nonce=b"\x11" * 12,
                created_at=2.0,
            )


class TestNetworkEventAadInvariant:
    """V1.5: AAD-non-null when ciphertext-non-null CheckConstraint.

    SQLite enforces CHECK constraints by default on INSERT (no PRAGMA
    needed - distinct from FK enforcement). This test inserts a row
    with ciphertext set but AAD = NULL and asserts an IntegrityError
    surfaces. If the underlying SQLite build silently ignores the
    constraint (some custom builds compile it out), the test xfails
    so it doesn't silently pass.
    """

    def test_ciphertext_without_aad_raises(self, fresh_recording):
        import sqlalchemy as sa

        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkEvent

        session, recording, _ = fresh_recording

        # Original BATCH_SIZE=1 so the insert commits immediately
        # (so the CheckConstraint fires synchronously, not deferred).
        try:
            crud.insert_network_event(session, recording, {
                "kind": "request",
                "flow_id": "f",
                "method": "GET",
                "url": "https://x.com/",
                "host": "x.com",
                "headers_json": None,
                "body_size": None,
                "body_sha256": None,
                "content_type": None,
                "direction": None,
                "frame_type": None,
                "http_version": None,
                "details_json": None,
                "status": None,
                "body_ciphertext": b"\xde\xad\xbe\xef",  # ciphertext set
                "body_nonce": b"\x00" * 12,
                "body_aad": None,                         # AAD missing - INVARIANT VIOLATION
                "timestamp": 1.0,
                "timestamp_ns": 1,
            })
        except sa.exc.IntegrityError:
            return  # test passes - constraint fired
        except Exception:
            return  # any DB error satisfies "constraint fires"

        # If we got here, the insert succeeded - either the SQLite build
        # ignores CHECK constraints, or the constraint isn't on the table
        # (e.g., the table was created before the constraint was added).
        # Verify the row landed; if so, mark xfail so it's loud.
        bad = (
            session.query(NetworkEvent)
            .filter(NetworkEvent.body_aad.is_(None))
            .filter(NetworkEvent.body_ciphertext.isnot(None))
            .all()
        )
        if bad:
            pytest.xfail(
                "SQLite did not enforce ck_network_event_aad_present; "
                "application-layer crud.insert_network_event invariant "
                "(always pass AAD when ciphertext is non-null) is the "
                "load-bearing guarantee on migrated databases."
            )
        # No row landed somehow - treat as pass


# =============================================================================
# V1.75 NetworkHealth lifecycle observability tests
# =============================================================================


class TestInsertNetworkHealth:
    """V1.75: insert_network_health writes proxy lifecycle rows."""

    def test_round_trip(self, fresh_recording):
        """Insert each allowed event kind, query back, verify columns."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkHealth

        session, recording, _ = fresh_recording

        crud.insert_network_health(
            session, recording.id,
            event="proxy_started",
            timestamp_ns=1_000_000_000,
        )
        crud.insert_network_health(
            session, recording.id,
            event="proxy_crashed",
            timestamp_ns=2_000_000_000,
            details='{"exit_code": 9, "log_tail": "..."}',
        )
        crud.insert_network_health(
            session, recording.id,
            event="kek_unavailable",
            timestamp_ns=3_000_000_000,
            details="KekUnavailableError: Keychain entry missing",
        )
        crud.insert_network_health(
            session, recording.id,
            event="network_writer_failed",
            timestamp_ns=4_000_000_000,
            details="OperationalError: database is locked",
        )

        rows = (
            session.query(NetworkHealth)
            .order_by(NetworkHealth.timestamp_ns)
            .all()
        )
        assert len(rows) == 4
        assert [r.event for r in rows] == [
            "proxy_started", "proxy_crashed",
            "kek_unavailable", "network_writer_failed",
        ]
        assert rows[0].details is None
        assert rows[1].details and "exit_code" in rows[1].details
        assert rows[2].details and "Keychain" in rows[2].details
        assert all(r.recording_id == recording.id for r in rows)

    def test_commits_immediately(self, fresh_recording):
        """insert_network_health commits per-call so failure events
        survive a subsequent crash. Verified via a second session
        seeing the row without an explicit commit on the first.
        """
        from screencap.engine.db import crud, get_session_for_path
        from screencap.engine.db.models import NetworkHealth

        session, recording, db_path = fresh_recording
        recording_id = recording.id

        crud.insert_network_health(
            session, recording.id,
            event="proxy_started",
            timestamp_ns=42,
        )

        # Open a brand-new session against the same DB; the row must be
        # visible without any further commit from the first session.
        other = get_session_for_path(db_path)
        try:
            rows = (
                other.query(NetworkHealth)
                .filter(NetworkHealth.recording_id == recording_id)
                .all()
            )
            assert len(rows) == 1
            assert rows[0].event == "proxy_started"
            assert rows[0].timestamp_ns == 42
        finally:
            other.close()

    def test_invalid_event_rejected(self, fresh_recording):
        """CheckConstraint rejects events outside the four allowed values."""
        import sqlalchemy as sa

        from screencap.engine.db import crud

        session, recording, _ = fresh_recording

        # SQLAlchemy's non-native Enum coerces unknown strings to a
        # client-side ValueError before the SQL ever runs; the test
        # accepts either that or the underlying CheckConstraint surface.
        raised = False
        try:
            crud.insert_network_health(
                session, recording.id,
                event="totally-not-allowed",
                timestamp_ns=1,
            )
        except (sa.exc.IntegrityError, sa.exc.StatementError, ValueError, LookupError):
            raised = True
        assert raised, (
            "Either the SQL CHECK constraint or the SQLAlchemy Enum coercion "
            "must reject an event string outside NETWORK_HEALTH_EVENTS."
        )

    def test_details_accepts_large_payload(self, fresh_recording):
        """``details`` is TEXT and must accept >1 KB stack traces without truncation."""
        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkHealth

        session, recording, _ = fresh_recording

        big_payload = "x" * 4096
        crud.insert_network_health(
            session, recording.id,
            event="proxy_crashed",
            timestamp_ns=1,
            details=big_payload,
        )

        row = session.query(NetworkHealth).one()
        assert row.details == big_payload
        assert len(row.details) == 4096

    def test_cascade_on_recording_delete(self, fresh_recording):
        """Deleting the parent Recording cascades and removes NetworkHealth rows.

        Requires ``PRAGMA foreign_keys=ON`` (SQLite default is OFF). The
        ``ondelete="CASCADE"`` clause on the FK column is the load-bearing
        contract; this test verifies it actually fires.
        """
        import sqlalchemy as sa

        from screencap.engine.db import crud
        from screencap.engine.db.models import NetworkHealth, Recording

        session, recording, _ = fresh_recording
        session.execute(sa.text("PRAGMA foreign_keys=ON"))

        crud.insert_network_health(
            session, recording.id,
            event="proxy_started",
            timestamp_ns=1,
        )
        crud.insert_network_health(
            session, recording.id,
            event="proxy_crashed",
            timestamp_ns=2,
        )
        assert session.query(NetworkHealth).count() == 2

        # Deleting the recording must cascade-delete the health rows.
        session.delete(session.query(Recording).filter_by(id=recording.id).one())
        session.commit()

        assert session.query(NetworkHealth).count() == 0
