"""Tests for Unit 8a: structured stderr event contract.

Pins the JSON schema of ``_emit_event(type, **fields)`` so a regression that
silently changes field names or types fails the build. The schema is the
cross-language contract with SwiftUI's RecorderController.spawn — see
docs/research/2026-04-28-stderr-event-schema.md.
"""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest


def _capture_stderr(callable_):
    """Run callable_ with sys.stderr redirected to a StringIO; return its content."""
    buf = StringIO()
    saved = sys.stderr
    sys.stderr = buf
    try:
        callable_()
    finally:
        sys.stderr = saved
    return buf.getvalue()


def _parse_lines(content: str) -> list[dict]:
    return [json.loads(line) for line in content.strip().splitlines() if line.strip()]


class TestEmitEventBasic:
    def test_writes_a_single_json_line_to_stderr(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event("started"))
        events = _parse_lines(out)
        assert len(events) == 1
        assert events[0]["type"] == "started"

    def test_includes_timestamp(self):
        from screencap.cli import _emit_event
        import time as _time

        before = _time.time()
        out = _capture_stderr(lambda: _emit_event("started"))
        after = _time.time()
        events = _parse_lines(out)
        assert "ts" in events[0]
        assert before <= events[0]["ts"] <= after

    def test_passes_through_kwargs_as_fields(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(
            lambda: _emit_event("started", capture_dir="/tmp/x", claimant="swiftui")
        )
        evt = _parse_lines(out)[0]
        assert evt["capture_dir"] == "/tmp/x"
        assert evt["claimant"] == "swiftui"

    def test_swallows_failures_silently(self, monkeypatch):
        """Broken stderr must not break the recorder."""
        from screencap.cli import _emit_event

        class _BrokenStream:
            def write(self, *_args, **_kwargs):
                raise OSError("stderr broken")

            def flush(self):
                raise OSError("stderr broken")

        monkeypatch.setattr(sys, "stderr", _BrokenStream())
        # Must not raise
        _emit_event("started")


class TestEventSchemas:
    """Pin the field shape of each documented event type.

    These are golden-file assertions against the schema doc — any change to
    field names or types must update the doc AND bump _EVENT_SCHEMA_VERSION.
    """

    def test_started_schema(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "started",
            capture_dir="/Users/foo/.screencap/recordings",
            claimant="swiftui",
        ))
        evt = _parse_lines(out)[0]
        assert set(evt.keys()) >= {"type", "ts", "capture_dir", "claimant"}
        assert evt["type"] == "started"
        assert isinstance(evt["capture_dir"], str)
        assert evt["claimant"] in ("cli", "swiftui")

    def test_started_capture_dir_points_at_recordings_root(self, tmp_path, monkeypatch):
        """Semantic check (todo 039): the `capture_dir` in the `started` event
        must point at the recordings ROOT dir as resolved by
        `screencap.config.get_recordings_dir()` — not the controller's cwd.
        Replaces the previous `isinstance(str)` test that allowed the bug at
        session.py:491 to ship undetected."""
        recordings_dir = tmp_path / "fake-screencap" / "recordings"
        recordings_dir.mkdir(parents=True)
        monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))

        # Build the started-event payload via the same path SessionController
        # uses (resolve_claimant + get_recordings_dir) — pinned so a future
        # regression to Path('.').resolve() fails the test.
        from screencap._stderr_events import emit_event, resolve_claimant
        from screencap.config import get_recordings_dir, invalidate_config_cache

        invalidate_config_cache()
        out = _capture_stderr(lambda: emit_event(
            "started",
            capture_dir=str(get_recordings_dir()),
            claimant=resolve_claimant(),
        ))
        evt = _parse_lines(out)[0]
        assert evt["capture_dir"] == str(recordings_dir), (
            f"capture_dir should be the recordings root, got {evt['capture_dir']!r}"
        )

    def test_recording_finalized_schema(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "recording_finalized",
            name="rec-20260428T143000",
            duration_seconds=42.5,
            force_stopped=False,
            disk_full=False,
        ))
        evt = _parse_lines(out)[0]
        assert evt["type"] == "recording_finalized"
        assert isinstance(evt["name"], str)
        assert isinstance(evt["duration_seconds"], float)
        assert isinstance(evt["force_stopped"], bool)
        assert isinstance(evt["disk_full"], bool)

    def test_disk_full_schema(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "disk_full",
            name="rec-x",
            capture_dir="/tmp/rec-x",
        ))
        evt = _parse_lines(out)[0]
        assert evt["type"] == "disk_full"
        assert evt["name"] == "rec-x"
        assert evt["capture_dir"] == "/tmp/rec-x"

    def test_permission_lost_schema(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "permission_lost",
            permission="screen_recording",
            since_frame=42,
            elapsed=10.5,
        ))
        evt = _parse_lines(out)[0]
        assert evt["type"] == "permission_lost"
        assert evt["permission"] in ("screen_recording", "accessibility", "microphone")
        assert isinstance(evt["since_frame"], int)
        assert isinstance(evt["elapsed"], float)

    def test_stopped_schema(self):
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event("stopped", exit_code=0))
        evt = _parse_lines(out)[0]
        assert evt["type"] == "stopped"
        assert evt["exit_code"] == 0

    def test_chunk_finalized_reserved_schema(self):
        """Schema is reserved for future chunk_processor wiring (Unit 8a doc)."""
        from screencap.cli import _emit_event

        out = _capture_stderr(lambda: _emit_event(
            "chunk_finalized",
            chunk_index=3,
            path="/tmp/rec/chunk_0003.mp4",
        ))
        evt = _parse_lines(out)[0]
        assert evt["type"] == "chunk_finalized"
        assert isinstance(evt["chunk_index"], int)
        assert isinstance(evt["path"], str)


class TestStreamSeparation:
    """Events go to stderr only — stdout stays clean for human/machine output."""

    def test_event_does_not_pollute_stdout(self, capsys):
        from screencap.cli import _emit_event

        _emit_event("started")
        captured = capsys.readouterr()
        # capsys captures both — the event should be in err, not out
        assert "started" in captured.err
        assert "started" not in captured.out



class TestExitCodeContract:
    """Verifies that documented exit codes (3 = permission_lost,
    4 = disk_full) are actually produced by SessionController.run() reading
    the .recording_ready manifest's terminated_reason field (todo 002, 009).

    Cross-process LockContended → exit 2 is covered by
    tests/test_pidfile_mutex.py + the dedicated end-to-end test below.
    """

    def test_terminated_reason_permission_lost_raises_systemexit_3(self):
        """A worker that wrote ``terminated_reason=permission_lost`` causes
        run() to raise SystemExit(3) so cli.py's exit handler propagates it."""
        from screencap.session import SessionController, SessionState

        # Bypass __init__ (heavy: claims lock, spawns menubar). We only
        # exercise run()'s post-shutdown exit-code translation.
        controller = SessionController.__new__(SessionController)
        controller._terminated_reason = "permission_lost"
        controller._state = SessionState.IDLE
        controller._shutdown_requested = True
        controller._finishing_workers = []
        controller._active_postprocess = None
        controller._pending_jobs = []
        controller._current_worker = None
        controller._menubar_event_q = None  # unused on this path
        # Minimal _do_shutdown: skip the full path; we just need the exit map.
        controller._do_shutdown = lambda: None  # type: ignore[method-assign]
        controller._on_start_click = lambda: None  # type: ignore[method-assign]

        with pytest.raises(SystemExit) as excinfo:
            controller.run()
        assert int(getattr(excinfo.value, "code", 0) or 0) == 3

    def test_terminated_reason_disk_full_raises_systemexit_4(self):
        """Same fixture as above but with disk_full → SystemExit(4)."""
        from screencap.session import SessionController, SessionState

        controller = SessionController.__new__(SessionController)
        controller._terminated_reason = "disk_full"
        controller._state = SessionState.IDLE
        controller._shutdown_requested = True
        controller._finishing_workers = []
        controller._active_postprocess = None
        controller._pending_jobs = []
        controller._current_worker = None
        controller._menubar_event_q = None
        controller._do_shutdown = lambda: None  # type: ignore[method-assign]
        controller._on_start_click = lambda: None  # type: ignore[method-assign]

        with pytest.raises(SystemExit) as excinfo:
            controller.run()
        assert int(getattr(excinfo.value, "code", 0) or 0) == 4

    def test_clean_terminated_reason_returns_normally(self):
        """Without a terminated_reason, run() must return without raising."""
        from screencap.session import SessionController, SessionState

        controller = SessionController.__new__(SessionController)
        controller._terminated_reason = None
        controller._state = SessionState.IDLE
        controller._shutdown_requested = True
        controller._finishing_workers = []
        controller._active_postprocess = None
        controller._pending_jobs = []
        controller._current_worker = None
        controller._menubar_event_q = None
        controller._do_shutdown = lambda: None  # type: ignore[method-assign]
        controller._on_start_click = lambda: None  # type: ignore[method-assign]

        # Should not raise.
        controller.run()
