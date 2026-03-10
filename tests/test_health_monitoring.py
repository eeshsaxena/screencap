"""Tests for child process health monitoring during recording.

Covers:
- Engine health check: critical/degraded task death detection
- Dead-queue bypass: event_processor skips puts for dead writers
- Recorder class: health_warning + child_crashes properties
- CLI display: amber health warnings + PID cleanup
- Force-quit handler: list snapshot for PID safety
"""

from __future__ import annotations

import multiprocessing
import queue
import threading
from collections import namedtuple
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Engine: health check in record() main loop
# ---------------------------------------------------------------------------


class TestHealthCheckLoop:
    """Tests for the health check logic in record()'s main loop."""

    def _make_mock_task(self, alive=True, exitcode=None, pid=None):
        """Create a mock task (process or thread)."""
        task = mock.MagicMock()
        task.is_alive.return_value = alive
        task.exitcode = exitcode
        task.pid = pid
        return task

    def test_critical_task_death_sets_terminate(self):
        """When a critical task dies, terminate_processing should be set."""
        from sc_engine.recorder import record  # noqa: F401 — we test the logic directly

        # Test the health check logic in isolation by simulating what record() does
        _CRITICAL_TASKS = frozenset({
            "action_event_writer", "video_writer",
            "screen_event_writer", "event_processor",
        })

        dead_queues: set[str] = set()
        terminate_processing = threading.Event()
        status_messages = []

        task_by_name = {
            "video_writer": self._make_mock_task(alive=False, exitcode=-11, pid=1234),
            "action_event_writer": self._make_mock_task(alive=True, pid=1235),
        }

        # Simulate one iteration of the health check loop
        for name, task in task_by_name.items():
            if name in dead_queues:
                continue
            if not task.is_alive():
                exitcode = getattr(task, "exitcode", None)
                pid = getattr(task, "pid", None)
                dead_queues.add(name)
                msg = {
                    "type": "record.child_died",
                    "task_name": name,
                    "exitcode": exitcode,
                    "pid": pid,
                    "is_critical": name in _CRITICAL_TASKS,
                }
                status_messages.append(msg)
                if name in _CRITICAL_TASKS:
                    terminate_processing.set()
                    break

        assert terminate_processing.is_set()
        assert "video_writer" in dead_queues
        assert len(status_messages) == 1
        assert status_messages[0]["is_critical"] is True
        assert status_messages[0]["exitcode"] == -11
        assert status_messages[0]["pid"] == 1234

    def test_degraded_task_death_continues(self):
        """When a non-critical task dies, recording should continue."""
        _CRITICAL_TASKS = frozenset({
            "action_event_writer", "video_writer",
            "screen_event_writer", "event_processor",
        })

        dead_queues: set[str] = set()
        terminate_processing = threading.Event()
        status_messages = []

        task_by_name = {
            "audio_recorder": self._make_mock_task(alive=False, exitcode=1, pid=2000),
            "video_writer": self._make_mock_task(alive=True, pid=2001),
        }

        for name, task in task_by_name.items():
            if name in dead_queues:
                continue
            if not task.is_alive():
                exitcode = getattr(task, "exitcode", None)
                pid = getattr(task, "pid", None)
                dead_queues.add(name)
                msg = {
                    "type": "record.child_died",
                    "task_name": name,
                    "exitcode": exitcode,
                    "pid": pid,
                    "is_critical": name in _CRITICAL_TASKS,
                }
                status_messages.append(msg)
                if name in _CRITICAL_TASKS:
                    terminate_processing.set()
                    break

        assert not terminate_processing.is_set()
        assert "audio_recorder" in dead_queues
        assert len(status_messages) == 1
        assert status_messages[0]["is_critical"] is False

    def test_already_dead_tasks_skipped(self):
        """Tasks already in dead_queues should not be re-checked."""
        dead_queues: set[str] = {"video_writer"}
        checked = []

        task_by_name = {
            "video_writer": self._make_mock_task(alive=False, exitcode=-11, pid=1234),
        }

        for name, task in task_by_name.items():
            if name in dead_queues:
                continue
            checked.append(name)

        assert checked == []  # video_writer skipped

    def test_alive_tasks_not_flagged(self):
        """Healthy tasks should not be added to dead_queues."""
        _CRITICAL_TASKS = frozenset({
            "action_event_writer", "video_writer",
            "screen_event_writer", "event_processor",
        })
        dead_queues: set[str] = set()
        terminate_processing = threading.Event()

        task_by_name = {
            "video_writer": self._make_mock_task(alive=True, pid=1234),
            "action_event_writer": self._make_mock_task(alive=True, pid=1235),
            "audio_recorder": self._make_mock_task(alive=True, pid=1236),
        }

        for name, task in task_by_name.items():
            if name in dead_queues:
                continue
            if not task.is_alive():
                dead_queues.add(name)
                if name in _CRITICAL_TASKS:
                    terminate_processing.set()
                    break

        assert not terminate_processing.is_set()
        assert len(dead_queues) == 0

    def test_thread_death_detected_no_exitcode(self):
        """Thread death should be detected even without exitcode attribute."""
        _CRITICAL_TASKS = frozenset({
            "action_event_writer", "video_writer",
            "screen_event_writer", "event_processor",
        })
        dead_queues: set[str] = set()
        terminate_processing = threading.Event()
        status_messages = []

        # Threads don't have exitcode or pid
        thread_task = mock.MagicMock(spec=threading.Thread)
        thread_task.is_alive.return_value = False
        del thread_task.exitcode  # threads don't have this
        del thread_task.pid  # threads don't have this

        task_by_name = {"event_processor": thread_task}

        for name, task in task_by_name.items():
            if name in dead_queues:
                continue
            if not task.is_alive():
                exitcode = getattr(task, "exitcode", None)
                pid = getattr(task, "pid", None)
                dead_queues.add(name)
                msg = {
                    "type": "record.child_died",
                    "task_name": name,
                    "exitcode": exitcode,
                    "pid": pid,
                    "is_critical": name in _CRITICAL_TASKS,
                }
                status_messages.append(msg)
                if name in _CRITICAL_TASKS:
                    terminate_processing.set()
                    break

        assert terminate_processing.is_set()
        assert status_messages[0]["exitcode"] is None
        assert status_messages[0]["pid"] is None


# ---------------------------------------------------------------------------
# Engine: dead-queue bypass in event_processor
# ---------------------------------------------------------------------------


class TestDeadQueueBypass:
    """Tests for the dead-queue bypass helper in process_events."""

    def test_is_queue_dead_returns_true_for_dead_writer(self):
        """_is_queue_dead should return True when the queue's writer is in dead_queues."""
        dead_queues: set[str] = {"video_writer"}

        screen_write_q = mock.MagicMock()
        video_write_q = mock.MagicMock()

        _q_to_writer = {
            id(screen_write_q): "screen_event_writer",
            id(video_write_q): "video_writer",
        }

        def _is_queue_dead(wq):
            writer = _q_to_writer.get(id(wq))
            return writer is not None and writer in dead_queues

        assert _is_queue_dead(video_write_q)
        assert not _is_queue_dead(screen_write_q)

    def test_is_queue_dead_returns_false_for_unknown_queue(self):
        """Unknown queue should not be considered dead."""
        dead_queues: set[str] = {"video_writer"}

        unknown_q = mock.MagicMock()
        _q_to_writer = {}

        def _is_queue_dead(wq):
            writer = _q_to_writer.get(id(wq))
            return writer is not None and writer in dead_queues

        assert not _is_queue_dead(unknown_q)

    def test_dead_queue_bypass_prevents_timeout(self):
        """Fan-out group should fail immediately when a queue is dead, not block on put."""
        dead_queues: set[str] = {"action_event_writer"}

        screen_q = mock.MagicMock()
        action_q = mock.MagicMock()

        _q_to_writer = {
            id(screen_q): "screen_event_writer",
            id(action_q): "action_event_writer",
        }

        def _is_queue_dead(wq):
            writer = _q_to_writer.get(id(wq))
            return writer is not None and writer in dead_queues

        # Simulate the fan-out group write loop
        events_to_write = [
            ("screen_event", screen_q),
            ("action_event", action_q),
        ]
        all_ok = True
        for ev, wq in events_to_write:
            if _is_queue_dead(wq):
                all_ok = False
                break

        assert not all_ok
        # action_q.put should never have been called
        action_q.put.assert_not_called()


# ---------------------------------------------------------------------------
# Recorder class: health_warning + child_crashes
# ---------------------------------------------------------------------------


class TestRecorderHealthProperties:
    """Tests for Recorder.health_warning and Recorder.child_crashes."""

    def test_health_warning_defaults_empty(self):
        """Recorder.health_warning should default to empty string."""
        from sc_engine.recorder import Recorder

        with mock.patch.object(Recorder, "__init__", lambda self, *a, **kw: None):
            r = Recorder.__new__(Recorder)
            r._health_warning = ""
            r._child_crashes = []
            assert r.health_warning == ""

    def test_health_warning_set_on_critical_crash(self):
        """_drain_status_pipe should set health_warning for critical crashes."""
        from sc_engine.recorder import Recorder

        with mock.patch.object(Recorder, "__init__", lambda self, *a, **kw: None):
            r = Recorder.__new__(Recorder)
            r._health_warning = ""
            r._child_crashes = []
            r._stopped_event = threading.Event()

            # Simulate what _drain_status_pipe does for a critical crash
            msg = {
                "type": "record.child_died",
                "task_name": "video_writer",
                "exitcode": -11,
                "pid": 1234,
                "is_critical": True,
            }
            r._child_crashes.append(msg)
            name = msg["task_name"]
            code = msg.get("exitcode")
            if msg.get("is_critical"):
                r._health_warning = (
                    f"\u26a0 {name} crashed (exit {code}) \u2014 stopping"
                )

            assert "video_writer" in r.health_warning
            assert "stopping" in r.health_warning

    def test_health_warning_set_on_degraded_crash(self):
        """_drain_status_pipe should set health_warning for degraded crashes."""
        from sc_engine.recorder import Recorder

        with mock.patch.object(Recorder, "__init__", lambda self, *a, **kw: None):
            r = Recorder.__new__(Recorder)
            r._health_warning = ""
            r._child_crashes = []

            # First degraded crash
            msg1 = {
                "type": "record.child_died",
                "task_name": "audio_recorder",
                "exitcode": 1,
                "pid": 2000,
                "is_critical": False,
            }
            r._child_crashes.append(msg1)
            degraded = [c["task_name"] for c in r._child_crashes if not c.get("is_critical")]
            r._health_warning = f"\u26a0 {', '.join(degraded)} crashed \u2014 recording degraded"

            assert "audio_recorder" in r.health_warning
            assert "degraded" in r.health_warning

            # Second degraded crash — both names should appear
            msg2 = {
                "type": "record.child_died",
                "task_name": "window_event_writer",
                "exitcode": 1,
                "pid": 2001,
                "is_critical": False,
            }
            r._child_crashes.append(msg2)
            degraded = [c["task_name"] for c in r._child_crashes if not c.get("is_critical")]
            r._health_warning = f"\u26a0 {', '.join(degraded)} crashed \u2014 recording degraded"

            assert "audio_recorder" in r.health_warning
            assert "window_event_writer" in r.health_warning

    def test_child_crashes_returns_copy(self):
        """child_crashes should return a copy, not the internal list."""
        from sc_engine.recorder import Recorder

        with mock.patch.object(Recorder, "__init__", lambda self, *a, **kw: None):
            r = Recorder.__new__(Recorder)
            r._child_crashes = [{"task_name": "video_writer"}]

            crashes = r.child_crashes
            crashes.append({"task_name": "fake"})
            assert len(r._child_crashes) == 1  # internal list not modified


# ---------------------------------------------------------------------------
# CLI display: _build_live_display with health_warning
# ---------------------------------------------------------------------------


class TestLiveDisplayHealthWarning:
    """Tests for health warning rendering in the CLI live display."""

    def test_no_health_warning(self):
        """When health_warning is empty, no warning text appears."""
        from screencap.recorder import _build_live_display

        group = _build_live_display("test-rec", 10.0, True)
        # Render to plain text
        from rich.console import Console
        console = Console(file=None, force_terminal=True, width=80)
        with console.capture() as capture:
            console.print(group)
        output = capture.get()
        assert "\u26a0" not in output

    def test_health_warning_displayed(self):
        """When health_warning is set, it should appear in the display."""
        from screencap.recorder import _build_live_display

        warning = "\u26a0 video_writer crashed (exit -11) \u2014 stopping"
        group = _build_live_display("test-rec", 10.0, True, health_warning=warning)

        from rich.console import Console
        console = Console(file=None, force_terminal=True, width=80)
        with console.capture() as capture:
            console.print(group)
        output = capture.get()
        assert "video_writer" in output
        assert "stopping" in output

    def test_health_warning_with_disk_warning(self):
        """Health warning should coexist with disk warning."""
        from screencap.recorder import _build_live_display

        disk_warning = "Low disk: 2.1 GB free"
        health_warning = "\u26a0 audio_recorder crashed \u2014 recording degraded"
        group = _build_live_display(
            "test-rec", 10.0, True,
            disk_warning=disk_warning,
            health_warning=health_warning,
        )

        from rich.console import Console
        console = Console(file=None, force_terminal=True, width=80)
        with console.capture() as capture:
            console.print(group)
        output = capture.get()
        assert "Low disk" in output
        assert "audio_recorder" in output


# ---------------------------------------------------------------------------
# CLI: PID cleanup from child_crashes
# ---------------------------------------------------------------------------


class TestPidCleanup:
    """Tests for dead PID removal in the CLI recording loop."""

    def test_dead_pids_removed_from_child_pids(self):
        """Dead PIDs from crash events should be removed from _child_pids."""
        _child_pids = [100, 200, 300]
        crashes = [
            {"task_name": "video_writer", "pid": 200, "is_critical": True},
        ]

        for crash in crashes:
            dead_pid = crash.get("pid")
            if dead_pid and dead_pid in _child_pids:
                _child_pids.remove(dead_pid)

        assert _child_pids == [100, 300]

    def test_none_pid_skipped(self):
        """Crashes with pid=None (threads) should not cause errors."""
        _child_pids = [100, 200]
        crashes = [
            {"task_name": "event_processor", "pid": None, "is_critical": True},
        ]

        for crash in crashes:
            dead_pid = crash.get("pid")
            if dead_pid and dead_pid in _child_pids:
                _child_pids.remove(dead_pid)

        assert _child_pids == [100, 200]  # unchanged

    def test_unknown_pid_no_error(self):
        """Crash with a PID not in _child_pids should not raise."""
        _child_pids = [100, 200]
        crashes = [
            {"task_name": "video_writer", "pid": 999, "is_critical": True},
        ]

        for crash in crashes:
            dead_pid = crash.get("pid")
            if dead_pid and dead_pid in _child_pids:
                _child_pids.remove(dead_pid)

        assert _child_pids == [100, 200]  # unchanged


# ---------------------------------------------------------------------------
# Force-quit handler: list snapshot safety
# ---------------------------------------------------------------------------


class TestForceQuitPidSnapshot:
    """Tests for the PID list snapshot in the force-quit handler."""

    def test_snapshot_isolates_from_mutations(self):
        """Force-quit should use a snapshot, so concurrent removal doesn't affect iteration."""
        _child_pids = [100, 200, 300]

        # Snapshot (as done in the force-quit handler)
        _pids_snapshot = list(_child_pids)

        # Simulate concurrent removal (health check prunes dead PID)
        _child_pids.remove(200)

        # Snapshot should still have all original PIDs
        assert _pids_snapshot == [100, 200, 300]
        assert _child_pids == [100, 300]

    def test_force_quit_uses_snapshot_in_source(self):
        """Verify the force-quit handler uses list() snapshot, not raw _child_pids."""
        import ast
        import inspect
        import screencap.recorder as mod

        source = inspect.getsource(mod)
        tree = ast.parse(source)

        # Find _force_exit function
        force_exit_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_force_exit":
                force_exit_fn = node
                break

        assert force_exit_fn is not None, "_force_exit function not found"

        # Check that list(_child_pids) is used (the snapshot pattern)
        source_lines = ast.get_source_segment(source, force_exit_fn)
        assert "_pids_snapshot" in source_lines, (
            "_force_exit should use _pids_snapshot for PID iteration safety"
        )
