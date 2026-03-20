"""Tests for screencap.exporter module."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from sc_engine.events import (
    MouseClickEvent,
    MouseButton,
)
from screencap.exporter import ExportError, build_export_metadata, export_recording


def _make_click(ts=1.0):
    return MouseClickEvent(timestamp=ts, x=100, y=200, button=MouseButton.LEFT)


def _mock_capture(export_events=None):
    """Create a mock Capture that returns given events from export_events()."""
    capture = mock.MagicMock()
    capture.__enter__ = mock.MagicMock(return_value=capture)
    capture.__exit__ = mock.MagicMock(return_value=False)
    if export_events is None:
        export_events = [_make_click()]
    capture.export_events.return_value = export_events
    return capture


@pytest.mark.parametrize("exclude_moves", [True, False])
def test_build_export_metadata(exclude_moves):
    meta = build_export_metadata(exclude_moves=exclude_moves)
    assert meta["_meta"] is True
    assert meta["format_version"] == 2
    assert "screencap_version" in meta
    assert "exported_at" in meta
    assert meta["exclude_moves"] is exclude_moves


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

    def failing_capture(*args, **kwargs):
        capture = mock.MagicMock()
        capture.__enter__ = mock.MagicMock(return_value=capture)
        capture.__exit__ = mock.MagicMock(return_value=False)
        capture.export_events.side_effect = RuntimeError("boom")
        return capture

    with mock.patch("sc_engine.capture.CaptureSession.load", side_effect=failing_capture):
        try:
            export_recording(rec_dir, out_file, exclude_moves=False)
        except RuntimeError:
            pass

    assert not os.path.exists(out_file + ".tmp")
    assert not os.path.exists(out_file)


def test_export_recording_missing_db_raises_export_error(tmp_path):
    """Missing recording database should raise ExportError."""
    rec_dir = tmp_path / "old-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    with mock.patch(
        "sc_engine.capture.CaptureSession.load",
        side_effect=FileNotFoundError("Capture not found"),
    ):
        with pytest.raises(ExportError, match="No recording database found"):
            export_recording(rec_dir, out_file, exclude_moves=False)

    assert not os.path.exists(out_file)


