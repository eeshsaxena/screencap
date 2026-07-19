"""Tests for the scrub-time window-geometry readers (SCR-33 U4).

The DB-dependent, scrub-only slice split out of the original privacy
``context`` module now lives in
``screencap.redaction.geometry``. These assertions are the geometry half of
the former ``tests/privacy/test_context.py`` characterization baseline.
"""

from __future__ import annotations

import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

from screencap.redaction.geometry import (
    WindowContext,
    associate_screenshot,
    find_nearest_window,
    list_screenshot_timestamps,
    load_window_events,
    parse_screenshot_timestamp,
)

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Screenshot timestamp parsing
# ---------------------------------------------------------------------------


class TestParseScreenshotTimestamp:
    def test_parses_recorder_filename_format(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpg") == pytest.approx(
            1709745600.123456
        )

    def test_parses_jpeg_extension(self):
        assert parse_screenshot_timestamp("1709745600.123456.jpeg") == pytest.approx(
            1709745600.123456
        )

    def test_parses_db_image_path_with_directory_prefix(self):
        # image_path in DB is stored as "screenshots/{ts}.jpg"
        assert parse_screenshot_timestamp("screenshots/1709745600.123456.jpg") == pytest.approx(
            1709745600.123456
        )

    def test_parses_encrypted_corpus_form(self):
        # Under corpus_encrypted, stills (and DB image_path values) carry the
        # ``.jpg.enc`` suffix; the timestamp parse must see through it.
        assert parse_screenshot_timestamp("1709745600.123456.jpg.enc") == pytest.approx(
            1709745600.123456
        )
        assert parse_screenshot_timestamp(
            "screenshots/1709745600.123456.jpg.enc"
        ) == pytest.approx(1709745600.123456)

    @pytest.mark.parametrize("filename", [
        "1709745600.123456.png",
        "screenshot.jpg",
    ])
    def test_rejects_non_parseable_filenames(self, filename):
        assert parse_screenshot_timestamp(filename) is None


class TestListScreenshotTimestamps:
    def test_lists_plaintext_and_encrypted_stills_once(self, tmp_path):
        """The flat-frame enumeration must see the encrypted-corpus ``.jpg.enc``
        form — a ``.jpg``-only glob silently drops the frame-presence signal for
        every encrypted library (the aggregate / day-timeline / backfill
        coverage inputs). A frame present as both forms mid-migration counts
        once."""
        shots = tmp_path / "screenshots"
        shots.mkdir()
        (shots / "100.5.jpg").write_bytes(b"\xff\xd8\xff")
        (shots / "200.5.jpg.enc").write_bytes(b"enc")
        # Mid-migration: the same frame in both forms.
        (shots / "300.5.jpg").write_bytes(b"\xff\xd8\xff")
        (shots / "300.5.jpg.enc").write_bytes(b"enc")
        (shots / "not-a-frame.txt").write_text("x")
        assert list_screenshot_timestamps(tmp_path) == [100.5, 200.5, 300.5]


# ---------------------------------------------------------------------------
# Nearest-event lookup (bisect logic)
# ---------------------------------------------------------------------------


class TestFindNearestWindow:
    def _make_events(self, timestamps: list[float]) -> list[WindowContext]:
        return [
            WindowContext(timestamp=ts, app_bundle_id=f"app{i}", title=f"title{i}")
            for i, ts in enumerate(timestamps)
        ]

    def test_exact_match(self):
        events = self._make_events([1.0, 2.0, 3.0])
        result = find_nearest_window(events, 2.0)
        assert result is not None
        assert result.timestamp == 2.0

    @pytest.mark.parametrize("target,expected_ts", [
        (1.3, 1.0),  # after first event, before second
        (2.8, 1.0),  # still before second event — latest-at-or-before is first
        (3.5, 3.0),  # after second event — latest-at-or-before is second
    ])
    def test_picks_latest_at_or_before(self, target, expected_ts):
        events = self._make_events([1.0, 3.0])
        result = find_nearest_window(events, target)
        assert result is not None
        assert result.timestamp == expected_ts

    def test_rejects_target_before_all_events(self):
        events = self._make_events([5.0, 10.0])
        assert find_nearest_window(events, 1.0) is None

    def test_rejects_events_beyond_max_delta(self):
        events = self._make_events([1.0])
        assert find_nearest_window(events, 10.0, max_delta=5.0) is None


# ---------------------------------------------------------------------------
# DB loaders (integration with real SQLite)
# ---------------------------------------------------------------------------


def _create_test_db(tmp_path: Path) -> Path:
    """Create a test SQLite DB with window_event table."""
    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE window_event (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            recording_timestamp REAL,
            timestamp REAL,
            state TEXT,
            title TEXT,
            "left" INTEGER,
            top INTEGER,
            width INTEGER,
            height INTEGER,
            window_id TEXT,
            app_bundle_id TEXT,
            app_version TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


class TestLoadWindowEvents:
    def test_loads_and_sorts_by_timestamp(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # Insert out of order to verify ORDER BY
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (2.0, 'com.apple.mail', 'Inbox', 'w1')"
        )
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id) "
            "VALUES (1.0, 'com.apple.Safari', 'Google', 'w2')"
        )
        conn.commit()
        conn.close()

        events = load_window_events(db_path)
        assert len(events) == 2
        assert events[0].timestamp == 1.0
        assert events[0].app_bundle_id == "com.apple.Safari"
        assert events[1].timestamp == 2.0

    def test_missing_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        assert load_window_events(db_path) == []

    def test_null_timestamps_excluded(self, tmp_path):
        db_path = _create_test_db(tmp_path)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title) "
            "VALUES (NULL, 'com.foo', 'Bar')"
        )
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title) "
            "VALUES (1.0, 'com.bar', 'Baz')"
        )
        conn.commit()
        conn.close()
        assert len(load_window_events(db_path)) == 1

    def test_browser_url_loaded_and_domain_extracted(self, tmp_path):
        """browser_url column is loaded and domain is extracted from it."""
        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE window_event (
                id INTEGER PRIMARY KEY,
                recording_id INTEGER,
                recording_timestamp REAL,
                timestamp REAL,
                state TEXT,
                title TEXT,
                "left" INTEGER,
                top INTEGER,
                width INTEGER,
                height INTEGER,
                window_id TEXT,
                app_bundle_id TEXT,
                app_version TEXT,
                browser_url TEXT
            )
        """)
        conn.execute(
            "INSERT INTO window_event (timestamp, app_bundle_id, title, window_id, browser_url) "
            "VALUES (1.0, 'com.google.Chrome', 'Gmail', 'w1', 'https://mail.google.com/inbox')"
        )
        conn.commit()
        conn.close()
        events = load_window_events(db_path)
        assert len(events) == 1
        assert events[0].browser_url == "https://mail.google.com/inbox"
        assert events[0].domain == "mail.google.com"


# ---------------------------------------------------------------------------
# associate_screenshot — integration
# ---------------------------------------------------------------------------


class TestAssociateScreenshot:
    def test_correlates_window_by_timestamp(self):
        windows = [
            WindowContext(timestamp=1.0, app_bundle_id="com.google.Chrome", title="Gmail"),
        ]
        meta = associate_screenshot(1.5, windows)
        assert meta.bundle_id == "com.google.Chrome"
        assert meta.domain is None
        assert meta.timestamp == 1.5

    def test_no_events_returns_empty_metadata(self):
        meta = associate_screenshot(1.0, [])
        assert meta.bundle_id == ""
        assert meta.domain is None

    def test_non_browser_window(self):
        """Switching from Chrome to Finder picks up the correct window."""
        windows = [
            WindowContext(timestamp=8.0, app_bundle_id="com.google.Chrome", title="Gmail"),
            WindowContext(timestamp=10.0, app_bundle_id="com.apple.Finder", title="Documents"),
        ]
        meta = associate_screenshot(10.0, windows)
        assert meta.bundle_id == "com.apple.Finder"
        assert meta.domain is None


# ---------------------------------------------------------------------------
# DAG guard: the shared classifier slice must stay free of recording_db
# ---------------------------------------------------------------------------


def test_classify_module_does_not_import_recording_db():
    """``screencap.privacy.classify`` must not pull in ``screencap.recording_db``.

    The classifier slice is the shared leaf both the capture and scrub paths
    construct; if it imported the DB layer, the leaf would no longer be a
    clean, DB-free shared core. Only ``redaction.geometry`` carries that dep.
    """
    code = (
        "import screencap.privacy.classify, sys; "
        "assert 'screencap.recording_db' not in sys.modules, "
        "'classify must not import recording_db'; "
        "print('classify clean of recording_db')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "classify clean of recording_db" in result.stdout
