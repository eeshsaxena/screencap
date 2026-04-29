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

    def test_v1_does_not_create_network_event_meta(self, legacy_capture_dir):
        """V1 does NOT create network_event_meta - that's V1.5.

        Locks the V1 scope: if a future V1.5 implementation lands the
        meta table, this test must be updated. Catching its premature
        appearance here closes the V1/V1.5 leakage gap.
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
            assert cur.fetchone() is None, (
                "V1 must NOT create network_event_meta - that table is V1.5."
            )
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
