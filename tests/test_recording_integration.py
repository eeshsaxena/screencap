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
    """Create a recording.db with realistic test data using the engine API.

    Uses screencap.engine.db.create_db + crud so the schema always matches
    the real engine. Inserts a click pair, keypress pair, and window event.
    """
    from screencap.engine.db import create_db, crud

    t = base_timestamp or time.time()
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": t,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    crud.insert_window_event(session, recording, t + 0.1, {
        "title": "Documents",
        "app_bundle_id": "com.apple.finder",
        "window_id": "win-1",
        "left": 0, "top": 0, "width": 1920, "height": 1080,
    })

    crud.insert_action_event(session, recording, t + 0.5, {
        "name": "click",
        "mouse_x": 500.0, "mouse_y": 300.0,
        "mouse_button_name": "left", "mouse_pressed": True,
        "window_event_timestamp": t + 0.1,
    })
    crud.insert_action_event(session, recording, t + 0.55, {
        "name": "click",
        "mouse_x": 500.0, "mouse_y": 300.0,
        "mouse_button_name": "left", "mouse_pressed": False,
        "window_event_timestamp": t + 0.1,
    })

    crud.insert_action_event(session, recording, t + 1.0, {
        "name": "press",
        "key_char": "h", "key_name": "h",
        "canonical_key_char": "h", "canonical_key_name": "h",
        "window_event_timestamp": t + 0.1,
    })
    crud.insert_action_event(session, recording, t + 1.05, {
        "name": "release",
        "key_char": "h", "key_name": "h",
        "canonical_key_char": "h", "canonical_key_name": "h",
        "window_event_timestamp": t + 0.1,
    })

    session.close()
    engine.dispose()
    return db_path


# Import shared FakeRecorder from conftest (available via pytest fixture discovery)
from tests.conftest import FakeRecorder


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

    Mocked: screencap.engine.recorder.Recorder (hardware), permissions, disk_usage, orphan
    scan, metrics.
    Real: config (env vars), pidfile (tmp_path), file I/O.

    Covers: A1, A2, A3, A6, A8, B5, D1.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-rec"
    pid_file = recording_env["pid_file"]

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
    ):
        capture_dir, elapsed, _, _ = start_recording(
            name="test-rec",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=0,
            verbose=True,
        )

    # A1 + B5: PID file lifecycle is the daemon supervisor's responsibility
    # post-Phase-2 (see daemon/supervisor.py). InheritLock.register_children /
    # release are no-ops, so this engine-level test cannot meaningfully
    # assert pid_file.exists() either way — coverage moved to
    # ``test_pidfile_write_read_delete_real_filesystem`` (primitive-level)
    # and the daemon supervisor integration suite.

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

    No mocking — exercises screencap.engine.Capture.load() and the full 11-stage
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

    Real: recording.db, sqlite3 queries, screencap.engine processing pipeline, Queue.
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

    Uses screencap.engine.db API so the schema always matches production.
    Events at t0+5, t0+15 (chunk 0: [t0, t0+30])
    Events at t0+35, t0+45 (chunk 1: [t0+30, t0+60])
    Window events at t0+1 and t0+31 for initial context testing.
    """
    from screencap.engine.db import create_db, crud

    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": t0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    # Window events
    crud.insert_window_event(session, recording, t0 + 1, {
        "title": "Editor", "app_bundle_id": "com.app.editor",
        "window_id": "win-1", "left": 0, "top": 0, "width": 1920, "height": 1080,
    })
    crud.insert_window_event(session, recording, t0 + 31, {
        "title": "Browser", "app_bundle_id": "com.app.browser",
        "window_id": "win-2", "left": 0, "top": 0, "width": 1920, "height": 1080,
    })

    # Chunk 0 events
    crud.insert_action_event(session, recording, t0 + 5, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    crud.insert_action_event(session, recording, t0 + 5.05, {
        "name": "click", "mouse_x": 100.0, "mouse_y": 200.0,
        "mouse_button_name": "left", "mouse_pressed": False,
    })
    crud.insert_action_event(session, recording, t0 + 15, {
        "name": "press", "key_char": "a", "key_name": "a",
        "canonical_key_char": "a", "canonical_key_name": "a",
    })
    crud.insert_action_event(session, recording, t0 + 15.05, {
        "name": "release", "key_char": "a", "key_name": "a",
        "canonical_key_char": "a", "canonical_key_name": "a",
    })

    # Chunk 1 events
    crud.insert_action_event(session, recording, t0 + 35, {
        "name": "click", "mouse_x": 300.0, "mouse_y": 400.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    crud.insert_action_event(session, recording, t0 + 35.05, {
        "name": "click", "mouse_x": 300.0, "mouse_y": 400.0,
        "mouse_button_name": "left", "mouse_pressed": False,
    })
    crud.insert_action_event(session, recording, t0 + 45, {
        "name": "press", "key_char": "b", "key_name": "b",
        "canonical_key_char": "b", "canonical_key_name": "b",
    })
    crud.insert_action_event(session, recording, t0 + 45.05, {
        "name": "release", "key_char": "b", "key_name": "b",
        "canonical_key_char": "b", "canonical_key_name": "b",
    })

    session.close()
    engine.dispose()


def test_multi_chunk_no_event_overlap_or_gaps(tmp_path):
    """Two chunks: events partition cleanly with no overlap or loss.

    Real: recording.db, sqlite3 queries, screencap.engine processing pipeline, Queue.
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

    Mocked: screencap.engine.recorder.Recorder (replaced with FakeChunkedRecorder),
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
            self.health_warning = ""
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

        def finalize_pipeline(self):
            return None

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeChunkedRecorder),
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
        capture_dir, elapsed, _, _ = start_recording(
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

    Mocked: screencap.engine.recorder.Recorder (hardware), permissions, disk_usage, orphan
    scan, metrics.
    Real: config (env vars), pidfile (tmp_path), file I/O.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-no-chunks"

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        mock.patch("screencap.chunk_processor.ChunkProcessor") as MockCP,
    ):
        capture_dir, elapsed, _, _ = start_recording(
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


# ---------------------------------------------------------------------------
# Scenario 6: Guard gate — privacy failure must not cause data loss
# ---------------------------------------------------------------------------


def test_stub_recording_not_called_when_uploads_disabled(recording_env):
    """T4: Privacy pipeline failure → stub_recording() never called → files preserved.

    When cloud_intent=True and the privacy pipeline fails to init,
    all_chunks_uploaded() returns False. The recorder shutdown path must NOT
    call stub_recording(), preserving all local media files.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-guard-gate"
    t0 = 1000.0

    class FakeCloudRecorder:
        def __init__(self, capture_dir_str, **kwargs):
            self.capture_dir = Path(capture_dir_str)
            self.is_recording = False
            self.health_warning = ""
            self.child_crashes = []
            self._chunk_process_q = multiprocessing.Queue()
            self._audio_ack_q = multiprocessing.Queue()
            self._flush_requested = None
            self._flush_ack_counter = None

        def __enter__(self):
            _create_multi_chunk_db(self.capture_dir / "recording.db", t0)
            # Create media files that must be preserved
            for i in range(2):
                (self.capture_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
                (self.capture_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 512)

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

        def finalize_pipeline(self):
            return None

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeCloudRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        # Privacy pipeline fails → uploads disabled
        mock.patch(
            "screencap.privacy.create_default_pipeline",
            side_effect=ImportError("test: no privacy deps"),
        ),
        # Mock side effects inside ChunkProcessor
        mock.patch("screencap.chunk_processor.ChunkProcessor._wait_for_audio", return_value=True),
        mock.patch("screencap.chunk_processor.ChunkProcessor._transcribe", return_value=None),
        mock.patch("screencap.chunk_processor.ChunkProcessor._generate_manifest"),
        # stub_recording must NOT be called
        mock.patch("screencap.chunk_processor.stub_recording") as mock_stub,
        # upload_sentinel must NOT be called
        mock.patch("screencap.chunk_processor.upload_sentinel") as mock_sentinel,
    ):
        capture_dir, elapsed, _, _ = start_recording(
            name="test-guard-gate",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=30,
            verbose=True,
            live_upload=True,
            cloud_intent=True,
        )

    # stub_recording must NOT have been called — data loss prevention
    mock_stub.assert_not_called()
    # sentinel must NOT have been uploaded (uploads disabled)
    mock_sentinel.assert_not_called()

    # ALL media files must still exist
    for i in range(2):
        assert (capture_dir / f"chunk_{i:04d}.mp4").exists(), \
            f"chunk_{i:04d}.mp4 was deleted — data loss! stub_recording() ran when it shouldn't"
        assert (capture_dir / f"audio_{i:04d}.flac").exists(), \
            f"audio_{i:04d}.flac was deleted — data loss!"


def test_upload_warning_surfaced_at_stop(recording_env):
    """T5: When upload_warning is set, the follow-up surfaces the specific failure reason.

    The recorder writes ``.upload_followup.json`` with ``kind="upload_disabled"``
    and the specific warning text; the CLI / session controller prints it
    (via ``print_upload_followup``) after any post-recording rename, so the
    suggested ``screencap upload <name>`` command matches the final on-disk
    directory.
    """
    from screencap.recorder import print_upload_followup, start_recording

    rec_dir = recording_env["recordings_dir"] / "test-warning"
    t0 = 1000.0

    class FakeCloudRecorder:
        def __init__(self, capture_dir_str, **kwargs):
            self.capture_dir = Path(capture_dir_str)
            self.is_recording = False
            self.health_warning = ""
            self.child_crashes = []
            self._chunk_process_q = multiprocessing.Queue()
            self._audio_ack_q = multiprocessing.Queue()
            self._flush_requested = None
            self._flush_ack_counter = None

        def __enter__(self):
            _create_multi_chunk_db(self.capture_dir / "recording.db", t0)
            self._chunk_process_q.put({
                "type": "final_chunk",
                "completed_index": 0,
                "chunk_start_time": t0,
                "rotation_time": t0 + 30,
            })
            return self

        def __exit__(self, *args):
            return False

        def wait_for_ready(self, timeout=30):
            return True

        def stop(self):
            self.is_recording = False

        def finalize_pipeline(self):
            return None

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeCloudRecorder),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        mock.patch(
            "screencap.privacy.create_default_pipeline",
            side_effect=ImportError("test: missing GLiNER models"),
        ),
        mock.patch("screencap.chunk_processor.ChunkProcessor._wait_for_audio", return_value=True),
        mock.patch("screencap.chunk_processor.ChunkProcessor._transcribe", return_value=None),
        mock.patch("screencap.chunk_processor.ChunkProcessor._generate_manifest"),
    ):
        capture_dir, elapsed, _, _ = start_recording(
            name="test-warning",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=30,
            verbose=True,
            live_upload=True,
            cloud_intent=True,
        )

    # The recorder must have written the follow-up state with the
    # upload-disabled kind + a concrete warning message.
    followup = json.loads((capture_dir / ".upload_followup.json").read_text())
    assert followup["kind"] == "upload_disabled"
    assert followup["upload_warning"], "upload_warning text must be populated"

    # The CLI renders the message via print_upload_followup after any rename.
    captured_output = []
    import screencap.recorder as recorder_mod

    def _spy_print(*args, **kwargs):
        captured_output.append(" ".join(str(a) for a in args))

    with mock.patch.object(recorder_mod.console, "print", side_effect=_spy_print):
        print_upload_followup("test-warning", capture_dir)

    assert any("Uploads disabled:" in line for line in captured_output), (
        f"print_upload_followup must render 'Uploads disabled:'. "
        f"Got:\n" + "\n".join(captured_output)
    )
    assert any("screencap upload test-warning" in line for line in captured_output)


def test_sentinel_not_uploaded_without_sentinel_for_cloud(recording_env):
    """T6: Cloud-intent: stub_recording() requires _sentinel_uploaded=True.

    When all chunks upload successfully but sentinel upload fails,
    stub_recording() must NOT be called — even if _db_uploaded is True.
    Without the sentinel, Cloud Run stitching never triggers, so deleting
    local files would make the recording unrecoverable.
    """
    from screencap.recorder import start_recording

    rec_dir = recording_env["recordings_dir"] / "test-sentinel-gate"
    t0 = 1000.0

    class FakeCloudRecorderWithUpload:
        def __init__(self, capture_dir_str, **kwargs):
            self.capture_dir = Path(capture_dir_str)
            self.is_recording = False
            self.health_warning = ""
            self.child_crashes = []
            self._chunk_process_q = multiprocessing.Queue()
            self._audio_ack_q = multiprocessing.Queue()
            self._flush_requested = None
            self._flush_ack_counter = None

        def __enter__(self):
            _create_multi_chunk_db(self.capture_dir / "recording.db", t0)
            # Create media + manifest files
            for i in range(2):
                (self.capture_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
                (self.capture_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 512)
                (self.capture_dir / f"chunk_{i:04d}_manifest.json").write_text("{}")

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

        def finalize_pipeline(self):
            return None

    # Build a real PrivacyConfig for the recorder's capture-time enforcement
    from screencap.privacy.policy import PrivacyConfig, PrivacyMode
    _test_privacy_config = PrivacyConfig(mode=PrivacyMode.PUBLIC)

    with (
        mock.patch("screencap.engine.recorder.Recorder", FakeCloudRecorderWithUpload),
        mock.patch("screencap.recorder._check_macos_permissions"),
        mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
        mock.patch("screencap.metrics.save_metrics"),
        # All chunks upload successfully
        mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True),
        # Pipeline init succeeds inside ChunkProcessor (mock the import path)
        mock.patch("screencap.privacy.create_default_pipeline") as mock_pipeline,
        mock.patch("screencap.privacy.Anonymizer"),
        # Provide real PrivacyConfig for capture-time enforcement
        mock.patch("screencap.config.get_privacy_config", return_value=_test_privacy_config),
        mock.patch("screencap.chunk_processor.ChunkProcessor._wait_for_audio", return_value=True),
        mock.patch("screencap.chunk_processor.ChunkProcessor._transcribe", return_value=None),
        mock.patch("screencap.chunk_processor.ChunkProcessor._generate_manifest"),
        mock.patch("screencap.chunk_processor.time.sleep"),
        # Sentinel upload FAILS
        mock.patch("screencap.chunk_processor.upload_sentinel", return_value=False) as mock_sentinel,
        # stub_recording must NOT be called
        mock.patch("screencap.chunk_processor.stub_recording") as mock_stub,
    ):
        mock_pipeline.return_value = mock.MagicMock()
        capture_dir, elapsed, _, _ = start_recording(
            name="test-sentinel-gate",
            audio=False,
            output_dir=rec_dir,
            wifi_metrics=False,
            app_versions=False,
            chunk_duration=30,
            verbose=True,
            live_upload=True,
            cloud_intent=True,
        )

    # Sentinel upload was attempted (all chunks succeeded)
    mock_sentinel.assert_called_once()

    # stub_recording must NOT have been called — sentinel failed
    mock_stub.assert_not_called()

    # Media files must still exist
    for i in range(2):
        assert (capture_dir / f"chunk_{i:04d}.mp4").exists(), \
            f"chunk_{i:04d}.mp4 was deleted — data loss! stub_recording ran without sentinel"
