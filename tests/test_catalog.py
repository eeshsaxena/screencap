"""Tests for screencap.catalog."""

import json
import sqlite3
import time
from pathlib import Path

import pytest

from screencap.catalog import find_db, list_recordings, read_intent


@pytest.fixture
def recordings_dir(tmp_path):
    return tmp_path / "recordings"


def _make_recording(base: Path, name: str, *, audio: bool = False, duration: float = 60.0):
    """Create a minimal mock recording directory with recording.db."""
    d = base / name
    d.mkdir(parents=True)

    db_path = d / "recording.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE recording (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            monitor_width INTEGER,
            monitor_height INTEGER,
            double_click_interval_seconds REAL,
            double_click_distance_pixels REAL,
            platform TEXT,
            task_description TEXT,
            video_start_time REAL,
            config TEXT,
            original_recording_id INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE action_event (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            recording_id INTEGER,
            name TEXT
        )
    """)

    started = time.time() - duration
    cur.execute(
        "INSERT INTO recording (id, timestamp, platform) VALUES (1, ?, 'darwin')",
        (started,),
    )
    cur.execute(
        "INSERT INTO action_event (id, timestamp, recording_id, name) VALUES (1, ?, 1, 'click')",
        (started + duration,),
    )
    conn.commit()
    conn.close()

    if audio:
        (d / "audio.flac").write_bytes(b"fake")

    return d


def test_list_empty(recordings_dir):
    recordings_dir.mkdir(parents=True)
    result = list_recordings(recordings_dir)
    assert result == []


def test_list_nonexistent(tmp_path):
    result = list_recordings(tmp_path / "nope")
    assert result == []


def test_list_single(recordings_dir):
    _make_recording(recordings_dir, "test1", audio=True, duration=120)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "test1"
    assert result[0].has_audio is True
    assert "2m" in result[0].duration


def test_list_multiple(recordings_dir):
    _make_recording(recordings_dir, "alpha", duration=60)
    _make_recording(recordings_dir, "beta", audio=True, duration=300)
    result = list_recordings(recordings_dir)
    assert len(result) == 2
    names = [r.name for r in result]
    assert "alpha" in names
    assert "beta" in names


def test_skips_non_recording_dirs(recordings_dir):
    recordings_dir.mkdir(parents=True)
    # Dir without recording.db should be skipped
    (recordings_dir / "random_dir").mkdir()
    result = list_recordings(recordings_dir)
    assert len(result) == 0


def test_finds_capture_db(recordings_dir):
    """Recordings with capture.db (legacy name) should also be found."""
    d = recordings_dir / "legacy-rec"
    d.mkdir(parents=True)
    db_path = d / "capture.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL, platform TEXT)")
    cur.execute("CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL)")
    started = time.time() - 45
    cur.execute("INSERT INTO recording VALUES (1, ?, 'darwin')", (started,))
    cur.execute("INSERT INTO action_event VALUES (1, ?)", (started + 45,))
    conn.commit()
    conn.close()

    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "legacy-rec"


def test_find_db_prefers_recording_db(tmp_path):
    """If both recording.db and capture.db exist, recording.db wins."""
    (tmp_path / "recording.db").touch()
    (tmp_path / "capture.db").touch()
    assert find_db(tmp_path).name == "recording.db"


def test_find_db_returns_none(tmp_path):
    assert find_db(tmp_path) is None


# --- read_intent tests ---


@pytest.mark.parametrize("destination", ["cloud", "local"])
def test_read_intent_returns_destination(tmp_path, destination):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": destination,
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    assert read_intent(tmp_path) == destination


def test_read_intent_missing(tmp_path):
    assert read_intent(tmp_path) is None


def test_read_intent_corrupt(tmp_path):
    intent_path = tmp_path / ".recording_intent"
    intent_path.write_text("NOT VALID JSON {{{")
    assert read_intent(tmp_path) is None


# --- list_recordings intent integration tests ---


def test_list_recordings_with_intent(recordings_dir):
    d = _make_recording(recordings_dir, "cloud-rec", duration=30)
    intent_path = d / ".recording_intent"
    intent_path.write_text(
        json.dumps(
            {
                "version": 1,
                "destination": "cloud",
                "privacy_mode": "public",
                "created_at": "2026-03-07T14:30:00Z",
                "source": "flag",
            }
        )
    )
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "cloud-rec"
    assert result[0].intent == "cloud"


def test_list_recordings_legacy_no_intent(recordings_dir):
    _make_recording(recordings_dir, "legacy-rec", duration=45)
    result = list_recordings(recordings_dir)
    assert len(result) == 1
    assert result[0].name == "legacy-rec"
    assert result[0].intent is None
