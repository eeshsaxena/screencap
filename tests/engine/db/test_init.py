"""Tests for DB init / migration helpers (V1 network logging slice).

Covers:
- ``_ensure_network_tables`` adds the ``network_event`` table to a
  recording.db that pre-dates the network feature, without disturbing
  existing rows.
- ``Capture.export_events()`` continues to work after the migration.
- A readonly recording.db (chmod 444) skips migration and surfaces the
  ``Capture._network_tables_unavailable`` instance attribute. Capture
  load must not crash.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

import pytest


def _create_legacy_recording_db(db_path: str) -> None:
    """Create a recording.db that lacks the network_event table.

    Mimics the schema before the network feature landed: only the
    `recording` table is required for `Capture.load()` to succeed.
    Other tables follow whatever the live schema declares so the
    other migration paths don't false-positive.
    """
    # Use a fresh declarative base to ensure we don't cache test artifacts
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(db_path)
    session = Session()

    recording_data = {
        "timestamp": time.time(),
        "monitor_width": 1920,
        "monitor_height": 1080,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5,
        "platform": "darwin",
        "task_description": "legacy-fixture",
    }
    crud.insert_recording(session, recording_data)
    session.close()
    engine.dispose()

    # Now drop the network_event table from the just-created DB so it
    # looks like a recording.db from before the network feature.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS network_event")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def legacy_capture_dir():
    """Create a capture dir with a recording.db that lacks network_event."""
    with tempfile.TemporaryDirectory() as tmpdir:
        capture_dir = Path(tmpdir) / "legacy_capture"
        capture_dir.mkdir()
        db_path = capture_dir / "recording.db"
        _create_legacy_recording_db(str(db_path))

        # Sanity: the table really is absent from the legacy fixture
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='network_event'"
            )
            assert cur.fetchone() is None, "fixture should NOT have network_event"
        finally:
            conn.close()

        yield capture_dir


class TestEnsureNetworkTables:
    """V1: _ensure_network_tables creates network_event idempotently."""

    def test_creates_table_on_legacy_db(self, legacy_capture_dir):
        """Loading a legacy DB adds network_event without disturbing rows."""
        from screencap.engine.capture import Capture

        # Load should NOT crash even though network_event was missing
        capture = Capture.load(legacy_capture_dir)
        try:
            # Recording row preserved
            assert capture._recording is not None
            assert capture._recording.task_description == "legacy-fixture"
            # No readonly flag - DB is writable
            assert getattr(capture, "_network_tables_unavailable", False) is False
        finally:
            capture.close()

        # network_event table now exists
        db_path = legacy_capture_dir / "recording.db"
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='network_event'"
            )
            assert cur.fetchone() is not None, "network_event should be created"
            cur = conn.execute("PRAGMA table_info(network_event)")
            cols = {row[1] for row in cur.fetchall()}
            # Spot-check columns
            assert "kind" in cols
            assert "flow_id" in cols
            assert "headers_json" in cols
            assert "details_json" in cols
            assert "body_sha256" in cols
            assert "timestamp_ns" in cols
        finally:
            conn.close()

    def test_idempotent_on_second_load(self, legacy_capture_dir):
        """Second Capture.load() is a no-op (checkfirst=True path)."""
        from screencap.engine.capture import Capture

        capture1 = Capture.load(legacy_capture_dir)
        capture1.close()

        # Second load should not crash, should not raise on existing table
        capture2 = Capture.load(legacy_capture_dir)
        try:
            assert capture2._recording is not None
        finally:
            capture2.close()

    def test_export_events_works_after_migration(self, legacy_capture_dir):
        """V1: Capture.export_events() returns events without crashing.

        Legacy recording with the network_event table freshly added
        should still produce its (empty) export_events list cleanly.
        """
        from screencap.engine.capture import Capture

        capture = Capture.load(legacy_capture_dir)
        try:
            events = capture.export_events()
            # No action events were inserted, so the list is empty.
            # The important assertion is that it doesn't crash.
            assert events == []
        finally:
            capture.close()

    def test_v1_5_creates_network_event_meta(self, legacy_capture_dir):
        """V1.5: _ensure_network_tables also creates network_event_meta.

        V1 scope was metadata-only and intentionally omitted this table;
        V1.5 introduces per-recording KEK-wrapped DEK metadata for body
        encryption-at-rest. Loading a recording.db that pre-dates V1.5
        must create both `network_event` and `network_event_meta`.
        """
        from screencap.engine.capture import Capture

        capture = Capture.load(legacy_capture_dir)
        capture.close()

        db_path = legacy_capture_dir / "recording.db"
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='network_event_meta'"
            )
            assert cur.fetchone() is not None, (
                "V1.5: network_event_meta should be created by "
                "_ensure_network_tables."
            )
            # Spot-check expected columns
            cur = conn.execute("PRAGMA table_info(network_event_meta)")
            cols = {row[1] for row in cur.fetchall()}
            assert "recording_id" in cols
            assert "dek_wrapped" in cols
            assert "dek_nonce" in cols
            assert "created_at" in cols
            # Locked: no kek_version column in V1.5 (rotation deferred to V2)
            assert "kek_version" not in cols, (
                "V1.5 explicitly omits kek_version - KEK rotation is V2."
            )
        finally:
            conn.close()

    def test_ensure_network_tables_idempotent_on_meta(self, legacy_capture_dir):
        """V1.5: second call to _ensure_network_tables must be a no-op for meta.

        Mirrors the existing `test_idempotent_on_second_load` behavior for
        the new V1.5 meta table - ensures `Table.create(engine, checkfirst=True)`
        works for both tables.
        """
        from screencap.engine.capture import Capture

        # First load creates both tables
        capture1 = Capture.load(legacy_capture_dir)
        capture1.close()

        # Second load must not crash on existing meta table
        capture2 = Capture.load(legacy_capture_dir)
        try:
            assert capture2._recording is not None
        finally:
            capture2.close()

        # Both tables still present
        db_path = legacy_capture_dir / "recording.db"
        conn = sqlite3.connect(str(db_path))
        try:
            for table_name in ("network_event", "network_event_meta"):
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name=?",
                    (table_name,),
                )
                assert cur.fetchone() is not None, f"{table_name} should still exist"
        finally:
            conn.close()

    def test_v1_75_creates_network_health(self, legacy_capture_dir):
        """V1.75: _ensure_network_tables also creates network_health.

        Old recordings that never had proxy-lifecycle observability will
        simply not have any rows in this table; the chunk processor
        treats absence-of-row as "no incident" at export time.
        """
        from screencap.engine.capture import Capture

        capture = Capture.load(legacy_capture_dir)
        capture.close()

        db_path = legacy_capture_dir / "recording.db"
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='network_health'"
            )
            assert cur.fetchone() is not None, (
                "V1.75: network_health should be created by "
                "_ensure_network_tables."
            )
            cur = conn.execute("PRAGMA table_info(network_health)")
            cols = {row[1] for row in cur.fetchall()}
            for required in ("recording_id", "event", "timestamp_ns", "details"):
                assert required in cols, (
                    f"network_health is missing required column {required}"
                )

            # The (recording_id, timestamp_ns) compound index backs the
            # chunk-overlap query in U5; verify it landed.
            cur = conn.execute("PRAGMA index_list(network_health)")
            index_names = {row[1] for row in cur.fetchall()}
            assert "ix_network_health_recording_ts" in index_names, (
                "V1.75: ix_network_health_recording_ts must back the "
                "chunk overlap query."
            )
        finally:
            conn.close()

    def test_ensure_network_tables_idempotent_on_health(self, legacy_capture_dir):
        """V1.75: second call to _ensure_network_tables must be a no-op for health."""
        from screencap.engine.capture import Capture

        capture1 = Capture.load(legacy_capture_dir)
        capture1.close()

        capture2 = Capture.load(legacy_capture_dir)
        try:
            assert capture2._recording is not None
        finally:
            capture2.close()

        db_path = legacy_capture_dir / "recording.db"
        conn = sqlite3.connect(str(db_path))
        try:
            for table_name in (
                "network_event", "network_event_meta", "network_health",
            ):
                cur = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name=?",
                    (table_name,),
                )
                assert cur.fetchone() is not None, f"{table_name} should still exist"
        finally:
            conn.close()


class TestReadonlyDatabase:
    """V1: readonly recording.db must not crash; flag is set on Capture."""

    def test_readonly_db_load_succeeds_with_flag(self, legacy_capture_dir):
        """chmod 444 a legacy DB; load works, flag is True, no crash."""
        from screencap.engine.capture import Capture

        # First make sure the legacy DB has network_event REMOVED so
        # _ensure_network_tables actually attempts a write under chmod 444.
        db_path = legacy_capture_dir / "recording.db"

        # Drop network_event again (the fixture starts without it; but defensive)
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DROP TABLE IF EXISTS network_event")
            conn.commit()
        finally:
            conn.close()

        # Make the DB AND its directory readonly. SQLite needs to write
        # journal files to the parent directory too.
        os.chmod(str(db_path), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(str(legacy_capture_dir),
                 stat.S_IRUSR | stat.S_IXUSR
                 | stat.S_IRGRP | stat.S_IXGRP
                 | stat.S_IROTH | stat.S_IXOTH)

        try:
            capture = Capture.load(legacy_capture_dir)
            try:
                # Load did not crash
                assert capture._recording is not None
                # Flag was set on the instance
                assert getattr(capture, "_network_tables_unavailable", False) is True
            finally:
                capture.close()
        finally:
            # Restore permissions for cleanup
            os.chmod(str(legacy_capture_dir),
                     stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            os.chmod(str(db_path), stat.S_IRUSR | stat.S_IWUSR)
