"""Root conftest — shared fixtures for the screencap test suite.

All fixtures are function-scoped (unique tmp_path per test, no cross-test
contamination). DB fixtures use the real engine create_db + crud API so the
schema always matches production.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Config cache reset (autouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Reset screencap.config._config_cache between tests.

    Resets on BOTH setup and teardown. Tests that intentionally pre-set the
    cache should do so in the test body (after fixture setup), not at module
    level.
    """
    import screencap.config

    screencap.config._config_cache = None
    yield
    screencap.config._config_cache = None


# ---------------------------------------------------------------------------
# Real engine DB fixture
# ---------------------------------------------------------------------------


class RecordingDB:
    """Helper wrapping a real engine SQLite DB for test use.

    Provides the db_path, engine, session, and recording object plus
    convenience methods for inserting common event types.
    """

    def __init__(self, db_path, engine, session, recording):
        self.db_path = db_path
        self.engine = engine
        self.session = session
        self.recording = recording
        self._base_ts = recording.timestamp

    # -- convenience inserters ----------------------------------------------

    def add_click(self, ts_offset, *, x=500.0, y=300.0, button="left"):
        """Insert a click down+up pair at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        crud.insert_action_event(self.session, self.recording, ts, {
            "name": "click",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": True,
        })
        crud.insert_action_event(self.session, self.recording, ts + 0.05, {
            "name": "click",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button,
            "mouse_pressed": False,
        })

    def add_keypress(self, ts_offset, char="h"):
        """Insert a key press+release pair at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        crud.insert_action_event(self.session, self.recording, ts, {
            "name": "press",
            "key_char": char,
            "key_name": char,
            "canonical_key_char": char,
            "canonical_key_name": char,
        })
        crud.insert_action_event(self.session, self.recording, ts + 0.05, {
            "name": "release",
            "key_char": char,
            "key_name": char,
            "canonical_key_char": char,
            "canonical_key_name": char,
        })

    def add_window_event(
        self, ts_offset, *, title="Editor", bundle_id="com.app.editor",
        window_id="win-1", left=0, top=0, width=1920, height=1080,
        browser_url=None,
    ):
        """Insert a window event at base_ts + ts_offset."""
        from screencap.engine.db import crud

        ts = self._base_ts + ts_offset
        data = {
            "title": title,
            "app_bundle_id": bundle_id,
            "window_id": window_id,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }
        if browser_url is not None:
            data["browser_url"] = browser_url
        crud.insert_window_event(self.session, self.recording, ts, data)

    def close(self):
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def recording_db(tmp_path):
    """Create a real engine SQLite DB with configurable events.

    Uses screencap.engine.db.create_db + crud so the schema always matches
    the real engine. Returns a RecordingDB helper.
    """
    from screencap.engine.db import create_db, crud

    db_path = tmp_path / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()

    recording = crud.insert_recording(session, {
        "timestamp": 1000.0,
        "platform": "darwin",
        "monitor_width": 1920,
        "monitor_height": 1080,
        "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5,
        "double_click_distance_pixels": 5.0,
    })

    helper = RecordingDB(db_path, engine, session, recording)
    yield helper
    helper.close()


# ---------------------------------------------------------------------------
# FakeRecorder — medium-fidelity stand-in for screencap.engine.Recorder
# ---------------------------------------------------------------------------


class FakeRecorder:
    """Stand-in for screencap.engine.Recorder (external hardware boundary).

    Creates a real recording.db on __enter__ using the engine's create_db +
    crud API so downstream code (export, catalog) works with real data.
    Sets is_recording=False so the live-display loop exits immediately.
    """

    def __init__(self, capture_dir_str, **kwargs):
        self.capture_dir = str(Path(capture_dir_str).resolve())
        self.is_recording = False
        self.health_warning = ""
        self.child_crashes = []
        self._stopped = False
        self._chunk_process_q = None
        self._audio_ack_q = None
        self._flush_requested = None
        self._flush_ack_counter = None

    def __enter__(self):
        self._create_db()
        return self

    def __exit__(self, *args):
        return False

    def wait_for_ready(self, timeout=30):
        return True

    def stop(self):
        self.is_recording = False
        self._stopped = True

    def _create_db(self):
        """Create a recording.db with minimal realistic data."""
        from screencap.engine.db import create_db, crud

        db_path = Path(self.capture_dir) / "recording.db"
        engine, Session = create_db(str(db_path))
        session = Session()

        t = 1000.0
        recording = crud.insert_recording(session, {
            "timestamp": t,
            "platform": "darwin",
            "monitor_width": 1920,
            "monitor_height": 1080,
            "pixel_ratio": 2.0,
            "double_click_interval_seconds": 0.5,
            "double_click_distance_pixels": 5.0,
        })

        # Window event
        crud.insert_window_event(session, recording, t + 0.1, {
            "title": "Documents",
            "app_bundle_id": "com.apple.finder",
            "window_id": "win-1",
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        })

        # Click pair
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

        # Key press+release
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


# ---------------------------------------------------------------------------
# recording_dir — full recording directory structure with real DB
# ---------------------------------------------------------------------------


@pytest.fixture
def recording_dir(tmp_path):
    """Scaffold a recording directory with real engine DB and seed data.

    Creates ``<tmp_path>/recordings/test-rec/recording.db`` with a window
    event, click pair, and keypress pair — enough for the export pipeline
    to produce valid JSONL output.  Returns the recording directory Path.
    """
    from screencap.engine.db import create_db, crud

    recordings_dir = tmp_path / "recordings"
    rec_dir = recordings_dir / "test-rec"
    rec_dir.mkdir(parents=True)

    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()

    t = 1000.0
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

    return rec_dir


# ---------------------------------------------------------------------------
# FakeChunkedRecorder — FakeRecorder with pre-loaded chunk rotation messages
# ---------------------------------------------------------------------------


class FakeChunkedRecorder(FakeRecorder):
    """FakeRecorder that provides chunk queues with pre-loaded rotation messages.

    Simulates a Recorder that produced 2 chunks of 30 seconds each.
    Uses real multiprocessing.Queue instances so ChunkProcessor works
    without modification.
    """

    def __init__(self, capture_dir_str, **kwargs):
        import multiprocessing

        super().__init__(capture_dir_str, **kwargs)
        self._chunk_process_q = multiprocessing.Queue()
        self._audio_ack_q = multiprocessing.Queue()

    def __enter__(self):
        self._create_db()
        self._create_multi_chunk_data()
        self._load_chunk_messages()
        return self

    def _create_multi_chunk_data(self):
        """Add chunk-1 events to the existing DB via raw sqlite3.

        The base _create_db already creates events at t=1000.0–1001.05
        (chunk 0). This adds events in the [t+30, t+60) range for chunk 1.
        """
        import sqlite3

        db_path = Path(self.capture_dir) / "recording.db"
        conn = sqlite3.connect(str(db_path))
        t = 1000.0

        # Window event in chunk 1
        conn.execute(
            "INSERT INTO window_event "
            "(timestamp, recording_id, title, app_bundle_id, window_id, "
            '"left", top, width, height) '
            "VALUES (?, 1, 'Browser', 'com.app.browser', 'win-2', "
            "0, 0, 1920, 1080)",
            (t + 31,),
        )
        # Click pair in chunk 1
        conn.execute(
            "INSERT INTO action_event "
            "(name, timestamp, recording_id, mouse_x, mouse_y, "
            "mouse_button_name, mouse_pressed) "
            "VALUES ('click', ?, 1, 300.0, 400.0, 'left', 1)",
            (t + 35,),
        )
        conn.execute(
            "INSERT INTO action_event "
            "(name, timestamp, recording_id, mouse_x, mouse_y, "
            "mouse_button_name, mouse_pressed) "
            "VALUES ('click', ?, 1, 300.0, 400.0, 'left', 0)",
            (t + 35.05,),
        )
        conn.commit()
        conn.close()

    def _load_chunk_messages(self):
        """Pre-load rotation messages for 2 chunks."""
        t = 1000.0
        self._chunk_process_q.put({
            "type": "chunk_rotated",
            "completed_index": 0,
            "chunk_start_time": t,
            "rotation_time": t + 30,
        })
        self._chunk_process_q.put({
            "type": "final_chunk",
            "completed_index": 1,
            "chunk_start_time": t + 30,
            "rotation_time": t + 60,
        })
