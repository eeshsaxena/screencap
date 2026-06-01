"""Tests for child process health monitoring during recording.

Covers the integration seams:
- Recorder._drain_status_pipe processes record.child_died messages (real pipe)
- CLI _build_live_display renders health warnings
- PID cleanup from crash events
- Force-quit handler PID snapshot safety (AST guard)
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Recorder._drain_status_pipe integration tests (real pipe)
# ---------------------------------------------------------------------------


def _make_recorder_for_drain_test():
    """Create a minimal Recorder with real pipe, bypassing full __init__."""
    from screencap.engine.recorder import Recorder

    r = Recorder.__new__(Recorder)
    r._child_crashes = []
    r._health_warning = ""
    r._recording_duration = None
    r._stopped_event = threading.Event()
    r._ready_event = threading.Event()
    r._status_recv, r._status_send = multiprocessing.Pipe(duplex=False)
    return r


class TestDrainStatusPipe:
    """Tests for Recorder._drain_status_pipe handling record.child_died messages."""

    def test_critical_crash_sets_stopping_warning(self):
        """A critical child crash via the pipe sets health_warning with 'stopping'."""
        r = _make_recorder_for_drain_test()

        # Send the message through the real pipe
        r._status_send.send({
            "type": "record.child_died",
            "task_name": "video_writer",
            "exitcode": -11,
            "pid": 1234,
            "is_critical": True,
        })

        # Run drain in a thread, then stop it
        t = threading.Thread(target=r._drain_status_pipe, daemon=True)
        t.start()
        # Give it time to process the message
        time.sleep(0.2)
        r._stopped_event.set()
        t.join(timeout=2)

        assert "video_writer" in r.health_warning
        assert "stopping" in r.health_warning
        assert r.child_crashes[0]["exitcode"] == -11
        assert r.child_crashes[0]["pid"] == 1234

    def test_degraded_crashes_accumulate_names(self):
        """Multiple degraded crashes accumulate all task names in the warning."""
        r = _make_recorder_for_drain_test()

        r._status_send.send({
            "type": "record.child_died",
            "task_name": "audio_recorder",
            "exitcode": 1,
            "pid": 2000,
            "is_critical": False,
        })
        r._status_send.send({
            "type": "record.child_died",
            "task_name": "window_event_writer",
            "exitcode": 1,
            "pid": 2001,
            "is_critical": False,
        })

        t = threading.Thread(target=r._drain_status_pipe, daemon=True)
        t.start()
        time.sleep(0.2)
        r._stopped_event.set()
        t.join(timeout=2)

        assert "audio_recorder" in r.health_warning
        assert "window_event_writer" in r.health_warning
        assert "degraded" in r.health_warning
        assert len(r.child_crashes) == 2


# ---------------------------------------------------------------------------
# CLI display: _build_live_display with health_warning
# ---------------------------------------------------------------------------


class TestLiveDisplayHealthWarning:
    """Tests for health warning rendering in the CLI live display."""

    def _render(self, group) -> str:
        from rich.console import Console
        console = Console(file=None, force_terminal=True, width=80)
        with console.capture() as capture:
            console.print(group)
        return capture.get()

    def test_health_warning_displayed(self):
        """Health warning string appears in the rendered panel output."""
        from screencap.recorder import _build_live_display

        warning = "\u26a0 video_writer crashed (exit -11) \u2014 stopping"
        group = _build_live_display("test-rec", 10.0, True, health_warning=warning)
        output = self._render(group)
        assert "video_writer" in output
        assert "stopping" in output

    def test_health_and_disk_warnings_coexist(self):
        """Both disk warning and health warning appear when both are set."""
        from screencap.recorder import _build_live_display

        group = _build_live_display(
            "test-rec", 10.0, True,
            disk_warning="Low disk: 2.1 GB free",
            health_warning="\u26a0 audio_recorder crashed \u2014 recording degraded",
        )
        output = self._render(group)
        assert "Low disk" in output
        assert "audio_recorder" in output


# ---------------------------------------------------------------------------
# CLI: PID cleanup from child_crashes
# ---------------------------------------------------------------------------


class TestPidCleanup:
    """Tests for dead PID removal using the same logic as the CLI loop."""

    def test_dead_pid_pruned_and_none_pid_safe(self):
        """Real PIDs are removed; None PIDs (threads) are safely skipped."""
        _child_pids = [100, 200, 300]
        crashes = [
            {"task_name": "video_writer", "pid": 200, "is_critical": True},
            {"task_name": "event_processor", "pid": None, "is_critical": True},
        ]

        for crash in crashes:
            dead_pid = crash.get("pid")
            if dead_pid and dead_pid in _child_pids:
                _child_pids.remove(dead_pid)

        assert _child_pids == [100, 300]


# ---------------------------------------------------------------------------
# Force-quit handler: PID snapshot safety (AST guard)
# ---------------------------------------------------------------------------


class TestForceQuitPidSnapshot:
    """Structural guard: force-quit handler must use _pids_snapshot, not raw _child_pids."""

    def test_force_quit_uses_pid_snapshot(self):
        """Verify _force_exit iterates _pids_snapshot to prevent PID recycling."""
        import ast
        import inspect

        from screencap.recorder import _run_screen_recorder

        # ``_run_screen_recorder`` is re-exported from
        # ``screencap.engine.screen_recorder`` (recorder.py is now a CLI
        # adapter); ``inspect.getsource`` resolves to the engine source,
        # where the nested ``_force_exit`` lives.
        source = inspect.getsource(_run_screen_recorder)
        tree = ast.parse(source)

        force_exit_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_force_exit":
                force_exit_fn = node
                break

        assert force_exit_fn is not None, "_force_exit function not found"
        source_text = ast.get_source_segment(source, force_exit_fn)
        assert "_pids_snapshot" in source_text, (
            "_force_exit should use _pids_snapshot for PID iteration safety"
        )
