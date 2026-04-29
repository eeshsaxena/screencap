"""Tests for screencap.pidfile — PID file lifecycle and orphan detection."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from screencap import pidfile


@pytest.fixture(autouse=True)
def _isolate_pidfile(tmp_path, monkeypatch):
    """Redirect PID_FILE to tmp_path so tests don't touch real ~/.screencap/."""
    test_pid_file = tmp_path / "recording.pid"
    monkeypatch.setattr(pidfile, "PID_FILE", test_pid_file)


class TestWriteAndRead:
    def test_write_then_read(self, tmp_path):
        children = [{"pid": 123, "name": "writer_a"}, {"pid": 456, "name": "writer_b"}]
        pidfile.write_pidfile(tmp_path / "capture", children)

        data = pidfile.read_pidfile()
        assert data is not None
        assert data["parent_pid"] == os.getpid()
        assert len(data["children"]) == 2
        assert data["children"][0]["pid"] == 123
        assert data["capture_dir"] == str(tmp_path / "capture")
        assert "started_at" in data

    def test_read_missing_returns_none(self):
        assert pidfile.read_pidfile() is None

    def test_read_corrupt_returns_none(self, tmp_path):
        pidfile.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        pidfile.PID_FILE.write_text("not json{{{")
        assert pidfile.read_pidfile() is None


class TestDeletePidfile:
    def test_delete_existing(self, tmp_path):
        pidfile.write_pidfile(tmp_path / "cap", [])
        assert pidfile.PID_FILE.exists()
        pidfile.delete_pidfile()
        assert not pidfile.PID_FILE.exists()

    def test_delete_missing_is_noop(self):
        pidfile.delete_pidfile()  # Should not raise


class TestIsScreencapProcess:
    def test_matching_process(self):
        mock_proc = mock.MagicMock()
        mock_proc.cmdline.return_value = ["python", "-m", "screencap.engine.recorder"]
        with mock.patch("psutil.Process", return_value=mock_proc):
            assert pidfile._is_screencap_process(123) is True

    def test_non_matching_process(self):
        mock_proc = mock.MagicMock()
        mock_proc.cmdline.return_value = ["vim", "somefile.py"]
        with mock.patch("psutil.Process", return_value=mock_proc):
            assert pidfile._is_screencap_process(123) is False

    def test_no_such_process(self):
        import psutil

        with mock.patch("psutil.Process", side_effect=psutil.NoSuchProcess(123)):
            assert pidfile._is_screencap_process(123) is False


class TestFindOrphanedProcesses:
    def test_no_pidfile_no_orphans(self):
        with mock.patch("psutil.process_iter", return_value=[]):
            assert pidfile.find_orphaned_processes() == []

    def test_stale_pidfile_with_dead_children(self, tmp_path):
        pidfile.write_pidfile(tmp_path / "cap", [{"pid": 99999, "name": "writer"}])
        with mock.patch.object(pidfile, "_pid_exists", return_value=False):
            orphans = pidfile.find_orphaned_processes()
        assert orphans == []

    def test_pidfile_with_live_orphans(self, tmp_path):
        pidfile.write_pidfile(tmp_path / "cap", [{"pid": 12345, "name": "screen_writer"}])

        def mock_pid_exists(pid):
            return pid == 12345  # child alive, parent dead

        with (
            mock.patch.object(pidfile, "_pid_exists", side_effect=mock_pid_exists),
            mock.patch.object(pidfile, "_is_screencap_process", return_value=True),
        ):
            orphans = pidfile.find_orphaned_processes()

        assert len(orphans) == 1
        assert orphans[0]["pid"] == 12345

    def test_live_parent_means_no_orphans(self, tmp_path):
        pidfile.write_pidfile(tmp_path / "cap", [{"pid": 12345, "name": "screen_writer"}])

        with (
            mock.patch.object(pidfile, "_pid_exists", return_value=True),
            mock.patch.object(pidfile, "_is_screencap_process", return_value=True),
        ):
            orphans = pidfile.find_orphaned_processes()

        assert orphans == []


class TestTerminateProcesses:
    def test_terminate_with_sigterm(self):
        mock_proc = mock.MagicMock()

        with (
            mock.patch.object(pidfile, "_pid_exists", return_value=True),
            mock.patch.object(pidfile, "_is_screencap_process", return_value=True),
            mock.patch("psutil.Process", return_value=mock_proc),
            mock.patch("psutil.wait_procs", return_value=([], [])),
        ):
            result = pidfile.terminate_processes([{"pid": 100, "name": "writer"}])

        assert len(result) == 1
        mock_proc.terminate.assert_called_once()

    def test_terminate_with_force(self):
        mock_proc = mock.MagicMock()

        with (
            mock.patch.object(pidfile, "_pid_exists", return_value=True),
            mock.patch.object(pidfile, "_is_screencap_process", return_value=True),
            mock.patch("psutil.Process", return_value=mock_proc),
        ):
            result = pidfile.terminate_processes([{"pid": 100, "name": "writer"}], force=True)

        assert len(result) == 1
        mock_proc.kill.assert_called_once()

    def test_skip_non_screencap_process(self):
        with (
            mock.patch.object(pidfile, "_pid_exists", return_value=True),
            mock.patch.object(pidfile, "_is_screencap_process", return_value=False),
        ):
            result = pidfile.terminate_processes([{"pid": 100, "name": "writer"}])

        assert result == []


# ---------------------------------------------------------------------------
# Network proxy child support: cmdline-allowlist widening + add_child/remove_child
# ---------------------------------------------------------------------------


class TestIsScreencapProcessWithAllowlist:
    """The cmdline filter must accept names supplied via name_allowlist.

    macOS spawn-mode children's cmdline is the Python interpreter path +
    multiprocessing bootstrap args -- NO "screencap" substring. Without the
    allowlist widening, terminate_processes would skip the mitmproxy child.
    """

    def test_screencap_in_cmdline_still_matches(self):
        proc = mock.MagicMock()
        proc.cmdline.return_value = ["python", "-m", "screencap", "start"]
        with mock.patch("psutil.Process", return_value=proc):
            assert pidfile._is_screencap_process(123) is True

    def test_no_match_without_allowlist(self):
        proc = mock.MagicMock()
        proc.cmdline.return_value = [
            "/usr/bin/python3.12",
            "-c",
            "from multiprocessing.spawn import spawn_main; spawn_main(...)",
        ]
        with mock.patch("psutil.Process", return_value=proc):
            assert pidfile._is_screencap_process(123) is False

    def test_match_via_allowlist_substring(self):
        proc = mock.MagicMock()
        proc.cmdline.return_value = [
            "/usr/bin/python3.12",
            "-c",
            "import mitmproxy; mitmproxy.run()",
        ]
        with mock.patch("psutil.Process", return_value=proc):
            # Without allowlist: not matched (no "screencap" in cmdline).
            assert pidfile._is_screencap_process(123) is False
            # With allowlist: matched via "mitmproxy".
            assert pidfile._is_screencap_process(123, {"mitmproxy"}) is True

    def test_allowlist_match_is_case_insensitive(self):
        proc = mock.MagicMock()
        proc.cmdline.return_value = ["python", "MITMPROXY-CLI"]
        with mock.patch("psutil.Process", return_value=proc):
            assert pidfile._is_screencap_process(123, {"mitmproxy"}) is True


class TestAddRemoveChild:
    def test_add_child_to_existing_pidfile(self, tmp_path):
        # Set up a pidfile with one child.
        with mock.patch.object(pidfile, "PID_FILE", tmp_path / "recording.pid"):
            pidfile.write_pidfile(
                tmp_path,
                child_pids=[{"pid": 100, "name": "writer"}],
            )
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=12345,
                worker_pid=12344,
                proxy_create_time=1000.0,
                proxy_cmdline_tail="proxy_runner",
            )
            data = pidfile.read_pidfile()
        assert data is not None
        names = [c.get("name") for c in data["children"]]
        assert "writer" in names
        assert "mitmproxy" in names
        mitm = next(c for c in data["children"] if c["name"] == "mitmproxy")
        assert mitm["pid"] == 12345
        assert mitm["worker_pid"] == 12344
        assert mitm["create_time"] == 1000.0
        assert mitm["cmdline_tail"] == "proxy_runner"

    def test_add_child_creates_pidfile_if_absent(self, tmp_path):
        with mock.patch.object(pidfile, "PID_FILE", tmp_path / "recording.pid"):
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=12345,
                worker_pid=12344,
                proxy_create_time=1000.0,
                proxy_cmdline_tail="proxy_runner",
            )
            data = pidfile.read_pidfile()
        assert data is not None
        assert any(c.get("name") == "mitmproxy" for c in data["children"])

    def test_add_child_replaces_duplicate_name(self, tmp_path):
        with mock.patch.object(pidfile, "PID_FILE", tmp_path / "recording.pid"):
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=100,
                worker_pid=99,
                proxy_create_time=1.0,
                proxy_cmdline_tail="old",
            )
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=200,
                worker_pid=199,
                proxy_create_time=2.0,
                proxy_cmdline_tail="new",
            )
            data = pidfile.read_pidfile()
        children = [c for c in data["children"] if c.get("name") == "mitmproxy"]
        assert len(children) == 1
        assert children[0]["pid"] == 200
        assert children[0]["cmdline_tail"] == "new"

    def test_remove_child_by_name(self, tmp_path):
        with mock.patch.object(pidfile, "PID_FILE", tmp_path / "recording.pid"):
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=100,
                worker_pid=99,
                proxy_create_time=1.0,
                proxy_cmdline_tail="x",
            )
            pidfile.remove_child("mitmproxy")
            data = pidfile.read_pidfile()
        assert all(c.get("name") != "mitmproxy" for c in data.get("children", []))

    def test_remove_child_no_op_if_missing(self, tmp_path):
        with mock.patch.object(pidfile, "PID_FILE", tmp_path / "recording.pid"):
            # No pidfile at all
            pidfile.remove_child("mitmproxy")  # should not raise
            # Now write one without the target child
            pidfile.write_pidfile(tmp_path, child_pids=[{"pid": 100, "name": "writer"}])
            pidfile.remove_child("mitmproxy")  # still a no-op
            data = pidfile.read_pidfile()
        assert any(c.get("name") == "writer" for c in data["children"])

    def test_atomic_write(self, tmp_path):
        pid_file = tmp_path / "recording.pid"
        with mock.patch.object(pidfile, "PID_FILE", pid_file):
            pidfile.add_child(
                "mitmproxy",
                proxy_pid=100,
                worker_pid=99,
                proxy_create_time=1.0,
                proxy_cmdline_tail="x",
            )
        # No leftover .tmp
        assert not pid_file.with_suffix(pid_file.suffix + ".tmp").exists()
        assert pid_file.exists()
