"""Tests for screencap.chunk_processor — ChunkProcessor pipeline."""

from __future__ import annotations

import json
import multiprocessing
import time
from collections import defaultdict
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def capture_dir(tmp_path):
    """Create a minimal capture directory."""
    db_path = tmp_path / "recording.db"
    # Create minimal SQLite DB with action_event table
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE action_event (id INTEGER PRIMARY KEY, timestamp REAL, type TEXT, data TEXT)"
    )
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording VALUES (1, 1000.0)")
    conn.commit()
    conn.close()
    return tmp_path


def test_run_loop_survives_long_idle_and_responds(capture_dir):
    """Regression: _run must survive 12s of empty queue and still process messages.

    Previously, queue.Empty (normal timeout) was caught by `except Exception`,
    which incremented consecutive_errors. After 5 timeouts (10s), the thread
    exited — killing the processor before any chunk rotation message arrived.
    """
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir,
        q,
        ack_q,
        recording_name="test",
        upload_enabled=False,
        auto_delete=False,
    )
    cp.start()

    # Wait longer than the old 10s death threshold
    time.sleep(12)

    # Thread must still be alive
    assert cp._thread is not None
    assert cp._thread.is_alive(), (
        "ChunkProcessor thread died after 12s of empty queue — "
        "queue.Empty must not be treated as an error"
    )

    # Verify it's still responsive by sending a poison pill
    q.put({"type": "poison_pill"})
    cp._thread.join(timeout=5)
    assert not cp._thread.is_alive(), "Thread should have exited after poison pill"


def test_all_chunks_uploaded_empty_returns_false(capture_dir):
    """all_chunks_uploaded() must return False when no chunks were processed."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
    )
    assert cp.all_chunks_uploaded() is False


def test_was_force_stopped_reflects_stop_event(capture_dir):
    """was_force_stopped is False normally, True after _stop_event is set."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
    )
    assert cp.was_force_stopped is False

    # Simulate what stop() does when the thread times out
    cp._stop_event.set()
    assert cp.was_force_stopped is True


# ---------------------------------------------------------------------------
# Cloud-intent scrubbing tests
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_capture_dir(tmp_path):
    """Capture dir with recording.db including window_event table."""
    import sqlite3

    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE action_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, name TEXT, "
        "mouse_x REAL, mouse_y REAL, mouse_dx REAL, mouse_dy REAL, "
        "mouse_button_name TEXT, mouse_pressed INTEGER, "
        "mouse_pressure REAL, modifier_flags INTEGER, "
        "scroll_phase INTEGER, momentum_phase INTEGER, is_continuous INTEGER, "
        "key_char TEXT, key_name TEXT, key_vk TEXT, "
        "canonical_key_char TEXT, canonical_key_name TEXT, canonical_key_vk TEXT, "
        "text TEXT, element_state TEXT, "
        "active_segment_description TEXT, available_segment_descriptions TEXT)"
    )
    conn.execute(
        "CREATE TABLE window_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, window_id TEXT, "
        "left INTEGER, top INTEGER, width INTEGER, height INTEGER)"
    )
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording VALUES (1, 1000.0)")
    conn.commit()
    conn.close()
    return tmp_path


def _make_mock_pipeline():
    """Create a mock pipeline/anonymizer that detects 'John Smith' as PERSON."""
    from screencap.privacy import Anonymizer, Detection, DetectionResult

    pipeline = MagicMock()

    def mock_detect(text):
        detections = []
        idx = text.find("John Smith")
        while idx != -1:
            detections.append(Detection(
                entity_type="PERSON",
                start=idx,
                end=idx + 10,
                score=0.95,
                source="test",
            ))
            idx = text.find("John Smith", idx + 10)
        return DetectionResult(text, detections)

    pipeline.detect = mock_detect
    anonymizer = Anonymizer()
    return pipeline, anonymizer


@pytest.fixture
def cloud_processor(cloud_capture_dir):
    """ChunkProcessor with mock pipeline for cloud-intent scrubbing tests."""
    from screencap.chunk_processor import ChunkProcessor

    q = multiprocessing.Queue()
    ack_q = multiprocessing.Queue()

    cp = ChunkProcessor(
        cloud_capture_dir, q, ack_q, recording_name="test",
        upload_enabled=False, auto_delete=False,
        cloud_intent=True,
    )

    pipeline, anonymizer = _make_mock_pipeline()
    cp._pipeline = pipeline
    cp._anonymizer = anonymizer
    return cp


class TestCloudIntentGating:
    """Test that cloud-intent recordings gate media uploads."""

    def test_collect_chunk_files_includes_video_audio_for_cloud_intent(self, cloud_capture_dir):
        """Cloud-intent now includes .mp4/.flac (with placeholder frames for blocked intervals)."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        (cloud_capture_dir / "chunk_0000.mp4").write_bytes(b"video data")
        (cloud_capture_dir / "audio_0000.flac").write_bytes(b"audio data")
        (cloud_capture_dir / "events_0000.jsonl").write_text('{"name":"click"}\n')
        (cloud_capture_dir / "chunk_0000_manifest.json").write_text('{}')

        # Cloud-intent: includes video and audio (with placeholder redaction)
        cp_cloud = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=True,
        )
        cloud_names = [f["name"] for f in cp_cloud._collect_chunk_files(0, None)]
        assert "chunk_0000.mp4" in cloud_names
        assert "audio_0000.flac" in cloud_names
        assert "events_0000.jsonl" in cloud_names
        assert "chunk_0000_manifest.json" in cloud_names

        # Non-cloud: everything
        cp_local = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=False,
        )
        local_names = [f["name"] for f in cp_local._collect_chunk_files(0, None)]
        assert "chunk_0000.mp4" in local_names
        assert "audio_0000.flac" in local_names

    def test_checkpoint_and_upload_db_skips_for_cloud_intent(self, cloud_capture_dir):
        """checkpoint_and_upload_db must skip upload for cloud-intent."""
        from screencap.chunk_processor import checkpoint_and_upload_db

        result = checkpoint_and_upload_db(
            cloud_capture_dir, "test", cloud_intent=True,
        )
        assert result is True  # returns True (success) without uploading

    def test_pipeline_init_failure_disables_uploads(self, cloud_capture_dir):
        """If privacy deps fail to import, uploads must be disabled (fail-closed)."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        with patch(
            "screencap.privacy.create_default_pipeline",
            side_effect=ImportError("test: no privacy deps"),
        ):
            cp = ChunkProcessor(
                cloud_capture_dir, q, ack_q, recording_name="test",
                upload_enabled=True, auto_delete=False,
                cloud_intent=True,
            )
            assert cp._upload_enabled is False
            assert cp._pipeline is None

    def test_non_cloud_skips_pipeline_init(self, cloud_capture_dir):
        """Non-cloud recordings must not initialize the scrubbing pipeline."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=True, auto_delete=False,
            cloud_intent=False,
        )
        assert cp._pipeline is None
        assert cp._anonymizer is None


class TestInlineScrubbing:
    """Test inline scrubbing of text surfaces."""

    def test_scrub_transcripts_txt_and_json(self, cloud_capture_dir, cloud_processor):
        """Both transcript formats must have PII replaced, including segment text."""
        txt_path = cloud_capture_dir / "transcript_0000.txt"
        txt_path.write_text("Meeting with John Smith about the project")

        json_path = cloud_capture_dir / "transcript_0000.json"
        json_path.write_text(json.dumps({
            "text": "Call with John Smith",
            "segments": [
                {"start": 0, "end": 5, "text": "Call with John Smith"},
            ],
        }))

        cp = cloud_processor
        cp._scrub_transcript_txt(txt_path)
        cp._scrub_transcript_json(json_path)

        # .txt
        txt_result = txt_path.read_text()
        assert "John Smith" not in txt_result
        assert "<PERSON>" in txt_result

        # .json top-level and segment
        json_data = json.loads(json_path.read_text())
        assert "John Smith" not in json_data["text"]
        assert "<PERSON>" in json_data["text"]
        assert "John Smith" not in json_data["segments"][0]["text"]

    def test_scrub_manifest_dominant_title(self, cloud_capture_dir, cloud_processor):
        """Manifest dominant_title must be scrubbed and derived_name re-derived."""
        manifest_path = cloud_capture_dir / "chunk_0000_manifest.json"
        manifest_path.write_text(json.dumps({
            "chunk_index": 0,
            "tasks": [{
                "start_ts": 1000.0,
                "end_ts": 1060.0,
                "dominant_app": "com.google.Chrome",
                "dominant_title": "John Smith - Contract Review - Google Chrome",
                "derived_name": "chrome_john-smith-contract-review",
            }],
            "summary": {"primary_task": "chrome_john-smith-contract-review"},
        }))

        cp = cloud_processor
        cp._scrub_manifest(manifest_path)

        data = json.loads(manifest_path.read_text())
        task = data["tasks"][0]
        assert "John Smith" not in task["dominant_title"]
        assert "<PERSON>" in task["dominant_title"]
        assert "john-smith" not in task["derived_name"]

    def test_scrub_v2_manifest_skips(self, cloud_capture_dir, cloud_processor):
        """v2 manifests have no text fields — scrub should be a no-op."""
        manifest_path = cloud_capture_dir / "chunk_0000_manifest.json"
        original = {
            "format_version": 2,
            "chunk_index": 0,
            "chunk_start": 1000.0,
            "chunk_end": 2000.0,
            "stats": {"total_events": 42, "total_window_switches": 3},
            "blocked_intervals": [],
        }
        manifest_path.write_text(json.dumps(original))

        cloud_processor._scrub_manifest(manifest_path)

        # File should be unchanged
        data = json.loads(manifest_path.read_text())
        assert data == original

    def test_scrub_text_field_returns_sentinel_on_all_detectors_failed(
        self, cloud_capture_dir, cloud_processor,
    ):
        """_scrub_text_field must return '<SCRUB_FAILED>' when all detectors fail."""
        from screencap.privacy import AllDetectorsFailedError

        cloud_processor._pipeline.detect = MagicMock(
            side_effect=AllDetectorsFailedError("all failed"),
        )

        result = cloud_processor._scrub_text_field("some sensitive text")
        assert result == "<SCRUB_FAILED>"

    def test_scrub_chunk_files_renames_on_per_file_failure(
        self, cloud_capture_dir, cloud_processor,
    ):
        """_scrub_chunk_files must rename a file to .scrub_failed when its scrub raises."""
        # Write a valid events file but make the scrub method raise
        events_path = cloud_capture_dir / "events_0000.jsonl"
        events_path.write_text('{"name":"click"}\n')

        with patch.object(
            cloud_processor, "_scrub_events_jsonl",
            side_effect=RuntimeError("simulated scrub failure"),
        ):
            cloud_processor._scrub_chunk_files(0, None)

        # Original file should be renamed, not uploaded
        assert not events_path.exists()
        assert (cloud_capture_dir / "events_0000.jsonl.scrub_failed").exists()

    def test_scrub_v2_key_type_anonymizes_pii(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.type text with PII → anonymized, children key_char nulled."""
        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.type",
                "timestamp": 1000.0,
                "text": "John Smith",
                "children": [
                    {"type": "key.down", "timestamp": 1000.0, "key_char": "J"},
                    {"type": "key.up", "timestamp": 1000.01, "key_char": "J"},
                    {"type": "key.down", "timestamp": 1000.1, "key_char": "o"},
                    {"type": "key.up", "timestamp": 1000.11, "key_char": "o"},
                ],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cloud_processor._scrub_events_jsonl(events_path)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        key_type = scrubbed[1]
        # Text is anonymized (not null) — preserves context for downstream LLM
        assert "John Smith" not in str(key_type["text"])
        assert key_type["text"] is not None
        for child in key_type["children"]:
            assert child["key_char"] is None

    def test_scrub_v2_key_type_leaves_clean(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.type text without PII should be left unchanged."""
        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.type",
                "timestamp": 1000.0,
                "text": "hello world",
                "children": [],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cloud_processor._scrub_events_jsonl(events_path)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        assert scrubbed[1]["text"] == "hello world"

    def test_scrub_v2_key_shortcut_anonymizes_pii(self, cloud_capture_dir, cloud_processor):
        """v2 format: key.shortcut with PII → text anonymized via recursive scrub."""
        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "key.shortcut",
                "timestamp": 1000.0,
                "text": "John Smith",
                "children": [
                    {"type": "key.down", "timestamp": 1000.0, "key_char": "J"},
                    {"type": "key.down", "timestamp": 1000.1, "key_char": "o"},
                ],
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cloud_processor._scrub_events_jsonl(events_path)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        shortcut = scrubbed[1]
        # Text is anonymized by _scrub_json_recursive (not targeted key.type handler)
        assert "John Smith" not in str(shortcut["text"])

    def test_scrub_v2_window_switch_title(self, cloud_capture_dir, cloud_processor):
        """v2 format: window.switch window_title with PII should be scrubbed."""
        events = [
            {"_meta": True, "format_version": 2},
            {
                "type": "window.switch",
                "timestamp": 1000.0,
                "app_name": "Chrome",
                "app_bundle_id": "com.google.Chrome",
                "window_title": "John Smith - Contract Review",
                "window_id": "1",
                "x": 0, "y": 0, "width": 800, "height": 600,
            },
        ]

        events_path = cloud_capture_dir / "events_0000.jsonl"
        with open(events_path, "w") as f:
            for evt in events:
                f.write(json.dumps(evt) + "\n")

        cloud_processor._scrub_events_jsonl(events_path)

        scrubbed = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        ws = scrubbed[1]
        assert "John Smith" not in ws["window_title"]
        assert "<PERSON>" in ws["window_title"]

class TestBlockedIntervalsInManifest:
    """Tests for Phase 6: blocked intervals in chunk manifest."""

    def test_generate_manifest_passes_blocked_intervals(self, cloud_capture_dir):
        """_process_chunk passes screen_filter intervals to _generate_manifest."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()

        mock_filter = MagicMock()
        mock_filter.get_blocked_intervals.return_value = [
            {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
        ]

        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False, cloud_intent=False,
            screen_filter=mock_filter,
        )

        # Patch _generate_manifest to capture what _process_chunk passes
        captured_kwargs = {}
        original_generate = cp._generate_manifest

        def spy_generate(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return original_generate(*args, **kwargs)

        with patch.object(cp, "_generate_manifest", side_effect=spy_generate), \
             patch.object(cp, "_wait_for_audio"), \
             patch.object(cp, "_transcribe", return_value=None), \
             patch.object(cp, "_trigger_flush"):
            cp._process_chunk({
                "completed_index": 0,
                "chunk_start_time": 1000.0,
                "rotation_time": 1060.0,
            })

        mock_filter.get_blocked_intervals.assert_called_once_with(1000.0, 1060.0)
        assert captured_kwargs["blocked_intervals"] == [
            {"start_ts": 1000.5, "end_ts": 1010.0, "reason": "app_policy"},
        ]


class TestPlaceholderFrame:
    """Tests for Phase 1: placeholder frame generation."""

    def test_make_placeholder_frame_dimensions(self):
        """Placeholder frame has correct dimensions and color."""
        from sc_engine.recorder import _make_placeholder_frame

        frame = _make_placeholder_frame(1920, 1080)
        assert frame.size == (1920, 1080)
        assert frame.mode == "RGB"

        # Check that top-left corner is near-black (30, 30, 30)
        pixel = frame.getpixel((0, 0))
        assert pixel == (30, 30, 30)



class TestUnifiedEventExport:
    """Tests for the unified _export_events pipeline (Phase 3)."""

    def _insert_action(self, conn, ts, name="click", **kwargs):
        """Insert an action_event row."""
        cols = {"timestamp": ts, "name": name}
        cols.update(kwargs)
        keys = ", ".join(cols.keys())
        placeholders = ", ".join("?" * len(cols))
        conn.execute(f"INSERT INTO action_event ({keys}) VALUES ({placeholders})", list(cols.values()))

    def _insert_window(self, conn, ts, title="Finder", bundle_id="com.apple.finder", window_id="1"):
        """Insert a window_event row."""
        conn.execute(
            "INSERT INTO window_event (timestamp, title, app_bundle_id, window_id, "
            "\"left\", top, width, height) VALUES (?, ?, ?, ?, 0, 0, 800, 600)",
            (ts, title, bundle_id, window_id),
        )

    def test_produces_processed_events(self, cloud_capture_dir):
        """_export_events should produce processed Pydantic events, not raw DB rows."""
        import sqlite3

        conn = sqlite3.connect(str(cloud_capture_dir / "recording.db"))
        # Insert a click down + up → should be merged into mouse.singleclick
        self._insert_action(conn, 1000.0, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(conn, 1000.1, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)
        conn.commit()
        conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 999.0, 1001.0)

        jsonl_path = cloud_capture_dir / "events_0000.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")

        # First line is _meta header
        header = json.loads(lines[0])
        assert header["_meta"] is True
        assert header["format_version"] == 2

        # Should have merged into singleclick
        events = [json.loads(line) for line in lines[1:]]
        types = [e["type"] for e in events]
        assert "mouse.singleclick" in types
        # Should NOT have raw "click" entries
        assert not any(e.get("name") == "click" for e in events)

    def test_includes_window_switch_events(self, cloud_capture_dir):
        """_export_events should include deduplicated window.switch events."""
        import sqlite3

        conn = sqlite3.connect(str(cloud_capture_dir / "recording.db"))
        self._insert_window(conn, 1000.0, "Documents", "com.apple.finder", "1")
        self._insert_window(conn, 1000.5, "Downloads", "com.apple.finder", "1")  # same window, title change
        self._insert_window(conn, 1001.0, "Google", "com.google.Chrome", "2")  # different window
        self._insert_action(conn, 1000.2, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(conn, 1000.3, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)
        conn.commit()
        conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 999.0, 1002.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]  # skip _meta
        ws_events = [e for e in events if e["type"] == "window.switch"]

        # Deduped: Finder (1 window) + Chrome = 2 switches
        assert len(ws_events) == 2
        assert ws_events[0]["app_bundle_id"] == "com.apple.finder"
        assert ws_events[1]["app_bundle_id"] == "com.google.Chrome"

    def test_initial_window_context(self, cloud_capture_dir):
        """First window.switch should be from before chunk start (initial context)."""
        import sqlite3

        conn = sqlite3.connect(str(cloud_capture_dir / "recording.db"))
        # Window event before chunk start
        self._insert_window(conn, 999.0, "Pre-chunk App", "com.example.app", "10")
        self._insert_action(conn, 1000.2, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(conn, 1000.3, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)
        conn.commit()
        conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 1000.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]

        # First event should be window.switch from initial context
        assert events[0]["type"] == "window.switch"
        assert events[0]["app_bundle_id"] == "com.example.app"

    def test_atomic_write(self, cloud_capture_dir):
        """No .tmp file should remain after successful export."""
        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 1000.0, 1001.0)

        assert (cloud_capture_dir / "events_0000.jsonl").exists()
        assert not (cloud_capture_dir / "events_0000.jsonl.tmp").exists()

    def test_excludes_mouse_move_by_default(self, cloud_capture_dir):
        """Mouse.move events should be excluded by default."""
        import sqlite3

        conn = sqlite3.connect(str(cloud_capture_dir / "recording.db"))
        self._insert_action(conn, 1000.0, "move", mouse_x=100, mouse_y=200)
        self._insert_action(conn, 1000.1, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(conn, 1000.2, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)
        conn.commit()
        conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        cp._export_events(0, 999.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]
        assert not any(e["type"] == "mouse.move" for e in events)

    def test_malformed_rows_skipped_gracefully(self, cloud_capture_dir):
        """Rows that fail conversion should be skipped without crashing export."""
        import sqlite3

        conn = sqlite3.connect(str(cloud_capture_dir / "recording.db"))
        # Valid click pair
        self._insert_action(conn, 1000.0, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=1)
        self._insert_action(conn, 1000.1, "click", mouse_x=100, mouse_y=200,
                            mouse_button_name="left", mouse_pressed=0)
        # Malformed: click with mouse_pressed=None → dict_to_action_event returns None
        self._insert_action(conn, 1000.5, "click", mouse_x=50, mouse_y=50,
                            mouse_button_name="left")  # mouse_pressed defaults to NULL
        conn.commit()
        conn.close()

        from screencap.chunk_processor import ChunkProcessor

        q = multiprocessing.Queue()
        ack_q = multiprocessing.Queue()
        cp = ChunkProcessor(
            cloud_capture_dir, q, ack_q, recording_name="test",
            upload_enabled=False, auto_delete=False,
        )

        # Should not raise
        cp._export_events(0, 999.0, 1001.0)

        lines = (cloud_capture_dir / "events_0000.jsonl").read_text().strip().split("\n")
        events = [json.loads(line) for line in lines[1:]]
        # The valid click pair should still be exported
        assert any(e["type"] == "mouse.singleclick" for e in events)


# ---------------------------------------------------------------------------
# Upload gating integration tests — sentinel upload should be gated behind
# all_chunks_uploaded().  These tests exercise the real ChunkProcessor thread
# loop with real queues and the real _upload_chunk retry logic; only the HTTP
# layer (upload_chunk_files) is mocked.
# ---------------------------------------------------------------------------


def _create_seven_chunk_db(db_path: Path, t0: float) -> None:
    """Create a DB with events spanning 7 chunks (30s each)."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE action_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, name TEXT, "
        "mouse_x REAL, mouse_y REAL, mouse_dx REAL, mouse_dy REAL, "
        "mouse_button_name TEXT, mouse_pressed INTEGER, "
        "mouse_pressure REAL, modifier_flags INTEGER, "
        "scroll_phase INTEGER, momentum_phase INTEGER, is_continuous INTEGER, "
        "key_char TEXT, key_name TEXT, key_vk TEXT, "
        "canonical_key_char TEXT, canonical_key_name TEXT, canonical_key_vk TEXT, "
        "text TEXT, element_state TEXT, "
        "active_segment_description TEXT, available_segment_descriptions TEXT)"
    )
    conn.execute(
        "CREATE TABLE window_event ("
        "id INTEGER PRIMARY KEY, timestamp REAL, title TEXT, "
        "app_bundle_id TEXT, window_id TEXT, "
        "left INTEGER, top INTEGER, width INTEGER, height INTEGER)"
    )
    conn.execute(
        "CREATE TABLE recording (id INTEGER PRIMARY KEY, timestamp REAL)"
    )
    conn.execute("INSERT INTO recording VALUES (1, ?)", (t0,))
    # Insert one click pair per chunk so _export_events has something to process
    for i in range(7):
        chunk_ts = t0 + i * 30 + 5
        conn.execute(
            "INSERT INTO action_event (timestamp, name, mouse_x, mouse_y, "
            "mouse_button_name, mouse_pressed) VALUES (?, 'click', 100, 200, 'left', 1)",
            (chunk_ts,),
        )
        conn.execute(
            "INSERT INTO action_event (timestamp, name, mouse_x, mouse_y, "
            "mouse_button_name, mouse_pressed) VALUES (?, 'click', 100, 200, 'left', 0)",
            (chunk_ts + 0.1,),
        )
    conn.commit()
    conn.close()


def _create_chunk_media_files(capture_dir: Path, n_chunks: int) -> None:
    """Create stub .mp4, .flac files for each chunk."""
    for i in range(n_chunks):
        (capture_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 1024)
        (capture_dir / f"audio_{i:04d}.flac").write_bytes(b"\x00" * 512)


def _build_chunk_processor(
    capture_dir: Path,
) -> tuple:
    """Build a ChunkProcessor with upload enabled but side-effects mocked.

    Returns (processor, chunk_queue).  The real _upload_chunk retry logic is
    preserved — only upload_chunk_files (the HTTP layer) needs to be patched
    by the caller via @mock.patch.
    """
    from screencap.chunk_processor import ChunkProcessor

    chunk_q = multiprocessing.Queue()
    audio_q = multiprocessing.Queue()
    cp = ChunkProcessor(
        capture_dir,
        chunk_q,
        audio_q,
        recording_name="test-upload-gating",
        upload_enabled=True,
        auto_delete=True,
        cloud_intent=False,  # skip privacy pipeline init
    )

    # Mock side effects that aren't under test
    cp._wait_for_audio = lambda *a, **kw: True
    cp._transcribe = lambda *a, **kw: None
    cp._generate_manifest = lambda *a, **kw: None

    return cp, chunk_q


def _enqueue_chunks(q, t0: float, n: int) -> None:
    """Put n chunk rotation messages into the queue."""
    for i in range(n):
        q.put({
            "type": "chunk_rotated" if i < n - 1 else "final_chunk",
            "completed_index": i,
            "chunk_start_time": t0 + i * 30,
            "rotation_time": t0 + (i + 1) * 30,
        })


class TestUploadGatingAllSucceed:
    """Scenario 1: All 7 chunks upload successfully."""

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files", return_value=True)
    def test_seven_chunks_all_succeed(self, mock_upload, mock_sleep, tmp_path):
        """Happy path: all chunks upload, cleanup runs, state is correct."""
        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Core state
        assert cp.all_chunks_uploaded() is True
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 7
        assert n_total == 7

        # Upload called once per chunk (no retries needed)
        assert mock_upload.call_count == 7

        # Old chunk media cleaned up (keep_recent=2: only chunks 5,6 remain)
        for i in range(5):
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up"
            assert not (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac should have been cleaned up"
        for i in range(5, 7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should still exist (recent)"
            assert (tmp_path / f"audio_{i:04d}.flac").exists(), \
                f"audio_{i:04d}.flac should still exist (recent)"

        # No retry backoff needed
        mock_sleep.assert_not_called()


class TestUploadGatingPartialFailure:
    """Scenario 2: Chunk 3 fails permanently, rest succeed."""

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files")
    def test_seven_chunks_one_fails_permanently(self, mock_upload, mock_sleep, tmp_path):
        """Chunk 3 fails both attempts. Retry called, files preserved for recovery."""

        def upload_side_effect(recording_name, files, capture_dir):
            for f in files:
                if "chunk_0003" in f["name"]:
                    raise ConnectionError("upload failed")
            return True

        mock_upload.side_effect = upload_side_effect

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Core state
        assert cp.all_chunks_uploaded() is False
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 6
        assert n_total == 7

        # Retry: 6 successes (once each) + 2 attempts for chunk 3 = 8
        assert mock_upload.call_count == 8
        # 5s backoff sleep between chunk 3's two attempts
        mock_sleep.assert_called_once_with(5)

        # Chunk 3 media files preserved on disk (not cleaned up)
        assert (tmp_path / "chunk_0003.mp4").exists()
        assert (tmp_path / "audio_0003.flac").exists()

        # Successfully uploaded old chunks still cleaned up
        for i in [0, 1, 2]:
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up (uploaded OK)"


class TestUploadGatingRetryRecovery:
    """Scenario 3: Chunk 3 fails first attempt, succeeds on retry."""

    @mock.patch("screencap.chunk_processor.time.sleep")
    @mock.patch("screencap.chunk_processor.upload_chunk_files")
    def test_seven_chunks_one_recovers_on_retry(self, mock_upload, mock_sleep, tmp_path):
        """Chunk 3 fails first attempt, succeeds on retry. End state = happy path."""

        call_counts: dict[int, int] = defaultdict(int)

        def upload_side_effect(recording_name, files, capture_dir):
            chunk_id = None
            for f in files:
                if "chunk_0003" in f["name"]:
                    chunk_id = 3
                    break
            if chunk_id == 3:
                call_counts[3] += 1
                if call_counts[3] == 1:
                    raise ConnectionError("transient failure")
            return True

        mock_upload.side_effect = upload_side_effect

        t0 = time.time()
        _create_seven_chunk_db(tmp_path / "recording.db", t0)
        _create_chunk_media_files(tmp_path, 7)
        cp, chunk_q = _build_chunk_processor(tmp_path)
        cp.start()

        _enqueue_chunks(chunk_q, t0, 7)
        cp.stop(timeout=30)

        # Retry succeeded: all chunks marked as uploaded
        assert cp.all_chunks_uploaded() is True
        n_uploaded, n_total = cp.upload_summary()
        assert n_uploaded == 7
        assert n_total == 7

        # Upload called 8 times: 6 once each + chunk 3 twice
        assert mock_upload.call_count == 8
        # Backoff sleep called once (before chunk 3's retry)
        mock_sleep.assert_called_once_with(5)

        # Chunk 3 treated as success: old chunks cleaned up normally
        for i in range(5):
            assert not (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should have been cleaned up"
        for i in range(5, 7):
            assert (tmp_path / f"chunk_{i:04d}.mp4").exists(), \
                f"chunk_{i:04d}.mp4 should still exist (recent)"
