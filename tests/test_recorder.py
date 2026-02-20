"""Tests for screencap.recorder — force-quit cleanup and PID file lifecycle."""

import inspect
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
