"""Tests for screencap.exporter module."""

from __future__ import annotations

import json
import os
from unittest import mock

from screencap.exporter import build_export_metadata, export_recording


def _mock_action(event_json='{"type":"mouse.singleclick","timestamp":1.0,"x":100,"y":200}'):
    """Create a mock Action whose event.model_dump_json() returns the given JSON."""
    event = mock.MagicMock()
    event.model_dump_json.return_value = event_json
    action = mock.MagicMock()
    action.event = event
    return action


def _mock_capture(actions=None):
    """Create a mock Capture that yields given actions."""
    capture = mock.MagicMock()
    capture.__enter__ = mock.MagicMock(return_value=capture)
    capture.__exit__ = mock.MagicMock(return_value=False)
    if actions is None:
        actions = [_mock_action()]
    capture.actions.return_value = iter(actions)
    return capture


def test_build_export_metadata():
    meta = build_export_metadata(exclude_moves=True)
    assert meta["_meta"] is True
    assert "screencap_version" in meta
    assert "exported_at" in meta
    assert meta["exclude_moves"] is True


def test_build_export_metadata_include_moves():
    meta = build_export_metadata(exclude_moves=False)
    assert meta["exclude_moves"] is False


def test_export_recording_writes_metadata_header(tmp_path):
    """First line of output should be a _meta JSON header."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    capture = _mock_capture()
    meta = build_export_metadata(exclude_moves=False)

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        count = export_recording(rec_dir, out_file, exclude_moves=False, metadata=meta)

    assert count == 1
    lines = open(out_file).read().strip().split("\n")
    assert len(lines) == 2  # metadata + 1 event

    header = json.loads(lines[0])
    assert header["_meta"] is True
    assert "screencap_version" in header
    assert "exported_at" in header

    event = json.loads(lines[1])
    assert event["type"] == "mouse.singleclick"


def test_export_recording_no_metadata(tmp_path):
    """When metadata is None, no header line is written."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    capture = _mock_capture()

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        count = export_recording(rec_dir, out_file, exclude_moves=False, metadata=None)

    assert count == 1
    lines = open(out_file).read().strip().split("\n")
    assert len(lines) == 1  # just the event, no header


def test_export_recording_atomic_write_cleanup_on_failure(tmp_path):
    """On failure, .tmp file should be cleaned up and output should not exist."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    # Make Capture.load raise after opening file
    def failing_capture(*args, **kwargs):
        capture = mock.MagicMock()
        capture.__enter__ = mock.MagicMock(return_value=capture)
        capture.__exit__ = mock.MagicMock(return_value=False)
        capture.actions.side_effect = RuntimeError("boom")
        return capture

    with mock.patch("sc_engine.capture.CaptureSession.load", side_effect=failing_capture):
        try:
            export_recording(rec_dir, out_file, exclude_moves=False)
        except RuntimeError:
            pass

    # Neither .tmp nor the final file should exist
    assert not os.path.exists(out_file + ".tmp")
    assert not os.path.exists(out_file)


def test_export_recording_atomic_write_success(tmp_path):
    """On success, .tmp should not persist — only the final file."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    capture = _mock_capture()

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        export_recording(rec_dir, out_file, exclude_moves=False)

    assert os.path.exists(out_file)
    assert not os.path.exists(out_file + ".tmp")


def test_export_recording_legacy_db_returns_negative(tmp_path):
    """Legacy capture.db format should return -1."""
    rec_dir = tmp_path / "old-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    with mock.patch(
        "sc_engine.capture.CaptureSession.load",
        side_effect=FileNotFoundError("Capture not found"),
    ):
        count = export_recording(rec_dir, out_file, exclude_moves=False)

    assert count == -1
    assert not os.path.exists(out_file)
