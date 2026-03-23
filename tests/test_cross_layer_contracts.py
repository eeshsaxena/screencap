"""Cross-layer contract tests.

Verify that the screencap.engine public API matches what the screencap layer
calls. If the engine renames Recorder.stop() or changes CaptureSession.load's
return type, these tests catch it.
"""

from __future__ import annotations

import inspect


def test_recorder_api_surface():
    """Verify engine.Recorder has every attribute/method that recorder.py uses."""
    from screencap.engine.recorder import Recorder

    # Methods used by screencap.recorder.start_recording
    assert hasattr(Recorder, "__enter__")
    assert hasattr(Recorder, "__exit__")
    assert hasattr(Recorder, "wait_for_ready")
    assert hasattr(Recorder, "stop")

    # Properties/attributes read by the screencap layer
    # (verified via instance after __init__, not on the class)
    sig = inspect.signature(Recorder.__init__)
    params = sig.parameters
    assert "capture_dir" in params

    # Check that key properties exist on the class
    assert isinstance(Recorder.is_recording, property)
    assert isinstance(Recorder.health_warning, property)
    assert isinstance(Recorder.child_crashes, property)

    # Instance attributes set in __init__ — verify they appear in the source
    source = inspect.getsource(Recorder.__init__)
    for attr in (
        "capture_dir",
        "_chunk_process_q",
        "_audio_ack_q",
        "_flush_requested",
        "_flush_ack_counter",
    ):
        assert attr in source, f"Recorder.__init__ must set self.{attr}"


def test_capture_session_load_returns_expected_interface(recording_db):
    """Verify CaptureSession.load() returns object with export_events."""
    from screencap.engine import Capture, CaptureSession

    # Capture is an alias for CaptureSession
    assert Capture is CaptureSession

    # Load a real DB and verify the interface
    capture_dir = recording_db.db_path.parent
    with CaptureSession.load(str(capture_dir)) as session:
        assert hasattr(session, "export_events")
        assert callable(session.export_events)
        assert hasattr(session, "capture_dir")
        assert hasattr(session, "id")
        assert hasattr(session, "started_at")


def test_engine_config_attributes_match_screencap_reads():
    """Verify engine config has attributes the screencap layer reads."""
    from screencap.engine.config import config

    # screencap.recorder reads RECORD_WINDOW_DATA from engine config
    assert hasattr(config, "RECORD_WINDOW_DATA")
    assert isinstance(config.RECORD_WINDOW_DATA, bool)
