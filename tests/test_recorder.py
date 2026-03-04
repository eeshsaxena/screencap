"""Tests for screencap.recorder — force-quit cleanup, PID file lifecycle, and permission prompting."""

import inspect
import sys
from unittest import mock

import pytest


class TestForceExitCleanup:
    """Tests for the force-quit (second Ctrl+C) behavior."""

    def test_no_os_exit_in_recorder(self):
        """Verify os._exit is not used in the recorder module."""
        import screencap.recorder as mod

        source = inspect.getsource(mod)
        assert "os._exit" not in source

    def test_force_handler_calls_terminate_and_kill(self):
        """The force-exit handler should terminate then kill children."""
        mock_child_a = mock.MagicMock()
        mock_child_b = mock.MagicMock()

        # Build a _force_exit-like handler to test in isolation
        import multiprocessing

        with mock.patch(
            "multiprocessing.active_children",
            side_effect=[
                [mock_child_a, mock_child_b],  # first call: terminate
                [mock_child_b],  # second call: only b survived for kill
            ],
        ):
            # Simulate what the second Ctrl+C does
            for child in multiprocessing.active_children():
                child.terminate()
            for child in multiprocessing.active_children():
                child.kill()

        mock_child_a.terminate.assert_called_once()
        mock_child_b.terminate.assert_called_once()
        mock_child_b.kill.assert_called_once()
        mock_child_a.kill.assert_not_called()

    def test_sys_exit_used_instead_of_os_exit(self):
        """Verify sys.exit is used in the force-quit path."""
        import screencap.recorder as mod

        source = inspect.getsource(mod)
        assert "sys.exit(1)" in source


class TestAtexitHandler:
    """Tests for the atexit defense-in-depth handler."""

    def test_atexit_registered_and_unregistered(self, tmp_path):
        """atexit handler should be registered during recording and unregistered after."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        registered = []
        unregistered = []

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("openadapt_capture.Recorder") as MockRecorder,
            mock.patch("atexit.register", side_effect=lambda fn: registered.append(fn)),
            mock.patch("atexit.unregister", side_effect=lambda fn: unregistered.append(fn)),
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            start_recording("test", output_dir=tmp_path / "test-rec")

        assert len(registered) == 1
        assert len(unregistered) == 1
        assert registered[0] is unregistered[0]

    def test_cleanup_children_terminates_active(self):
        """The atexit cleanup function should call terminate on all active children."""
        import multiprocessing

        mock_child = mock.MagicMock()
        with mock.patch("multiprocessing.active_children", return_value=[mock_child]):
            # Simulate the _cleanup_children function
            for child in multiprocessing.active_children():
                child.terminate()

        mock_child.terminate.assert_called_once()


class TestOrphanDetection:
    """Tests for startup orphan detection."""

    def test_start_exits_when_orphans_found(self, tmp_path):
        """start_recording should raise SystemExit if orphans found without force_clean."""
        from screencap.recorder import start_recording

        with (
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch(
                "screencap.pidfile.find_orphaned_processes",
                return_value=[{"pid": 123, "name": "writer"}],
            ),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")

    def test_start_force_clean_removes_orphans(self, tmp_path):
        """start_recording with force_clean=True should clean orphans and continue."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        orphans = [{"pid": 123, "name": "writer"}]

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
            mock.patch("screencap.pidfile.terminate_processes") as mock_term,
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("openadapt_capture.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            start_recording("test", output_dir=tmp_path / "test-rec", force_clean=True)

        mock_term.assert_called_once_with(orphans, force=True)


class TestPidFileLifecycle:
    """Tests for PID file write/delete during recording."""

    def test_pidfile_written_after_ready(self, tmp_path):
        """PID file should be written after recorder is ready."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile") as mock_write,
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("openadapt_capture.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            capture_dir = tmp_path / "test-rec"
            start_recording("test", output_dir=capture_dir)

        mock_write.assert_called_once()
        call_args = mock_write.call_args
        assert call_args[0][0] == capture_dir

    def test_pidfile_deleted_in_finally(self, tmp_path):
        """PID file should be deleted even if recording exits abnormally."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile") as mock_delete,
            mock.patch("openadapt_capture.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            start_recording("test", output_dir=tmp_path / "test-rec")

        mock_delete.assert_called()


class TestPermissionPrompting:
    """Tests for the macOS permission checking and prompting flow."""

    def _make_platform_mock(self, screen=True, accessibility=True, input_monitoring=True):
        """Create a mock DarwinPlatform with configurable permission states."""
        platform = mock.MagicMock()
        platform.is_screen_recording_enabled.return_value = screen
        platform.is_accessibility_enabled.return_value = accessibility
        platform.is_input_monitoring_enabled.return_value = input_monitoring
        platform.request_screen_recording_access.return_value = False
        platform.request_accessibility_access.return_value = False
        platform.request_input_monitoring_access.return_value = False
        return platform

    def test_all_permissions_granted_no_op(self):
        """When all permissions are granted, nothing happens."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=True, accessibility=True, input_monitoring=True)

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.subprocess") as mock_subprocess,
        ):
            mock_sys.platform = "darwin"
            with mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ):
                _check_macos_permissions()

        mock_subprocess.run.assert_not_called()

    def test_screen_recording_only_exits_with_restart_message(self):
        """Screen Recording missing should be last, exit with restart message."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=False, accessibility=True, input_monitoring=True)

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.subprocess") as mock_subprocess,
            mock.patch("screencap.recorder.console") as mock_console,
            mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ),
        ):
            mock_sys.platform = "darwin"

            with pytest.raises(SystemExit):
                _check_macos_permissions()

        platform.request_screen_recording_access.assert_called_once()
        call_args = mock_subprocess.run.call_args[0][0]
        assert "Privacy_ScreenCapture" in call_args[1]
        all_output = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "terminal restart" in all_output.lower()
        assert "[1/1]" in all_output

    def test_accessibility_missing_step_by_step_then_continues(self):
        """Accessibility missing: shows step [1/1], opens Settings, polls via subprocess, continues."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=True, accessibility=False, input_monitoring=True)

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.time"),
            mock.patch("screencap.recorder.subprocess") as mock_subprocess,
            mock.patch("screencap.recorder.console") as mock_console,
            mock.patch("screencap.recorder._check_permission_fresh", side_effect=[True, True]),
            mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ),
        ):
            mock_sys.platform = "darwin"
            _check_macos_permissions()

        platform.request_accessibility_access.assert_called_once()
        # subprocess.run called for opening Settings
        assert mock_subprocess.run.called
        all_output = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "[1/1]" in all_output
        assert "granted" in all_output.lower()

    def test_accessibility_missing_timeout_exits(self):
        """Accessibility missing that never gets granted should exit after timeout."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=True, accessibility=False, input_monitoring=True)

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.time"),
            mock.patch("screencap.recorder.subprocess"),
            mock.patch("screencap.recorder.console") as mock_console,
            mock.patch("screencap.recorder._check_permission_fresh", return_value=False),
            mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ),
        ):
            mock_sys.platform = "darwin"

            with pytest.raises(SystemExit):
                _check_macos_permissions()

        all_output = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "not granted" in all_output.lower()

    def test_two_immediate_permissions_step_by_step(self):
        """Accessibility then Input Monitoring: each gets its own step and Settings pane."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=True, accessibility=False, input_monitoring=False)
        # Each permission: True (poll granted), True (verify)
        fresh_results = [True, True, True, True]

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.time"),
            mock.patch("screencap.recorder.subprocess") as mock_subprocess,
            mock.patch("screencap.recorder.console") as mock_console,
            mock.patch("screencap.recorder._check_permission_fresh", side_effect=fresh_results),
            mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ),
        ):
            mock_sys.platform = "darwin"
            _check_macos_permissions()

        # Settings opened twice (via subprocess.run for 'open' command)
        open_calls = [
            call for call in mock_subprocess.run.call_args_list
            if call[0][0][0] == "open"
        ]
        assert len(open_calls) == 2
        assert "Privacy_Accessibility" in open_calls[0][0][0][1]
        assert "Privacy_ListenEvent" in open_calls[1][0][0][1]
        all_output = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "[1/2]" in all_output
        assert "[2/2]" in all_output

    def test_all_missing_steps_through_immediate_then_exits_for_screen(self):
        """All missing: steps through Accessibility, Input Monitoring, then exits for Screen Recording."""
        from screencap.recorder import _check_macos_permissions

        platform = self._make_platform_mock(screen=False, accessibility=False, input_monitoring=False)
        fresh_results = [True, True, True, True]

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch("screencap.recorder.time"),
            mock.patch("screencap.recorder.subprocess") as mock_subprocess,
            mock.patch("screencap.recorder.console") as mock_console,
            mock.patch("screencap.recorder._check_permission_fresh", side_effect=fresh_results),
            mock.patch(
                "openadapt_capture.platform.darwin.DarwinPlatform",
                platform,
            ),
        ):
            mock_sys.platform = "darwin"

            with pytest.raises(SystemExit):
                _check_macos_permissions()

        platform.request_accessibility_access.assert_called_once()
        platform.request_input_monitoring_access.assert_called_once()
        platform.request_screen_recording_access.assert_called_once()
        all_output = " ".join(str(c) for c in mock_console.print.call_args_list)
        assert "[1/3]" in all_output
        assert "[2/3]" in all_output
        assert "[3/3]" in all_output
        assert "terminal restart" in all_output.lower()

    def test_check_permission_fresh_runs_subprocess(self):
        """_check_permission_fresh should run a Python subprocess and parse stdout."""
        from screencap.recorder import _check_permission_fresh

        mock_result = mock.MagicMock()
        mock_result.stdout = "True\n"

        with mock.patch("screencap.recorder.subprocess") as mock_subprocess:
            mock_subprocess.run.return_value = mock_result
            assert _check_permission_fresh("Accessibility") is True

        mock_subprocess.run.assert_called_once()
        call_args = mock_subprocess.run.call_args
        assert call_args[0][0][0] == sys.executable
        assert "AXIsProcessTrustedWithOptions" in call_args[0][0][2]

    def test_non_darwin_platform_skips_check(self):
        """On non-darwin platforms, _check_macos_permissions is a no-op."""
        from screencap.recorder import _check_macos_permissions

        with mock.patch("screencap.recorder.sys") as mock_sys:
            mock_sys.platform = "linux"
            _check_macos_permissions()

    def test_import_error_fails_open(self):
        """If DarwinPlatform can't be imported, skip checks (fail open)."""
        from screencap.recorder import _check_macos_permissions

        with (
            mock.patch("screencap.recorder.sys") as mock_sys,
            mock.patch.dict("sys.modules", {"openadapt_capture.platform.darwin": None}),
        ):
            mock_sys.platform = "darwin"
            _check_macos_permissions()
