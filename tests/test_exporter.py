"""Tests for screencap.exporter module."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from sc_engine.events import (
    EventType,
    MouseClickEvent,
    MouseButton,
    WindowSwitchEvent,
)
from screencap.exporter import build_export_metadata, export_recording


def _mock_event(event_json='{"type":"mouse.singleclick","timestamp":1.0,"x":100,"y":200}'):
    """Create a mock Pydantic event whose model_dump_json() returns the given JSON."""
    event = mock.MagicMock()
    event.model_dump_json.return_value = event_json
    # Not a WindowSwitchEvent
    event.__class__ = type("FakeEvent", (), {})
    return event


def _make_click(ts=1.0):
    return MouseClickEvent(timestamp=ts, x=100, y=200, button=MouseButton.LEFT)


def _make_window_switch(ts=0.5, bundle_id="com.apple.finder", title="Documents"):
    return WindowSwitchEvent(
        timestamp=ts, app_name="Finder", app_bundle_id=bundle_id,
        window_title=title, window_id="1", x=0, y=0, width=800, height=600,
    )


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


def test_export_recording_writes_metadata_header(tmp_path):
    """First line of output should be a _meta JSON header with format_version."""
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
    assert header["format_version"] == 2
    assert "screencap_version" in header

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


def test_export_includes_window_switch_events(tmp_path):
    """Window.switch events should appear in the exported JSONL."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    events = [
        _make_window_switch(ts=0.5),
        _make_click(ts=1.0),
    ]
    capture = _mock_capture(export_events=events)
    meta = build_export_metadata(exclude_moves=True)

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        count = export_recording(rec_dir, out_file, exclude_moves=True, metadata=meta)

    assert count == 2
    lines = open(out_file).read().strip().split("\n")
    assert len(lines) == 3  # meta + window.switch + click

    ws = json.loads(lines[1])
    assert ws["type"] == "window.switch"
    assert ws["app_bundle_id"] == "com.apple.finder"
    assert ws["window_title"] == "Documents"

    click = json.loads(lines[2])
    assert click["type"] == "mouse.singleclick"


def test_privacy_filter_suppresses_excluded_apps(tmp_path):
    """Privacy filter should suppress window.switch events for EXCLUDE apps."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    events = [
        _make_window_switch(ts=0.5, bundle_id="com.1password.1password"),
        _make_click(ts=1.0),
    ]
    capture = _mock_capture(export_events=events)

    # Build a filter that suppresses the event
    def exclude_filter(event):
        if event.app_bundle_id == "com.1password.1password":
            return None
        return event

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        from screencap.exporter import _write_events
        with open(out_file, "w") as f:
            count = _write_events(capture, f, True, None, privacy_filter=exclude_filter)

    assert count == 1  # only the click, window.switch suppressed
    lines = open(out_file).read().strip().split("\n")
    event = json.loads(lines[0])
    assert event["type"] == "mouse.singleclick"


def test_privacy_filter_masks_titles(tmp_path):
    """Privacy filter can replace window_title for MASK_WINDOW apps."""
    rec_dir = tmp_path / "my-rec"
    rec_dir.mkdir()
    out_file = str(rec_dir / "events.jsonl")

    events = [
        _make_window_switch(ts=0.5, title="Confidential Email - Subject"),
    ]
    capture = _mock_capture(export_events=events)

    def mask_filter(event):
        return event.model_copy(update={"window_title": event.app_name})

    with mock.patch("sc_engine.capture.CaptureSession.load", return_value=capture):
        from screencap.exporter import _write_events
        with open(out_file, "w") as f:
            count = _write_events(capture, f, True, None, privacy_filter=mask_filter)

    assert count == 1
    ws = json.loads(open(out_file).read().strip())
    assert ws["window_title"] == "Finder"  # masked to app_name


def test_build_privacy_filter_cloud_intent_forces_public_mode():
    """cloud_intent=True overrides configured privacy mode to public.

    The public mode action matrix treats CHAT apps (Slack, Teams) as
    MASK_WINDOW instead of TEXT_REDACT, which replaces the window title
    with the app name — preventing leakage of Slack channel names etc.
    """
    from screencap.exporter import build_privacy_filter

    # Slack (com.tinyspeck.slackmacgap) is classified as CHAT.
    # internal mode → TEXT_REDACT (passes through with scrubbing).
    # public mode → MASK_WINDOW (replaces title with app name).
    slack_event = _make_window_switch(
        ts=1.0, bundle_id="com.tinyspeck.slackmacgap", title="#secret-channel",
    )
    # model_copy to set app_name since _make_window_switch uses "Finder"
    slack_event = slack_event.model_copy(update={"app_name": "Slack"})

    # With cloud_intent=True, even if configured as "internal", should use public
    pf_cloud = build_privacy_filter(privacy_mode="internal", cloud_intent=True)
    result = pf_cloud(slack_event)
    # MASK_WINDOW → title replaced with app name
    assert result is not None
    assert result.window_title == "Slack"
    assert result.domain is None

    # Without cloud_intent, internal mode lets CHAT through with original title
    pf_local = build_privacy_filter(privacy_mode="internal", cloud_intent=False)
    result_local = pf_local(slack_event)
    assert result_local is not None
    assert result_local.window_title == "#secret-channel"
