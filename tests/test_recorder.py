"""Tests for screencap.recorder — force-quit cleanup, PID file lifecycle, permission prompting, disk checks."""

import ast
import inspect
import sys
from collections import namedtuple
from unittest import mock

import pytest


# Generous disk usage for tests that don't test disk checks
_PLENTY_OF_DISK = namedtuple("DiskUsage", ["total", "used", "free"])(
    total=500e9, used=100e9, free=400e9,
)


class TestForceExitCleanup:
    """Tests for the force-quit (second Ctrl+C) behavior."""

    def test_os_exit_only_in_force_exit(self):
        """Verify os._exit is used only inside _force_exit, nowhere else.

        os._exit is required in _force_exit to avoid threading._shutdown
        deadlock, but must not appear elsewhere (it skips finally blocks).
        """
        import screencap.recorder as mod

        source = inspect.getsource(mod)
        tree = ast.parse(source)

        def _walk_excluding(node, excluded_names):
            """Walk AST but skip FunctionDef nodes whose name is in excluded_names."""
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef) and child.name in excluded_names:
                    continue
                yield child
                yield from _walk_excluding(child, excluded_names)

        # Check every function EXCEPT _force_exit for os._exit calls
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name != "_force_exit":
                for child in _walk_excluding(node, {"_force_exit"}):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "_exit"
                        and isinstance(child.func.value, ast.Name)
                        and child.func.value.id == "os"
                    ):
                        raise AssertionError(
                            f"os._exit found in {node.name} — only allowed in _force_exit"
                        )

    def test_force_quit_cleans_up_pidfile(self):
        """The force-quit handler should call delete_pidfile before os._exit."""
        import screencap.recorder as mod

        source = inspect.getsource(mod)
        tree = ast.parse(source)

        force_exit_fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_force_exit":
                force_exit_fn = node
                break
        assert force_exit_fn is not None

        # Verify delete_pidfile is called inside _force_exit
        delete_calls = [
            node for node in ast.walk(force_exit_fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "delete_pidfile"
        ]
        assert len(delete_calls) >= 1, "_force_exit must call delete_pidfile"


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
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=orphans),
            mock.patch("screencap.pidfile.terminate_processes") as mock_term,
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("sc_engine.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            start_recording("test", output_dir=tmp_path / "test-rec", force_clean=True)

        mock_term.assert_called_once_with(orphans, force=True)


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
                "sc_engine.platform.darwin.DarwinPlatform",
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
                "sc_engine.platform.darwin.DarwinPlatform",
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
                "sc_engine.platform.darwin.DarwinPlatform",
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
                "sc_engine.platform.darwin.DarwinPlatform",
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
                "sc_engine.platform.darwin.DarwinPlatform",
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
                "sc_engine.platform.darwin.DarwinPlatform",
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
            mock.patch.dict("sys.modules", {"sc_engine.platform.darwin": None}),
        ):
            mock_sys.platform = "darwin"
            _check_macos_permissions()


# ---------------------------------------------------------------------------
# Disk space check tests
# ---------------------------------------------------------------------------

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


class TestPreRecordingDiskCheck:
    """Tests for the pre-recording disk space gate."""

    def test_sufficient_space_passes(self, tmp_path):
        """Recording starts normally when there is enough disk space."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        # 10 GB free — well above default 2000 MB warn threshold
        fake_usage = DiskUsage(total=100e9, used=90e9, free=10e9)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", return_value=fake_usage),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("sc_engine.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            capture_dir, elapsed = start_recording("test", output_dir=tmp_path / "test-rec")
            assert capture_dir.exists()

    def test_insufficient_space_aborts(self, tmp_path):
        """Recording refuses to start when free space is below warn threshold."""
        from screencap.recorder import start_recording

        # 500 MB free — below default 2000 MB warn threshold
        fake_usage = DiskUsage(total=100e9, used=99.5e9, free=500e6)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", return_value=fake_usage),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")

    def test_file_not_found_hard_error(self, tmp_path):
        """FileNotFoundError from disk_usage produces hard error."""
        from screencap.recorder import start_recording

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", side_effect=FileNotFoundError("not found")),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")

    def test_oserror_fails_open(self, tmp_path):
        """Other OSError from disk_usage is logged and recording proceeds."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", side_effect=OSError("FUSE error")),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("sc_engine.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            # Should not raise — fail-open behavior
            capture_dir, elapsed = start_recording("test", output_dir=tmp_path / "test-rec", verbose=True)
            assert capture_dir.exists()

    def test_warn_mb_zero_disables_check(self, tmp_path):
        """Setting warn_mb=0 disables the pre-recording disk check."""
        from screencap.recorder import start_recording

        mock_recorder = mock.MagicMock()
        mock_recorder.wait_for_ready.return_value = True
        mock_recorder.is_recording = False

        # Very low disk but check disabled
        fake_usage = DiskUsage(total=100e9, used=99.9e9, free=100e6)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=0),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=0),
            mock.patch("shutil.disk_usage", return_value=fake_usage),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("sc_engine.Recorder") as MockRecorder,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock(return_value=mock_recorder)
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            capture_dir, _ = start_recording("test", output_dir=tmp_path / "test-rec")
            assert capture_dir.exists()

    def test_check_runs_before_mkdir(self, tmp_path):
        """Disk check should run before capture_dir.mkdir() — no leftover dirs on failure."""
        from screencap.recorder import start_recording

        fake_usage = DiskUsage(total=100e9, used=99.5e9, free=500e6)
        capture_dir = tmp_path / "should-not-exist"

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=2000),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("shutil.disk_usage", return_value=fake_usage),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=capture_dir)

        # Directory should NOT have been created
        assert not capture_dir.exists()


class TestThresholdValidation:
    """Tests for warn/stop threshold ordering."""

    def test_stop_gte_warn_rejected(self, tmp_path):
        """stop_mb >= warn_mb (when both non-zero) should be rejected."""
        from screencap.recorder import start_recording

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=500),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=500),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")

    def test_stop_greater_than_warn_rejected(self, tmp_path):
        """stop_mb > warn_mb should be rejected."""
        from screencap.recorder import start_recording

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=500),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=1000),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")


class TestPrivacyFilterInitFailure:
    """Tests for privacy filter construction failure behavior.

    Public mode must hard-error (SystemExit) if the privacy filter can't
    be created — recording without it would expose sensitive data.
    Internal mode should warn and continue.
    """

    def test_public_mode_hard_errors_on_filter_failure(self, tmp_path):
        """Public mode raises SystemExit(1) when privacy filter fails."""
        from screencap.privacy.policy import PrivacyConfig, PrivacyMode
        from screencap.recorder import start_recording

        public_config = PrivacyConfig(mode=PrivacyMode.PUBLIC)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=0),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=0),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("screencap.config.get_privacy_config", return_value=public_config),
            mock.patch(
                "screencap.privacy.recorder_enforcement.RecorderPrivacyFilter",
                side_effect=RuntimeError("missing dep"),
            ),
            mock.patch("sc_engine.Recorder"),
        ):
            with pytest.raises(SystemExit):
                start_recording("test", output_dir=tmp_path / "test-rec")

    def test_internal_mode_exits_on_filter_failure(self, tmp_path):
        """Internal mode hard-fails when privacy config exists but filter init fails."""
        from screencap.privacy.policy import PrivacyConfig, PrivacyMode
        from screencap.recorder import start_recording

        internal_config = PrivacyConfig(mode=PrivacyMode.INTERNAL)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_audio_default", return_value=False),
            mock.patch("screencap.recorder.get_wifi_metrics", return_value=False),
            mock.patch("screencap.recorder.get_disk_warn_mb", return_value=0),
            mock.patch("screencap.recorder.get_disk_stop_mb", return_value=0),
            mock.patch("screencap.pidfile.find_orphaned_processes", return_value=[]),
            mock.patch("screencap.pidfile.write_pidfile"),
            mock.patch("screencap.pidfile.delete_pidfile"),
            mock.patch("screencap.config.get_privacy_config", return_value=internal_config),
            mock.patch(
                "screencap.privacy.recorder_enforcement.RecorderPrivacyFilter",
                side_effect=RuntimeError("missing dep"),
            ),
            mock.patch("sc_engine.Recorder") as MockRecorder,
            pytest.raises(SystemExit) as exc_info,
        ):
            MockRecorder.return_value.__enter__ = mock.MagicMock()
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)

            start_recording("test", output_dir=tmp_path / "test-rec")

        assert exc_info.value.code == 1


class TestSignalHandlerTiming:
    """Tests for signal handler installation order and safety guards."""

    def _get_start_recording_ast(self):
        """Parse the AST of start_recording()."""
        import screencap.recorder as mod

        source = inspect.getsource(mod.start_recording)
        return ast.parse(source)

    def _find_function(self, tree, name):
        """Find a FunctionDef by name in the AST."""
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        return None

    def test_signal_handler_installed_before_recorder_enter(self):
        """signal.signal(SIGINT/SIGTERM) must appear before Recorder() context entry.

        The handler must be active during Recorder.__enter__() and wait_for_ready()
        to prevent a window where signals are ignored or use default handlers.
        """
        tree = self._get_start_recording_ast()

        # Find signal.signal(signal.SIGINT, ...) calls
        sigint_lines = []
        recorder_lines = []

        for node in ast.walk(tree):
            # Match signal.signal(signal.SIGINT, ...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "signal"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "signal"
                and node.args
                and isinstance(node.args[0], ast.Attribute)
                and node.args[0].attr == "SIGINT"
            ):
                sigint_lines.append(node.lineno)

            # Match Recorder(...) as context manager (with statement)
            if isinstance(node, ast.With):
                for item in node.items:
                    call = item.context_expr
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "Recorder"
                    ):
                        recorder_lines.append(node.lineno)

        assert sigint_lines, "No signal.signal(SIGINT, ...) call found"
        assert recorder_lines, "No 'with Recorder(...)' found"

        first_sigint = min(sigint_lines)
        first_recorder = min(recorder_lines)
        assert first_sigint < first_recorder, (
            f"signal.signal(SIGINT) at line {first_sigint} must come before "
            f"Recorder() at line {first_recorder}"
        )

    def test_force_exit_guards_recorder_stop(self):
        """_force_exit must guard recorder.stop() with 'if recorder is not None'.

        When SIGINT arrives before Recorder.__enter__(), recorder is None.
        Calling recorder.stop() without a guard would crash with AttributeError.
        """
        tree = self._get_start_recording_ast()
        force_exit_fn = self._find_function(tree, "_force_exit")
        assert force_exit_fn is not None, "_force_exit not found"

        # Find all recorder.stop() calls
        stop_calls = []
        for node in ast.walk(force_exit_fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stop"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "recorder"
            ):
                stop_calls.append(node)

        assert stop_calls, "No recorder.stop() call found in _force_exit"

        # Each recorder.stop() should be inside an 'if recorder is not None'
        for stop_call in stop_calls:
            # Walk up to find parent If node
            found_guard = False
            for node in ast.walk(force_exit_fn):
                if isinstance(node, ast.If):
                    # Check for 'recorder is not None' pattern
                    test = node.test
                    if (
                        isinstance(test, ast.Compare)
                        and isinstance(test.left, ast.Name)
                        and test.left.id == "recorder"
                        and any(isinstance(op, ast.IsNot) for op in test.ops)
                    ):
                        # Check if stop_call is in this If's body
                        for child in ast.walk(node):
                            if child is stop_call:
                                found_guard = True
                                break
                if found_guard:
                    break
            assert found_guard, "recorder.stop() in _force_exit not guarded by 'if recorder is not None'"

    def test_force_exit_fallback_to_active_children(self):
        """Force-quit must fall back to multiprocessing.active_children() when _child_pids is empty."""
        tree = self._get_start_recording_ast()
        force_exit_fn = self._find_function(tree, "_force_exit")
        assert force_exit_fn is not None

        # Look for active_children() call in _force_exit
        found = False
        for node in ast.walk(force_exit_fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "active_children"
            ):
                found = True
                break

        assert found, (
            "_force_exit must call multiprocessing.active_children() "
            "as fallback when _child_pids is empty"
        )

    def test_sigterm_handler_guards_recorder_stop(self):
        """_sigterm_handler must guard recorder.stop() with 'if recorder is not None'."""
        tree = self._get_start_recording_ast()
        sigterm_fn = self._find_function(tree, "_sigterm_handler")
        assert sigterm_fn is not None, "_sigterm_handler not found"

        # Find recorder.stop() calls
        stop_calls = []
        for node in ast.walk(sigterm_fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "stop"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "recorder"
            ):
                stop_calls.append(node)

        assert stop_calls, "No recorder.stop() call found in _sigterm_handler"

        # Each should be guarded
        for stop_call in stop_calls:
            found_guard = False
            for node in ast.walk(sigterm_fn):
                if isinstance(node, ast.If):
                    test = node.test
                    if (
                        isinstance(test, ast.Compare)
                        and isinstance(test.left, ast.Name)
                        and test.left.id == "recorder"
                        and any(isinstance(op, ast.IsNot) for op in test.ops)
                    ):
                        for child in ast.walk(node):
                            if child is stop_call:
                                found_guard = True
                                break
                if found_guard:
                    break
            assert found_guard, "recorder.stop() in _sigterm_handler not guarded by 'if recorder is not None'"


class TestCloudIntentRecording:
    """Tests for cloud-intent recording behavior (Phases 3, 4)."""

    def test_cloud_intent_forces_public_mode(self, tmp_path):
        """Cloud-intent recording forces public privacy mode."""
        from screencap.recorder import start_recording
        from screencap.privacy.policy import PrivacyConfig, PrivacyMode

        internal_config = PrivacyConfig(mode=PrivacyMode.INTERNAL)

        captured_args = {}

        class FakeFilter:
            cloud_intent = True
            def __init__(self, config, **kwargs):
                captured_args["config"] = config
                captured_args["kwargs"] = kwargs

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_recordings_dir", return_value=tmp_path),
            mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
            mock.patch("screencap.config.get_privacy_config", return_value=internal_config),
            mock.patch(
                "screencap.privacy.recorder_enforcement.RecorderPrivacyFilter",
                side_effect=lambda config, **kw: FakeFilter(config, **kw),
            ),
            mock.patch("sc_engine.Recorder") as MockRecorder,
            mock.patch("sc_engine.config.config") as mock_engine_config,
        ):
            mock_engine_config.RECORD_WINDOW_DATA = True
            MockRecorder.return_value.__enter__ = mock.MagicMock()
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_recorder = MockRecorder.return_value.__enter__.return_value
            mock_recorder.is_recording = False
            mock_recorder.wait_for_ready = mock.MagicMock()

            try:
                start_recording("cloud-test", output_dir=tmp_path / "cloud-rec", cloud_intent=True)
            except (SystemExit, Exception):
                pass

        # The privacy config passed to the filter should have public mode
        assert captured_args["config"].mode == PrivacyMode.PUBLIC
        assert captured_args["kwargs"].get("cloud_intent") is True

    def test_cloud_intent_warning_printed(self, tmp_path, capsys):
        """Cloud-intent recording prints privacy warning."""
        from screencap.recorder import start_recording
        from screencap.privacy.policy import PrivacyConfig, PrivacyMode

        config = PrivacyConfig(mode=PrivacyMode.PUBLIC)

        with (
            mock.patch("screencap.recorder._check_macos_permissions"),
            mock.patch("screencap.recorder.get_recordings_dir", return_value=tmp_path),
            mock.patch("shutil.disk_usage", return_value=_PLENTY_OF_DISK),
            mock.patch("screencap.config.get_privacy_config", return_value=config),
            mock.patch("screencap.privacy.recorder_enforcement.RecorderPrivacyFilter") as MockFilter,
            mock.patch("sc_engine.Recorder") as MockRecorder,
            mock.patch("sc_engine.config.config") as mock_engine_config,
            mock.patch("screencap.recorder.console") as mock_console,
        ):
            mock_engine_config.RECORD_WINDOW_DATA = True
            mock_filter_instance = MockFilter.return_value
            MockRecorder.return_value.__enter__ = mock.MagicMock()
            MockRecorder.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_recorder = MockRecorder.return_value.__enter__.return_value
            mock_recorder.is_recording = False
            mock_recorder.wait_for_ready = mock.MagicMock()

            try:
                start_recording("cloud-test", output_dir=tmp_path / "cloud-rec", cloud_intent=True)
            except (SystemExit, Exception):
                pass

        # Check that the warning was printed
        print_calls = [str(c) for c in mock_console.print.call_args_list]
        warning_printed = any("Cloud Recording Privacy Notice" in str(c) for c in print_calls)
        assert warning_printed, f"Privacy warning not found in console output: {print_calls}"
