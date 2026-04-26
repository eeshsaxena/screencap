"""Tests for ``screencap.recording_db`` helpers."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from screencap.recording_db import Row, has_column, has_table, open_recording_db


def _make_current_schema_db(db_path: Path) -> None:
    """Create a recording.db with all evolving columns + window_geometry table."""
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, task_description TEXT)")
    cur.execute(
        "CREATE TABLE window_event (id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, window_id INTEGER, browser_url TEXT)"
    )
    cur.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, name TEXT, timestamp REAL, "
        "element_state TEXT)"
    )
    cur.execute(
        "CREATE TABLE screenshot (id INTEGER PRIMARY KEY, timestamp REAL, image_path TEXT, png_data BLOB)"
    )
    cur.execute(
        "CREATE TABLE window_geometry (id INTEGER PRIMARY KEY, screenshot_timestamp REAL, "
        "window_list_json TEXT)"
    )
    conn.commit()
    conn.close()


def _make_old_schema_db(db_path: Path) -> None:
    """Create a recording.db missing the evolving columns + window_geometry table.

    Mirrors the pre-migration shape: ``window_event`` has no ``browser_url``,
    ``action_event`` has no ``element_state``, ``screenshot`` has no
    ``image_path``, and ``window_geometry`` does not exist at all.
    """
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)")
    cur.execute(
        "CREATE TABLE window_event (id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, window_id INTEGER)"
    )
    cur.execute("CREATE TABLE action_event (id INTEGER PRIMARY KEY, name TEXT, timestamp REAL)")
    cur.execute("CREATE TABLE screenshot (id INTEGER PRIMARY KEY, timestamp REAL, png_data BLOB)")
    conn.commit()
    conn.close()


class TestOpenRecordingDb:
    def test_yields_usable_connection(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert isinstance(conn, sqlite3.Connection)
            assert conn.execute("SELECT 1").fetchone() == (1,)

    def test_read_only_sets_query_only_pragma(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path, read_only=True) as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    def test_writer_mode_leaves_query_only_off(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path, read_only=False) as conn:
            assert conn.execute("PRAGMA query_only").fetchone()[0] == 0
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000

    def test_row_factory_parameter_wires_column_by_name_access(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path, row_factory=Row) as conn:
            row = conn.execute("SELECT 1 AS x").fetchone()
            assert row["x"] == 1

    def test_writer_round_trip(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path, read_only=False) as conn:
            conn.execute("INSERT INTO recording (id, timestamp) VALUES (1, 1.0)")
            conn.commit()
        with open_recording_db(db_path) as conn:
            assert conn.execute("SELECT timestamp FROM recording WHERE id=1").fetchone() == (1.0,)

    def test_succeeds_without_wal_or_shm_sidecars(self, tmp_path):
        """Post-upload simulation: only the .db file exists, no .db-wal/.db-shm.

        Confirms ``query_only=ON`` works where ``mode=ro`` URI would fail.
        """
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src_db = src_dir / "recording.db"
        _make_current_schema_db(src_db)
        # Touch WAL/SHM in the source dir to be safe, then copy only the .db.
        with open_recording_db(src_db, read_only=False) as conn:
            conn.execute("INSERT INTO recording (id, timestamp) VALUES (1, 1.0)")
            conn.commit()

        dst_dir = tmp_path / "dst"
        dst_dir.mkdir()
        dst_db = dst_dir / "recording.db"
        shutil.copyfile(src_db, dst_db)
        # Sanity: no sidecars exist in dst.
        assert not (dst_dir / "recording.db-wal").exists()
        assert not (dst_dir / "recording.db-shm").exists()

        with open_recording_db(dst_db) as conn:
            row = conn.execute("SELECT timestamp FROM recording WHERE id=1").fetchone()
            assert row == (1.0,)

    def test_missing_path_raises_before_sqlite_connect(self, tmp_path):
        """``FileNotFoundError`` fires *before* ``sqlite3.connect`` would create an empty DB."""
        missing = tmp_path / "nope.db"
        with pytest.raises(FileNotFoundError):
            with open_recording_db(missing):
                pytest.fail("body should not execute")
        # Crucially: the file was never created on disk.
        assert not missing.exists()

    def test_corrupted_db_propagates_database_error(self, tmp_path):
        bad = tmp_path / "bad.db"
        bad.write_bytes(b"not a sqlite database, definitely not a header")
        with pytest.raises(sqlite3.DatabaseError):
            with open_recording_db(bad) as conn:
                conn.execute("SELECT 1 FROM sqlite_master").fetchone()

    def test_closes_connection_on_exception_in_body(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        captured = {}
        with pytest.raises(RuntimeError):
            with open_recording_db(db_path) as conn:
                captured["conn"] = conn
                raise RuntimeError("boom")
        # Closed connections raise ProgrammingError on use.
        with pytest.raises(sqlite3.ProgrammingError):
            captured["conn"].execute("SELECT 1")


class TestHasTable:
    def test_returns_true_for_existing_table(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert has_table(conn, "recording") is True
            assert has_table(conn, "window_event") is True
            assert has_table(conn, "window_geometry") is True

    def test_returns_false_for_missing_table(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_old_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert has_table(conn, "window_geometry") is False
            assert has_table(conn, "nonexistent") is False


class TestHasColumn:
    def test_returns_true_for_existing_column(self, tmp_path):
        db_path = tmp_path / "recording.db"
        _make_current_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert has_column(conn, "window_event", "browser_url") is True
            assert has_column(conn, "action_event", "element_state") is True
            assert has_column(conn, "screenshot", "image_path") is True

    def test_returns_false_for_missing_column_on_old_schema(self, tmp_path):
        """R2.6: old-schema fixture — every evolving column reports False."""
        db_path = tmp_path / "recording.db"
        _make_old_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert has_column(conn, "window_event", "browser_url") is False
            assert has_column(conn, "action_event", "element_state") is False
            assert has_column(conn, "screenshot", "image_path") is False
            assert has_table(conn, "window_geometry") is False

    def test_missing_table_does_not_raise(self, tmp_path):
        """``PRAGMA table_info`` on a missing table returns an empty cursor."""
        db_path = tmp_path / "recording.db"
        _make_old_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            assert has_column(conn, "no_such_table", "anything") is False

    def test_rejects_non_identifier_table_name(self, tmp_path):
        """A SQL-injection-shaped table name is rejected before PRAGMA runs."""
        db_path = tmp_path / "recording.db"
        _make_old_schema_db(db_path)
        with open_recording_db(db_path) as conn:
            with pytest.raises(ValueError):
                has_column(conn, "recording; DROP TABLE recording", "id")
            with pytest.raises(ValueError):
                has_column(conn, "with space", "id")
