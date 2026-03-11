"""Tests for LLM-based task segmentation in Cloud Run processor."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Stub out Cloud Run dependencies that aren't installed in the dev venv
_ff = types.ModuleType("functions_framework")
_ff.cloud_event = lambda fn: fn  # no-op decorator
sys.modules.setdefault("functions_framework", _ff)

_gcs = types.ModuleType("google.cloud.storage")
_gcs.Client = MagicMock
_gcs.Bucket = MagicMock
sys.modules.setdefault("google.cloud.storage", _gcs)
sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
sys.modules.setdefault("google", types.ModuleType("google"))

# Ensure the stubs exist in the package hierarchy
_google = sys.modules["google"]
_google.cloud = sys.modules["google.cloud"]
sys.modules["google.cloud"].storage = _gcs

# Add the process-recording script dir to sys.path so we can import main
_SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts" / "process-recording"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_manifests():
    """Two v2 manifests spanning 0–3600s and 3600–7200s."""
    return [
        {
            "format_version": 2,
            "chunk_index": 0,
            "chunk_start": 1000.0,
            "chunk_end": 4600.0,
            "stats": {"total_events": 200, "total_window_switches": 5},
            "blocked_intervals": [],
        },
        {
            "format_version": 2,
            "chunk_index": 1,
            "chunk_start": 4600.0,
            "chunk_end": 8200.0,
            "stats": {"total_events": 150, "total_window_switches": 3},
            "blocked_intervals": [],
        },
    ]


@pytest.fixture
def events_chunk_0():
    """Simulated events JSONL for chunk 0."""
    events = [
        {"_meta": True, "format_version": 2},
        {"type": "window.switch", "timestamp": 1100.0,
         "app_bundle_id": "com.microsoft.VSCode", "window_title": "main.py — screencap"},
        {"type": "key.type", "timestamp": 1110.0, "text": "def foo():"},
        {"type": "key.shortcut", "timestamp": 1120.0, "text": "Cmd+S"},
        {"type": "mouse.singleclick", "timestamp": 1130.0},
        {"type": "mouse.scroll", "timestamp": 1140.0},
        {"type": "window.switch", "timestamp": 2000.0,
         "app_bundle_id": "com.google.Chrome", "window_title": "Gmail",
         "domain": "mail.google.com"},
        {"type": "key.type", "timestamp": 2010.0, "text": "Hey team"},
        {"type": "window.switch", "timestamp": 3000.0,
         "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py — screencap"},
        {"type": "key.type", "timestamp": 3010.0, "text": "class Auth:"},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture
def events_chunk_1():
    """Simulated events JSONL for chunk 1 — continues VSCode work."""
    events = [
        {"_meta": True, "format_version": 2},
        {"type": "window.switch", "timestamp": 4700.0,
         "app_bundle_id": "com.microsoft.VSCode", "window_title": "auth.py — screencap"},
        {"type": "key.type", "timestamp": 4710.0, "text": "def token():"},
        {"type": "mouse.singleclick", "timestamp": 4720.0},
        {"type": "window.switch", "timestamp": 6000.0,
         "app_bundle_id": "com.tinyspeck.slackmacgap", "window_title": "#dev — Slack"},
        {"type": "key.type", "timestamp": 6010.0, "text": "PR is ready"},
    ]
    return "\n".join(json.dumps(e) for e in events) + "\n"


@pytest.fixture
def transcript_chunk_0():
    return json.dumps({
        "segments": [
            {"start": 50.0, "end": 55.0, "text": "Let me fix this bug first"},
            {"start": 950.0, "end": 955.0, "text": "Now let me check email"},
        ]
    })


def _mock_blob_bytes(events_0, events_1, transcript_0=None):
    """Return a side_effect function for _blob_bytes that serves test data."""
    data_map = {
        "recordings/test-rec/events_0000.jsonl": events_0.encode(),
        "recordings/test-rec/events_0001.jsonl": events_1.encode(),
    }
    if transcript_0:
        data_map["recordings/test-rec/transcript_0000.json"] = transcript_0.encode()

    def _side_effect(blob_name):
        return data_map.get(blob_name)

    return _side_effect


# ---------------------------------------------------------------------------
# Tests: _classify_app
# ---------------------------------------------------------------------------

class TestClassifyApp:
    def test_known_bundle(self):
        from main import _classify_app
        assert _classify_app("com.microsoft.VSCode") == "CODE"
        assert _classify_app("com.google.Chrome") == "BROWSER"
        assert _classify_app("com.tinyspeck.slackmacgap") == "CHAT"
        assert _classify_app("com.apple.mail") == "EMAIL"

    def test_known_domain(self):
        from main import _classify_app
        assert _classify_app("com.google.Chrome", domain="github.com") == "BROWSER"
        assert _classify_app("unknown.bundle", domain="github.com") == "CODE"
        assert _classify_app("unknown.bundle", domain="mail.google.com") == "EMAIL"

    def test_unknown_returns_other(self):
        from main import _classify_app
        assert _classify_app("com.unknown.app") == "OTHER"


# ---------------------------------------------------------------------------
# Tests: _format_relative_time / _parse_relative_time
# ---------------------------------------------------------------------------

class TestRelativeTime:
    def test_format(self):
        from main import _format_relative_time
        assert _format_relative_time(0) == "0:00:00"
        assert _format_relative_time(65) == "0:01:05"
        assert _format_relative_time(3661) == "1:01:01"

    def test_parse(self):
        from main import _parse_relative_time
        assert _parse_relative_time("0:00:00") == 0
        assert _parse_relative_time("0:01:05") == 65
        assert _parse_relative_time("1:01:01") == 3661

    def test_roundtrip(self):
        from main import _format_relative_time, _parse_relative_time
        for secs in (0, 1, 59, 60, 3599, 3600, 7261):
            assert _parse_relative_time(_format_relative_time(secs)) == secs

    def test_malformed_input_returns_zero(self):
        from main import _parse_relative_time
        assert _parse_relative_time("abc:def:ghi") == 0.0
        assert _parse_relative_time("not_a_number") == 0.0
        assert _parse_relative_time("") == 0.0


# ---------------------------------------------------------------------------
# Tests: _derive_activity_summary
# ---------------------------------------------------------------------------

class TestDeriveActivitySummary:
    @patch("main._blob_bytes")
    def test_basic_summary(self, mock_bytes, sample_manifests,
                           events_chunk_0, events_chunk_1):
        from main import _derive_activity_summary

        mock_bytes.side_effect = _mock_blob_bytes(events_chunk_0, events_chunk_1)
        result = _derive_activity_summary("test-rec", sample_manifests)

        assert result is not None
        assert "summary" in result
        assert "entries" in result
        assert "time_map" in result

        timeline = result["summary"]["timeline"]
        assert len(timeline) >= 3  # VSCode, Chrome, VSCode (merged), Slack

        # First entry should be VSCode
        assert timeline[0]["app"] == "VSCode"
        assert timeline[0]["cat"] == "CODE"

    @patch("main._blob_bytes")
    def test_cross_chunk_merge(self, mock_bytes, sample_manifests,
                               events_chunk_0, events_chunk_1):
        """Same app+title at chunk boundary should be merged."""
        from main import _derive_activity_summary

        mock_bytes.side_effect = _mock_blob_bytes(events_chunk_0, events_chunk_1)
        result = _derive_activity_summary("test-rec", sample_manifests)

        # The last VSCode entry from chunk 0 (auth.py) and first from chunk 1
        # (also auth.py) should merge into one entry
        entries = result["entries"]
        auth_entries = [e for e in entries if "auth.py" in e.get("title", "")]
        assert len(auth_entries) == 1  # merged into one

    @patch("main._blob_bytes")
    def test_transcript_included(self, mock_bytes, sample_manifests,
                                 events_chunk_0, events_chunk_1, transcript_chunk_0):
        from main import _derive_activity_summary

        mock_bytes.side_effect = _mock_blob_bytes(
            events_chunk_0, events_chunk_1, transcript_chunk_0,
        )
        result = _derive_activity_summary("test-rec", sample_manifests)

        assert "transcript" in result["summary"]
        assert len(result["summary"]["transcript"]) == 2
        assert "fix this bug" in result["summary"]["transcript"][0]["text"]

    @patch("main._blob_bytes")
    def test_typed_text_capped(self, mock_bytes, sample_manifests):
        """typed list should be capped at 5 entries per activity."""
        from main import _derive_activity_summary

        # Create events with 10 key.type events for one window
        events = [{"_meta": True}]
        events.append({
            "type": "window.switch", "timestamp": 1100.0,
            "app_bundle_id": "com.microsoft.VSCode", "window_title": "test.py",
        })
        for i in range(10):
            events.append({"type": "key.type", "timestamp": 1110.0 + i, "text": f"line {i}"})
        data = "\n".join(json.dumps(e) for e in events) + "\n"

        mock_bytes.side_effect = _mock_blob_bytes(data, "")
        result = _derive_activity_summary("test-rec", sample_manifests)

        assert result is not None
        vscode_entry = result["entries"][0]
        assert len(vscode_entry["typed"]) == 5

    @patch("main._blob_bytes")
    def test_no_events_returns_none(self, mock_bytes, sample_manifests):
        from main import _derive_activity_summary

        mock_bytes.return_value = None
        result = _derive_activity_summary("test-rec", sample_manifests)
        assert result is None


# ---------------------------------------------------------------------------
# Tests: _validate_llm_tasks
# ---------------------------------------------------------------------------

class TestValidateLlmTasks:
    def _make_result(self, tasks, summary=None):
        return {
            "tasks": tasks,
            "summary": summary or {
                "overview": "Test session.",
                "primary_focus": "development",
                "time_breakdown": {"development": 100},
                "key_accomplishments": ["Did stuff"],
            },
        }

    def test_valid_tasks(self):
        from main import _validate_llm_tasks

        result = self._make_result([
            {
                "start_time": "0:00:00", "end_time": "0:10:00",
                "name": "Coding in VSCode", "description": "Wrote code.",
                "category": "development", "apps_used": ["VSCode"],
                "confidence": "high",
            },
            {
                "start_time": "0:10:00", "end_time": "0:20:00",
                "name": "Checking email", "description": "Read emails.",
                "category": "communication", "apps_used": ["Chrome"],
                "confidence": "medium",
            },
        ])

        time_map = {"0:00:00": 1000.0, "0:10:00": 1600.0, "0:20:00": 2200.0}
        validated = _validate_llm_tasks(result, 1000.0, 2200.0, time_map)

        assert validated is not None
        assert len(validated["tasks"]) == 2
        assert validated["tasks"][0]["start_ts"] == 1000.0
        assert validated["tasks"][0]["end_ts"] == 1600.0
        assert validated["tasks"][0]["name"] == "Coding in VSCode"
        assert validated["tasks"][0]["category"] == "development"
        assert validated["tasks"][0]["derived_name"] == "coding-in-vscode"

    def test_gap_allowed(self):
        """Small gaps between tasks should pass validation."""
        from main import _validate_llm_tasks

        result = self._make_result([
            {
                "start_time": "0:00:00", "end_time": "0:10:00",
                "name": "Task A", "description": "A",
                "category": "development", "apps_used": [], "confidence": "high",
            },
            {
                "start_time": "0:10:05", "end_time": "0:20:00",
                "name": "Task B", "description": "B",
                "category": "other", "apps_used": [], "confidence": "high",
            },
        ])

        time_map = {}
        validated = _validate_llm_tasks(result, 1000.0, 2200.0, time_map)
        assert validated is not None

    def test_overlap_rejected(self):
        from main import _validate_llm_tasks

        result = self._make_result([
            {
                "start_time": "0:00:00", "end_time": "0:12:00",
                "name": "Task A", "description": "A",
                "category": "development", "apps_used": [], "confidence": "high",
            },
            {
                "start_time": "0:10:00", "end_time": "0:20:00",
                "name": "Task B", "description": "B",
                "category": "other", "apps_used": [], "confidence": "high",
            },
        ])

        time_map = {}
        validated = _validate_llm_tasks(result, 1000.0, 2200.0, time_map)
        assert validated is None

    def test_missing_field_rejected(self):
        from main import _validate_llm_tasks

        result = self._make_result([
            {
                "start_time": "0:00:00", "end_time": "0:10:00",
                # Missing "name" field
                "description": "A",
                "category": "development", "apps_used": [], "confidence": "high",
            },
        ])

        validated = _validate_llm_tasks(result, 1000.0, 1600.0, {})
        assert validated is None

    def test_empty_tasks_rejected(self):
        from main import _validate_llm_tasks

        result = self._make_result([])
        validated = _validate_llm_tasks(result, 1000.0, 2000.0, {})
        assert validated is None

    def test_invalid_category_normalized(self):
        from main import _validate_llm_tasks

        result = self._make_result([
            {
                "start_time": "0:00:00", "end_time": "0:10:00",
                "name": "Task", "description": "D",
                "category": "INVALID_CATEGORY",
                "apps_used": [], "confidence": "high",
            },
        ])

        validated = _validate_llm_tasks(result, 1000.0, 1600.0, {})
        assert validated is not None
        assert validated["tasks"][0]["category"] == "other"


# ---------------------------------------------------------------------------
# Tests: _compute_source_chunks / _map_tasks_to_chunks
# ---------------------------------------------------------------------------

class TestMapTasksToChunks:
    def test_single_chunk_task(self, sample_manifests):
        from main import _compute_source_chunks

        task = {"start_ts": 1200.0, "end_ts": 2000.0}
        chunks = _compute_source_chunks(task, sample_manifests)

        assert len(chunks) == 1
        assert chunks[0]["chunk_index"] == 0
        assert chunks[0]["start_ts"] == 1200.0
        assert chunks[0]["end_ts"] == 2000.0

    def test_cross_chunk_task(self, sample_manifests):
        from main import _compute_source_chunks

        task = {"start_ts": 4000.0, "end_ts": 5000.0}
        chunks = _compute_source_chunks(task, sample_manifests)

        assert len(chunks) == 2
        assert chunks[0]["chunk_index"] == 0
        assert chunks[0]["start_ts"] == 4000.0
        assert chunks[0]["end_ts"] == 4600.0
        assert chunks[1]["chunk_index"] == 1
        assert chunks[1]["start_ts"] == 4600.0
        assert chunks[1]["end_ts"] == 5000.0

    def test_map_fills_source_chunks(self, sample_manifests):
        from main import _map_tasks_to_chunks

        tasks = [
            {"start_ts": 1200.0, "end_ts": 2000.0},
            {"start_ts": 4000.0, "end_ts": 5000.0},
        ]
        result = _map_tasks_to_chunks(tasks, sample_manifests)

        assert len(result[0]["source_chunks"]) == 1
        assert len(result[1]["source_chunks"]) == 2


# ---------------------------------------------------------------------------
# Tests: _simple_segment_from_events
# ---------------------------------------------------------------------------

class TestSimpleSegment:
    @patch("main._blob_bytes")
    def test_basic_segmentation(self, mock_bytes, sample_manifests,
                                events_chunk_0, events_chunk_1):
        from main import _simple_segment_from_events

        mock_bytes.side_effect = _mock_blob_bytes(events_chunk_0, events_chunk_1)
        tasks = _simple_segment_from_events("test-rec", sample_manifests)

        # All events are within rest_threshold (120s) of each other within
        # each cluster, but chunks have a gap at 3010→4700 = 1690s
        assert len(tasks) >= 1
        for task in tasks:
            assert "source_chunks" in task
            assert len(task["source_chunks"]) >= 1
            assert task["derived_name"]

    @patch("main._blob_bytes")
    def test_no_events_returns_single_task(self, mock_bytes, sample_manifests):
        from main import _simple_segment_from_events

        mock_bytes.return_value = None
        tasks = _simple_segment_from_events("test-rec", sample_manifests)

        assert len(tasks) == 1
        assert tasks[0]["start_ts"] == 1000.0
        assert tasks[0]["end_ts"] == 8200.0


# ---------------------------------------------------------------------------
# Tests: _stats_summary
# ---------------------------------------------------------------------------

class TestStatsSummary:
    def test_basic_summary(self):
        from main import _stats_summary

        entries = [
            {"start_ts": 0, "end_ts": 600, "cat": "CODE", "app": "VSCode"},
            {"start_ts": 600, "end_ts": 900, "cat": "EMAIL", "app": "Chrome"},
        ]
        tasks = [{"start_ts": 0, "end_ts": 900}]

        summary = _stats_summary(entries, tasks)

        assert "overview" in summary
        assert "1 tasks" in summary["overview"]
        assert "2 apps" in summary["overview"]
        assert summary["primary_focus"] == "code"
        assert "CODE" in summary["time_breakdown"]
        assert summary["key_accomplishments"] == []

    def test_empty_entries(self):
        from main import _stats_summary

        summary = _stats_summary([], [])
        assert summary["overview"] == "Recording with 0 tasks across 0 apps."
        assert summary["key_accomplishments"] == []


# ---------------------------------------------------------------------------
# Tests: _process_v2_manifests (integration, LLM mocked)
# ---------------------------------------------------------------------------

class TestProcessV2Manifests:
    @patch("main._call_llm")
    @patch("main._blob_bytes")
    def test_llm_failure_gracefully_falls_back(self, mock_bytes, mock_llm,
                                                sample_manifests,
                                                events_chunk_0, events_chunk_1):
        """v2 manifests always attempt LLM; when it fails, idle-gap fallback kicks in."""
        from main import _process_v2_manifests

        mock_bytes.side_effect = _mock_blob_bytes(events_chunk_0, events_chunk_1)
        mock_llm.return_value = None  # LLM failed

        tasks, method, summary = _process_v2_manifests("test-rec", sample_manifests)

        assert method == "idle"
        assert len(tasks) >= 1
        assert summary is not None
        assert "overview" in summary

    @patch("main._call_llm")
    @patch("main._blob_bytes")
    def test_llm_success(self, mock_bytes, mock_llm, sample_manifests,
                         events_chunk_0, events_chunk_1):
        from main import _process_v2_manifests

        mock_bytes.side_effect = _mock_blob_bytes(events_chunk_0, events_chunk_1)

        # Mock LLM returning valid tasks
        mock_llm.return_value = {
            "tasks": [
                {
                    "start_time": "0:00:00", "end_time": "0:33:20",
                    "name": "Development in VSCode",
                    "description": "Wrote code and checked email.",
                    "category": "development",
                    "apps_used": ["VSCode", "Chrome"],
                    "confidence": "high",
                },
                {
                    "start_time": "0:33:20", "end_time": "2:00:00",
                    "name": "Team communication",
                    "description": "Continued coding then chatted on Slack.",
                    "category": "communication",
                    "apps_used": ["VSCode", "Slack"],
                    "confidence": "medium",
                },
            ],
            "summary": {
                "overview": "Dev session with email and Slack.",
                "primary_focus": "development",
                "time_breakdown": {"development": 70, "communication": 30},
                "key_accomplishments": ["Wrote auth module"],
            },
        }

        tasks, method, summary = _process_v2_manifests("test-rec", sample_manifests)

        assert method == "llm"
        assert len(tasks) == 2
        assert tasks[0]["name"] == "Development in VSCode"
        assert summary["primary_focus"] == "development"


# ---------------------------------------------------------------------------
# Tests: simplified manifest format
# ---------------------------------------------------------------------------

class TestSimplifiedManifest:
    def test_v2_manifest_format(self, tmp_path):
        """Verify the new task_manifest generates v2 format correctly."""
        from screencap.task_manifest import generate_manifest
        import sqlite3

        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE action_event (
            id INTEGER PRIMARY KEY, timestamp REAL, name TEXT
        )""")
        conn.execute("""CREATE TABLE window_event (
            id INTEGER PRIMARY KEY, timestamp REAL, title TEXT,
            app_bundle_id TEXT, window_id INTEGER
        )""")
        # Insert some events
        conn.execute("INSERT INTO action_event VALUES (1, 1000.5, 'click')")
        conn.execute("INSERT INTO action_event VALUES (2, 1001.0, 'type')")
        conn.execute("INSERT INTO action_event VALUES (3, 1002.0, 'move')")
        conn.execute("INSERT INTO window_event VALUES (1, 1000.0, 'test', 'com.test', 1)")
        conn.commit()
        conn.close()

        path = generate_manifest(tmp_path, 0, 1000.0, 2000.0, segmentation_mode="llm")

        data = json.loads(path.read_text())
        assert data["format_version"] == 2
        assert data["chunk_index"] == 0
        assert data["chunk_start"] == 1000.0
        assert data["chunk_end"] == 2000.0
        assert data["stats"]["total_events"] == 2  # excludes 'move'
        assert data["stats"]["total_window_switches"] == 1
        assert "tasks" not in data

    def test_v1_manifest_has_tasks(self, tmp_path):
        """Verify legacy mode still produces tasks array."""
        from screencap.task_manifest import generate_manifest
        import sqlite3

        db_path = tmp_path / "recording.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""CREATE TABLE action_event (
            id INTEGER PRIMARY KEY, timestamp REAL, name TEXT
        )""")
        conn.execute("""CREATE TABLE window_event (
            id INTEGER PRIMARY KEY, timestamp REAL, title TEXT,
            app_bundle_id TEXT, window_id INTEGER
        )""")
        conn.execute("INSERT INTO action_event VALUES (1, 1000.5, 'click')")
        conn.execute("INSERT INTO window_event VALUES (1, 1000.0, 'test', 'com.test', 1)")
        conn.commit()
        conn.close()

        path = generate_manifest(tmp_path, 0, 1000.0, 2000.0, segmentation_mode="idle")

        data = json.loads(path.read_text())
        assert "format_version" not in data
        assert "tasks" in data
