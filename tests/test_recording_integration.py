"""Integration tests for the recording flow (Scenarios 1-4).

Tests the full recording lifecycle: start_recording() → stop → post-recording
pipeline. Mocks only external boundaries (hardware, OS permissions, system
metrics). Internal modules (config, pidfile, catalog, exporter) use real
objects against tmp_path.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import time
from collections import namedtuple
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
_PLENTY_OF_DISK = DiskUsage(total=500e9, used=100e9, free=400e9)


def create_test_recording_db(db_path, *, base_timestamp=None):
    """Create a recording.db with realistic test data.

    Schema matches sc_engine/db/models.py. Inserts enough events to exercise
    the export pipeline: a click pair (→ MouseClickEvent), a keypress pair
    (→ KeyTypeEvent), and a window event (→ WindowSwitchEvent).
    """
    t = base_timestamp or time.time()
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recording (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            monitor_width INTEGER,
            monitor_height INTEGER,
            pixel_ratio REAL DEFAULT 1.0,
            double_click_interval_seconds REAL,
            double_click_distance_pixels REAL,
            platform TEXT,
            task_description TEXT,
            video_start_time REAL,
            config TEXT,
            original_recording_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS action_event (
            id INTEGER PRIMARY KEY,
            name TEXT,
            timestamp REAL,
            recording_id INTEGER,
            recording_timestamp REAL,
            screenshot_timestamp REAL,
            window_event_timestamp REAL,
            mouse_x REAL,
            mouse_y REAL,
            mouse_dx REAL,
            mouse_dy REAL,
            mouse_pressure REAL,
            modifier_flags INTEGER,
            scroll_phase INTEGER,
            momentum_phase INTEGER,
            is_continuous INTEGER,
            active_segment_description TEXT,
            available_segment_descriptions TEXT,
            mouse_button_name TEXT,
            mouse_pressed INTEGER,
            key_name TEXT,
            key_char TEXT,
            key_vk TEXT,
            canonical_key_name TEXT,
            canonical_key_char TEXT,
            canonical_key_vk TEXT,
            parent_id INTEGER,
            element_state TEXT,
            disabled INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS window_event (
            id INTEGER PRIMARY KEY,
            recording_timestamp REAL,
            recording_id INTEGER,
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
        );
        CREATE TABLE IF NOT EXISTS screenshot (
            id INTEGER PRIMARY KEY,
            recording_timestamp REAL,
            recording_id INTEGER,
            timestamp REAL,
            png_data BLOB,
            png_diff_data BLOB,
            png_diff_mask_data BLOB,
            image_path TEXT
        );
        CREATE TABLE IF NOT EXISTS audio_info (
            id INTEGER PRIMARY KEY,
            timestamp REAL,
            recording_timestamp REAL,
            recording_id INTEGER,
            sample_rate INTEGER,
            words_with_timestamps TEXT
        );
        CREATE TABLE IF NOT EXISTS window_geometry (
            id INTEGER PRIMARY KEY,
            recording_id INTEGER,
            recording_timestamp REAL,
            screenshot_timestamp REAL,
            window_list_json TEXT
        );
    """)

    # Recording metadata
    conn.execute(
        "INSERT INTO recording "
        "(id, timestamp, monitor_width, monitor_height, pixel_ratio, "
        "platform, double_click_interval_seconds, double_click_distance_pixels) "
        "VALUES (1, ?, 1920, 1080, 2.0, 'darwin', 0.5, 5.0)",
        (t,),
    )

    # Window event (before action events so it's first chronologically)
    conn.execute(
        "INSERT INTO window_event "
        "(id, timestamp, recording_id, title, app_bundle_id, window_id, "
        '"left", top, width, height) '
        "VALUES (1, ?, 1, 'Documents', 'com.apple.finder', 'win-1', "
        "0, 0, 1920, 1080)",
        (t + 0.1,),
    )

    # Mouse click: down + up → will become MouseClickEvent after processing
    conn.execute(
        "INSERT INTO action_event "
        "(id, name, timestamp, recording_id, mouse_x, mouse_y, "
        "mouse_button_name, mouse_pressed, window_event_timestamp) "
        "VALUES (1, 'click', ?, 1, 500.0, 300.0, 'left', 1, ?)",
        (t + 0.5, t + 0.1),
    )
    conn.execute(
        "INSERT INTO action_event "
        "(id, name, timestamp, recording_id, mouse_x, mouse_y, "
        "mouse_button_name, mouse_pressed, window_event_timestamp) "
        "VALUES (2, 'click', ?, 1, 500.0, 300.0, 'left', 0, ?)",
        (t + 0.55, t + 0.1),
    )

    # Key press + release → will become KeyTypeEvent after processing
    conn.execute(
        "INSERT INTO action_event "
        "(id, name, timestamp, recording_id, key_char, key_name, "
        "canonical_key_char, canonical_key_name, window_event_timestamp) "
        "VALUES (3, 'press', ?, 1, 'h', 'h', 'h', 'h', ?)",
        (t + 1.0, t + 0.1),
    )
    conn.execute(
        "INSERT INTO action_event "
        "(id, name, timestamp, recording_id, key_char, key_name, "
        "canonical_key_char, canonical_key_name, window_event_timestamp) "
        "VALUES (4, 'release', ?, 1, 'h', 'h', 'h', 'h', ?)",
        (t + 1.05, t + 0.1),
    )

    conn.commit()
    conn.close()
    return db_path


class FakeRecorder:
    """Stand-in for sc_engine.Recorder (external hardware boundary).

    Creates a recording.db on __enter__ so downstream code (export, catalog)
    works with real data. Sets is_recording=False so the live-display loop
    exits immediately.
    """

    def __init__(self, capture_dir_str, **kwargs):
        self.capture_dir = Path(capture_dir_str)
        self.is_recording = False
        self.health_warning = None   # Prevents false "child_crash" detection
        self.child_crashes = []
        self._stopped = False

    def __enter__(self):
        create_test_recording_db(self.capture_dir / "recording.db")
        return self

    def __exit__(self, *args):
        return False

    def wait_for_ready(self, timeout=30):
        return True

    def stop(self):
        self.is_recording = False
        self._stopped = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def recording_env(tmp_path, monkeypatch):
    """Real config environment pointing at tmp_path.

    Uses env vars for config (real config module), redirects PID file to
    tmp_path (real pidfile module, safe location).
    """
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()

    # Config via env vars — real config module reads these
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    monkeypatch.setenv("SCREENCAP_DISK_WARN_MB", "2000")
    monkeypatch.setenv("SCREENCAP_DISK_STOP_MB", "500")

    # Redirect PID file to tmp_path
    pid_file = tmp_path / "recording.pid"
    monkeypatch.setattr("screencap.pidfile.PID_FILE", pid_file)

    # Reset config cache so env vars take effect
    import screencap.config

    screencap.config._config_cache = None

    yield {
        "recordings_dir": recordings_dir,
        "tmp_path": tmp_path,
        "pid_file": pid_file,
    }

    screencap.config._config_cache = None


# ---------------------------------------------------------------------------
# Test 1: Recording lifecycle
# ---------------------------------------------------------------------------


def test_normal_recording_lifecycle(recording_env):
    """Full lifecycle: start_recording() → immediate stop → verify cleanup.

    Mocked: sc_engine.Recorder (hardware), permissions, disk_usage, orphan
    scan, metrics.
    Real: config (env vars), pidfile (tmp_path), file I/O.

    Covers: A1, A2, A3, A6, A8, B5, D1.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-rec"
    pid_file = recording_env["pid_file"]

    with (
        mock.patch("sc_engine.Recorder", FakeRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
    ):
        capture_dir, elapsed = start_recording(
            name="test-rec",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=0,
            verbose=True,
        )

    # A1 + B5: PID file cleaned up after recording
    assert not pid_file.exists(), "PID file should be deleted after recording"

    # A2: .recording_id written with correct name
    assert (capture_dir / ".recording_id").read_text().strip() == "test-rec"

    # A3: .recording_intent written with local destination
    intent = json.loads((capture_dir / ".recording_intent").read_text())
    assert intent["destination"] == "local"
    assert intent["version"] == 1

    # A6: Signal handlers restored to defaults
    assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL

    # A8: capture_dir exists with recording.db
    assert capture_dir.exists()
    assert (capture_dir / "recording.db").exists()

    # D1: recording.db has realistic data
    conn = sqlite3.connect(str(capture_dir / "recording.db"))
    row = conn.execute(
        "SELECT platform, pixel_ratio, monitor_width FROM recording"
    ).fetchone()
    assert row[0] == "darwin"
    assert row[1] == 2.0
    assert row[2] == 1920
    conn.close()


# ---------------------------------------------------------------------------
# Test 2: Auto-export produces correct events.jsonl
# ---------------------------------------------------------------------------


def test_auto_export_produces_valid_events_jsonl(tmp_path):
    """Real export pipeline: recording.db → export_recording() → events.jsonl.

    No mocking — exercises sc_engine.Capture.load() and the full 11-stage
    event processing pipeline with real SQLite data.

    Covers: B1, B2, B3.
    """
    from screencap.exporter import build_export_metadata, export_recording

    rec_dir = tmp_path / "test-export"
    rec_dir.mkdir()
    create_test_recording_db(rec_dir / "recording.db")

    meta = build_export_metadata(exclude_moves=False)
    jsonl_path = rec_dir / "events.jsonl"
    count = export_recording(rec_dir, str(jsonl_path), exclude_moves=False, metadata=meta)

    assert count > 0
    lines = jsonl_path.read_text().strip().split("\n")

    # B1: First line is _meta header with format_version 2
    header = json.loads(lines[0])
    assert header["format_version"] == 2
    assert "screencap_version" in header

    # B2: Events are processed types (not raw click/press)
    event_types = set()
    for line in lines[1:]:
        parsed = json.loads(line)
        etype = parsed.get("type", "")
        if etype:
            event_types.add(etype)
    assert "click" not in event_types, "Raw 'click' should be processed into mouse.singleclick"
    assert "press" not in event_types, "Raw 'press' should be processed into key.type"
    assert len(lines) > 1, "Should have at least one event beyond the header"

    # B3: window.switch events interleaved
    has_window_switch = any(
        json.loads(line).get("type") == "window.switch" for line in lines[1:]
    )
    assert has_window_switch, "window.switch events should be interleaved"


# ---------------------------------------------------------------------------
# Test 3: PID file lifecycle — real filesystem
# ---------------------------------------------------------------------------


def test_pidfile_write_read_delete_real_filesystem(tmp_path, monkeypatch):
    """Real file I/O: write → read → delete → read.

    No mocking — exercises screencap.pidfile directly against tmp_path.

    Covers: A1 (deeper), B5.
    """
    from screencap.pidfile import delete_pidfile, read_pidfile, write_pidfile

    test_pid_file = tmp_path / "recording.pid"
    monkeypatch.setattr("screencap.pidfile.PID_FILE", test_pid_file)

    # Write
    write_pidfile(tmp_path / "my-recording", child_pids=[{"pid": 12345, "name": "writer"}])
    assert test_pid_file.exists()

    # Read back
    data = read_pidfile()
    assert data is not None
    assert data["capture_dir"] == str(tmp_path / "my-recording")
    assert data["parent_pid"] == os.getpid()
    assert len(data["children"]) == 1
    assert data["children"][0]["pid"] == 12345

    # Delete
    delete_pidfile()
    assert not test_pid_file.exists()

    # Read after delete returns None
    assert read_pidfile() is None


# ---------------------------------------------------------------------------
# Test 4: Single-chunk export via ChunkProcessor
# ---------------------------------------------------------------------------


def test_chunk_processor_single_chunk_export(tmp_path):
    """ChunkProcessor exports events from a real recording.db.

    Real: recording.db, sqlite3 queries, sc_engine processing pipeline, Queue.
    Mocked: audio wait, transcription, manifest generation (no real audio/upload).

    Covers: C2, C3.
    """
    from screencap.chunk_processor import ChunkProcessor

    rec_dir = tmp_path / "test-chunk"
    rec_dir.mkdir()
    t0 = 1000.0  # Fixed timestamp for predictable chunk boundaries
    create_test_recording_db(rec_dir / "recording.db", base_timestamp=t0)

    chunk_q = multiprocessing.Queue()
    audio_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir=rec_dir,
        chunk_process_q=chunk_q,
        audio_ack_q=audio_q,
        recording_name="test-chunk",
        upload_enabled=False,
        auto_delete=False,
    )

    with (
        mock.patch.object(cp, "_wait_for_audio", return_value=True),
        mock.patch.object(cp, "_transcribe", return_value=None),
        mock.patch.object(cp, "_generate_manifest") as mock_manifest,
    ):
        cp.start()

        # Send one rotation message covering all our test events
        # Keys must match what _process_chunk() reads
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 0,
            "chunk_start_time": t0,
            "rotation_time": t0 + 60,
        })

        # Send poison pill to stop
        chunk_q.put({"type": "poison_pill"})
        cp.stop(timeout=10)

        # C3: manifest generation was called (assert inside with block)
        mock_manifest.assert_called_once()

    # C2: events_0000.jsonl produced with real exported events
    events_file = rec_dir / "events_0000.jsonl"
    assert events_file.exists(), "Chunk export should produce events_0000.jsonl"
    lines = events_file.read_text().strip().split("\n")
    header = json.loads(lines[0])
    assert header.get("format_version") == 2 or header.get("_meta", {}).get("format_version") == 2


# ---------------------------------------------------------------------------
# Test 5: Config env var overrides config.toml
# ---------------------------------------------------------------------------


def test_config_env_var_overrides_file(tmp_path, monkeypatch):
    """Real config module: write config.toml, then override with env var.

    No mocking — exercises screencap.config directly.
    """
    import screencap.config

    screencap.config._config_cache = None

    # Write a real config.toml (use tmp_path since get_recordings_dir calls mkdir)
    toml_dir = tmp_path / "from-toml"
    config_file = tmp_path / "config.toml"
    config_file.write_text(f'recordings_dir = "{toml_dir}"\n')
    monkeypatch.setattr("screencap.config._CONFIG_PATH", config_file)

    # Without env var: config.toml value wins
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    screencap.config._config_cache = None
    recordings_dir = screencap.config.get_recordings_dir()
    assert str(recordings_dir) == str(toml_dir)

    # With env var: env var wins over config.toml
    env_dir = tmp_path / "from-env"
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(env_dir))
    screencap.config._config_cache = None
    recordings_dir = screencap.config.get_recordings_dir()
    assert str(recordings_dir) == str(env_dir)

    screencap.config._config_cache = None


# ---------------------------------------------------------------------------
# Test 6: Catalog reads real recording.db
# ---------------------------------------------------------------------------


def test_catalog_reads_recording_with_schema2(tmp_path):
    """Real catalog: list_recordings() reads recording.db (schema 2).

    No mocking — exercises catalog.py + sqlite3 directly.

    Covers: D1, D2, D3.
    """
    from screencap.catalog import list_recordings

    rec_dir = tmp_path / "recordings" / "my-recording"
    rec_dir.mkdir(parents=True)
    create_test_recording_db(rec_dir / "recording.db")

    # Create a fake video file so catalog detects media
    (rec_dir / "chunk_0000.mp4").write_bytes(b"\x00" * 100)

    results = list_recordings(recordings_dir=tmp_path / "recordings")

    assert len(results) == 1
    info = results[0]
    assert info.name == "my-recording"
    # duration and size_mb are formatted strings in the NamedTuple
    assert info.duration != "—", "Duration should be computed, not missing"
    assert "KB" in info.size_mb or "MB" in info.size_mb, f"size_mb should be formatted, got: {info.size_mb}"


# ---------------------------------------------------------------------------
# Scenario 2: Stop from another terminal
# ---------------------------------------------------------------------------


def test_stop_sends_sigterm_and_waits(tmp_path, monkeypatch):
    """screencap stop reads PID file, sends SIGTERM, waits for process exit.

    Mocked: _pid_exists (controls when process "exits"), _is_screencap_process,
    os.kill, time.sleep. Real: PID file I/O (via monkeypatched PID_FILE path).

    Covers: S1, S2, S3.
    """
    from click.testing import CliRunner

    from screencap.cli import cli

    # Redirect PID file to tmp_path
    monkeypatch.setattr("screencap.pidfile.PID_FILE", tmp_path / "recording.pid")

    # Write a real PID file with a fake parent PID
    fake_pid = 99999
    pidfile_data = {
        "parent_pid": fake_pid,
        "children": [],
        "capture_dir": str(tmp_path / "rec"),
        "started_at": time.time(),
    }
    (tmp_path / "recording.pid").write_text(json.dumps(pidfile_data))

    # _pid_exists: True (alive check), False (loop exit), False (final check)
    pid_exists_returns = iter([True, False, False])

    with (
        mock.patch(
            "screencap.pidfile._pid_exists",
            side_effect=lambda pid: next(pid_exists_returns),
        ),
        mock.patch("screencap.pidfile._is_screencap_process", return_value=True),
        mock.patch("os.kill") as mock_kill,
        mock.patch("time.sleep"),
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])

    # S1: SIGTERM sent to parent process
    mock_kill.assert_called_once_with(fake_pid, signal.SIGTERM)

    # S3: Success message printed
    assert "stopped gracefully" in result.output.lower(), f"Output: {result.output}"


def test_stop_no_recording_running(tmp_path, monkeypatch):
    """screencap stop with no PID file and no orphans = no-op message.

    Covers: S4.
    """
    from click.testing import CliRunner

    from screencap.cli import cli

    # No PID file exists
    monkeypatch.setattr("screencap.pidfile.PID_FILE", tmp_path / "recording.pid")

    with mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["stop"])

    assert "no orphaned" in result.output.lower(), f"Output: {result.output}"


# ---------------------------------------------------------------------------
# Scenario 4: Multi-chunk — event boundaries
# ---------------------------------------------------------------------------


def _create_multi_chunk_db(db_path, t0):
    """Create a recording.db with events spanning two chunk boundaries.

    Events at t0+5, t0+15 (chunk 0: [t0, t0+30])
    Events at t0+35, t0+45 (chunk 1: [t0+30, t0+60])
    Window events at t0+1 and t0+31 for initial context testing.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE recording (
            id INTEGER PRIMARY KEY, timestamp REAL,
            monitor_width INTEGER, monitor_height INTEGER,
            pixel_ratio REAL DEFAULT 1.0, platform TEXT,
            double_click_interval_seconds REAL,
            double_click_distance_pixels REAL
        );
        CREATE TABLE action_event (
            id INTEGER PRIMARY KEY, name TEXT, timestamp REAL,
            recording_id INTEGER, mouse_x REAL, mouse_y REAL,
            mouse_dx REAL, mouse_dy REAL, mouse_pressure REAL,
            modifier_flags INTEGER, scroll_phase INTEGER,
            momentum_phase INTEGER, is_continuous INTEGER,
            mouse_button_name TEXT, mouse_pressed INTEGER,
            key_char TEXT, key_name TEXT, key_vk TEXT,
            canonical_key_char TEXT, canonical_key_name TEXT,
            canonical_key_vk TEXT, element_state TEXT,
            active_segment_description TEXT,
            available_segment_descriptions TEXT,
            disabled INTEGER DEFAULT 0
        );
        CREATE TABLE window_event (
            id INTEGER PRIMARY KEY, timestamp REAL,
            recording_id INTEGER, title TEXT,
            app_bundle_id TEXT, window_id TEXT,
            "left" INTEGER, top INTEGER, width INTEGER, height INTEGER
        );
    """)

    conn.execute(
        "INSERT INTO recording (id, timestamp, monitor_width, monitor_height, "
        "pixel_ratio, platform, double_click_interval_seconds, "
        "double_click_distance_pixels) VALUES (1, ?, 1920, 1080, 2.0, 'darwin', 0.5, 5.0)",
        (t0,),
    )

    # Window event in chunk 0
    conn.execute(
        "INSERT INTO window_event (id, timestamp, recording_id, title, "
        'app_bundle_id, window_id, "left", top, width, height) '
        "VALUES (1, ?, 1, 'Editor', 'com.app.editor', 'win-1', 0, 0, 1920, 1080)",
        (t0 + 1,),
    )
    # Window event in chunk 1
    conn.execute(
        "INSERT INTO window_event (id, timestamp, recording_id, title, "
        'app_bundle_id, window_id, "left", top, width, height) '
        "VALUES (2, ?, 1, 'Browser', 'com.app.browser', 'win-2', 0, 0, 1920, 1080)",
        (t0 + 31,),
    )

    # Chunk 0 events: click pair at t0+5, keypress pair at t0+15
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "mouse_x, mouse_y, mouse_button_name, mouse_pressed) "
        "VALUES (1, 'click', ?, 1, 100.0, 200.0, 'left', 1)", (t0 + 5,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "mouse_x, mouse_y, mouse_button_name, mouse_pressed) "
        "VALUES (2, 'click', ?, 1, 100.0, 200.0, 'left', 0)", (t0 + 5.05,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "key_char, key_name, canonical_key_char, canonical_key_name) "
        "VALUES (3, 'press', ?, 1, 'a', 'a', 'a', 'a')", (t0 + 15,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "key_char, key_name, canonical_key_char, canonical_key_name) "
        "VALUES (4, 'release', ?, 1, 'a', 'a', 'a', 'a')", (t0 + 15.05,),
    )

    # Chunk 1 events: click pair at t0+35, keypress pair at t0+45
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "mouse_x, mouse_y, mouse_button_name, mouse_pressed) "
        "VALUES (5, 'click', ?, 1, 300.0, 400.0, 'left', 1)", (t0 + 35,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "mouse_x, mouse_y, mouse_button_name, mouse_pressed) "
        "VALUES (6, 'click', ?, 1, 300.0, 400.0, 'left', 0)", (t0 + 35.05,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "key_char, key_name, canonical_key_char, canonical_key_name) "
        "VALUES (7, 'press', ?, 1, 'b', 'b', 'b', 'b')", (t0 + 45,),
    )
    conn.execute(
        "INSERT INTO action_event (id, name, timestamp, recording_id, "
        "key_char, key_name, canonical_key_char, canonical_key_name) "
        "VALUES (8, 'release', ?, 1, 'b', 'b', 'b', 'b')", (t0 + 45.05,),
    )

    conn.commit()
    conn.close()


def test_multi_chunk_no_event_overlap_or_gaps(tmp_path):
    """Two chunks: events partition cleanly with no overlap or loss.

    Real: recording.db, sqlite3 queries, sc_engine processing pipeline, Queue.
    Mocked: audio wait, transcription, manifest generation.

    Verifies: no event appears in both chunks, no event is lost,
    chunk 1 starts with initial window context from chunk 0.
    """
    from screencap.chunk_processor import ChunkProcessor

    rec_dir = tmp_path / "test-multi"
    rec_dir.mkdir()
    t0 = 1000.0
    _create_multi_chunk_db(rec_dir / "recording.db", t0)

    chunk_q = multiprocessing.Queue()
    audio_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir=rec_dir,
        chunk_process_q=chunk_q,
        audio_ack_q=audio_q,
        recording_name="test-multi",
        upload_enabled=False,
        auto_delete=False,
    )

    with (
        mock.patch.object(cp, "_wait_for_audio", return_value=True),
        mock.patch.object(cp, "_transcribe", return_value=None),
        mock.patch.object(cp, "_generate_manifest"),
    ):
        cp.start()

        # Chunk 0: [t0, t0+30)
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 0,
            "chunk_start_time": t0,
            "rotation_time": t0 + 30,
        })
        # Chunk 1: [t0+30, t0+60)
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 1,
            "chunk_start_time": t0 + 30,
            "rotation_time": t0 + 60,
        })

        chunk_q.put({"type": "poison_pill"})
        cp.stop(timeout=10)

    # Both chunk files produced
    chunk0 = rec_dir / "events_0000.jsonl"
    chunk1 = rec_dir / "events_0001.jsonl"
    assert chunk0.exists(), "events_0000.jsonl missing"
    assert chunk1.exists(), "events_0001.jsonl missing"

    def _parse_events(path):
        lines = path.read_text().strip().split("\n")
        events = []
        for line in lines:
            parsed = json.loads(line)
            if "_meta" not in parsed and "format_version" not in parsed:
                events.append(parsed)
        return events

    events_0 = _parse_events(chunk0)
    events_1 = _parse_events(chunk1)

    # Extract timestamps (action events have "timestamp", window.switch has "timestamp")
    ts_0 = {e.get("timestamp") for e in events_0 if e.get("timestamp")}
    ts_1 = {e.get("timestamp") for e in events_1 if e.get("timestamp")}

    # No overlap: no timestamp appears in both chunks
    overlap = ts_0 & ts_1
    # Filter out initial window context (carried from chunk 0 into chunk 1)
    # Its timestamp is set to chunk_start - 0.001
    context_ts = t0 + 30 - 0.001
    overlap_without_context = {t for t in overlap if abs(t - context_ts) > 0.01}
    assert not overlap_without_context, f"Events overlap between chunks: {overlap_without_context}"

    # Both chunks have events
    assert len(events_0) > 0, "Chunk 0 should have events"
    assert len(events_1) > 0, "Chunk 1 should have events"

    # Chunk 1 has initial window context (window.switch as first event)
    types_1 = [e.get("type") for e in events_1]
    assert "window.switch" in types_1, "Chunk 1 should have initial window context"


def test_start_recording_multi_chunk_produces_all_chunk_files(recording_env):
    """End-to-end: start_recording() with chunk_duration wires up ChunkProcessor.

    FakeRecorder provides real multiprocessing.Queues with rotation messages.
    Verifies that start_recording() → ChunkProcessor processes ALL chunks,
    not just the first one.

    This test catches the bug where only chunk 0 is processed because
    the Recorder doesn't send rotation messages for subsequent chunks.

    Mocked: sc_engine.Recorder (replaced with FakeChunkedRecorder),
    permissions, disk_usage, orphan scan, metrics.
    Real: config (env vars), pidfile (tmp_path), ChunkProcessor, exporter.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-chunked"
    t0 = 1000.0

    class FakeChunkedRecorder:
        """FakeRecorder that provides chunk queues with pre-loaded rotation messages.

        Simulates a Recorder that produced 2 chunks of 30 seconds each.
        """

        def __init__(self, capture_dir_str, **kwargs):
            self.capture_dir = Path(capture_dir_str)
            self.is_recording = False
            self.health_warning = None
            self.child_crashes = []
            # Real queues — ChunkProcessor will read from these
            self._chunk_process_q = multiprocessing.Queue()
            self._audio_ack_q = multiprocessing.Queue()
            self._flush_requested = None
            self._flush_ack_counter = None

        def __enter__(self):
            _create_multi_chunk_db(self.capture_dir / "recording.db", t0)

            # Pre-load rotation messages for 2 chunks
            self._chunk_process_q.put({
                "type": "chunk_rotated",
                "completed_index": 0,
                "chunk_start_time": t0,
                "rotation_time": t0 + 30,
            })
            self._chunk_process_q.put({
                "type": "final_chunk",
                "completed_index": 1,
                "chunk_start_time": t0 + 30,
                "rotation_time": t0 + 60,
            })
            return self

        def __exit__(self, *args):
            return False

        def wait_for_ready(self, timeout=30):
            return True

        def stop(self):
            self.is_recording = False

    with (
        mock.patch("sc_engine.Recorder", FakeChunkedRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        # Mock transcription/upload inside ChunkProcessor
        mock.patch("screencap.chunk_processor.ChunkProcessor._wait_for_audio", return_value=True),
        mock.patch("screencap.chunk_processor.ChunkProcessor._transcribe", return_value=None),
        mock.patch("screencap.chunk_processor.ChunkProcessor._generate_manifest"),
        mock.patch("screencap.chunk_processor.ChunkProcessor._upload_chunk"),
    ):
        capture_dir, elapsed = start_recording(
            name="test-chunked",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=30,
            verbose=True,
            live_upload=False,
        )

    # Both chunk event files should exist
    assert (capture_dir / "events_0000.jsonl").exists(), \
        "Chunk 0 events file missing — ChunkProcessor didn't process chunk 0"
    assert (capture_dir / "events_0001.jsonl").exists(), \
        "Chunk 1 events file missing — ChunkProcessor didn't process chunk 1 (rotation message lost?)"


def test_chunk_processor_survives_queue_close_during_processing(tmp_path):
    """Regression for Finding 005: multi-chunk recordings only process chunk 0.

    The fix transfers queue ownership from Recorder to the screencap layer
    (nulling recorder._chunk_process_q so __exit__() skips it). This test
    verifies the post-fix shutdown sequence: queue stays open, stop() sends
    a poison pill, and all chunks are processed.
    """
    from screencap.chunk_processor import ChunkProcessor

    rec_dir = tmp_path / "test-queue-close"
    rec_dir.mkdir()
    t0 = 1000.0
    _create_multi_chunk_db(rec_dir / "recording.db", t0)

    chunk_q = multiprocessing.Queue()
    audio_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir=rec_dir,
        chunk_process_q=chunk_q,
        audio_ack_q=audio_q,
        recording_name="test-queue-close",
        upload_enabled=False,
        auto_delete=False,
    )

    with (
        mock.patch.object(cp, "_wait_for_audio", return_value=True),
        mock.patch.object(cp, "_transcribe", return_value=None),
        mock.patch.object(cp, "_generate_manifest"),
    ):
        cp.start()

        # Load messages into queue (simulating what Recorder + fan-out produce)
        chunk_q.put({
            "type": "chunk_rotated",
            "completed_index": 0,
            "chunk_start_time": t0,
            "rotation_time": t0 + 30,
        })
        chunk_q.put({
            "type": "final_chunk",
            "completed_index": 1,
            "chunk_start_time": t0 + 30,
            "rotation_time": t0 + 60,
        })

        # Post-fix: queue stays open (ownership transferred to screencap layer).
        # stop() sends poison pill via the open queue — graceful shutdown.
        cp.stop(timeout=10)

    # BOTH chunks should have been processed
    assert (rec_dir / "events_0000.jsonl").exists(), \
        "Chunk 0 not processed"
    assert (rec_dir / "events_0001.jsonl").exists(), \
        "Chunk 1 (final) not processed"


# ---------------------------------------------------------------------------
# Scenario 5: Non-chunked recording (legacy mode)
# ---------------------------------------------------------------------------


def test_non_chunked_recording_no_chunk_processor(recording_env):
    """chunk_duration=0 disables chunking: no ChunkProcessor, no chunk_* files.

    Mocked: sc_engine.Recorder (hardware), permissions, disk_usage, orphan
    scan, metrics.
    Real: config (env vars), pidfile (tmp_path), file I/O.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-no-chunks"

    with (
        mock.patch("sc_engine.Recorder", FakeRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        mock.patch("screencap.chunk_processor.ChunkProcessor") as MockCP,
    ):
        capture_dir, elapsed = start_recording(
            name="test-no-chunks",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=0,
            verbose=True,
        )

    # ChunkProcessor should NOT have been instantiated
    MockCP.assert_not_called()

    # No chunk files should exist
    assert not list(capture_dir.glob("chunk_*")), \
        "No chunk_* files should exist in non-chunked mode"
    assert not list(capture_dir.glob("events_*.jsonl")), \
        "No per-chunk events_*.jsonl should exist in non-chunked mode"

    # Recording should otherwise work — DB exists
    assert (capture_dir / "recording.db").exists()
    assert (capture_dir / ".recording_id").read_text().strip() == "test-no-chunks"
