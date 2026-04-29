"""Tests for src/screencap/network/lifecycle.py."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from screencap.network.lifecycle import (
    NetworkAlreadyActiveError,
    NoFreePortError,
    acquire_network_lock,
    auto_negotiate_port,
    delete_network_child_handoff,
    delete_sentinel,
    is_pid_alive_with_create_time,
    read_network_child_handoff,
    read_sentinel,
    restore_orphaned_proxy_state,
    write_network_child_handoff,
    write_sentinel,
)
from screencap.network.system_proxy import ServiceProxyState, write_snapshot


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Port auto-negotiation
# ---------------------------------------------------------------------------


class TestAutoNegotiatePort:
    def test_returns_first_free_port(self):
        # The 8080-8090 range is unlikely to be entirely busy on dev machines.
        port = auto_negotiate_port(8080, 8090)
        assert 8080 <= port <= 8090

    def test_skips_busy_first_port(self):
        # Bind 8080 and verify 8081 is selected.
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))  # ephemeral
        ephemeral_port = sock.getsockname()[1]
        try:
            port = auto_negotiate_port(ephemeral_port, ephemeral_port + 5)
            assert port != ephemeral_port
            assert ephemeral_port < port <= ephemeral_port + 5
        finally:
            sock.close()

    def test_raises_when_all_busy(self):
        # Bind a small range of ports and assert NoFreePortError.
        socks: list[socket.socket] = []
        try:
            base_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            base_sock.bind(("127.0.0.1", 0))
            base_port = base_sock.getsockname()[1]
            socks.append(base_sock)
            # Bind base+1 and base+2
            for offset in (1, 2):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    s.bind(("127.0.0.1", base_port + offset))
                    socks.append(s)
                except OSError:
                    s.close()
                    pytest.skip("could not bind contiguous range; environment-dependent")
            with pytest.raises(NoFreePortError, match="all proxy ports busy"):
                auto_negotiate_port(base_port, base_port + 2)
        finally:
            for s in socks:
                s.close()


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


class TestAcquireNetworkLock:
    def test_acquires_when_free(self, tmp_path: Path):
        lock_path = tmp_path / ".network_active.lock"
        handle = acquire_network_lock(lock_path)
        try:
            assert handle.path == lock_path
            assert lock_path.exists()
        finally:
            handle.release()

    def test_second_acquire_raises(self, tmp_path: Path):
        lock_path = tmp_path / ".network_active.lock"
        handle = acquire_network_lock(lock_path)
        try:
            with pytest.raises(NetworkAlreadyActiveError):
                acquire_network_lock(lock_path)
        finally:
            handle.release()

    def test_release_then_reacquire(self, tmp_path: Path):
        lock_path = tmp_path / ".network_active.lock"
        h1 = acquire_network_lock(lock_path)
        h1.release()
        h2 = acquire_network_lock(lock_path)
        h2.release()


# ---------------------------------------------------------------------------
# Sentinel + handoff round-trip
# ---------------------------------------------------------------------------


class TestSentinelRoundTrip:
    def test_write_read(self, tmp_path: Path):
        sentinel_path = tmp_path / ".network_active"
        write_sentinel(
            worker_pid=12345,
            worker_create_time=1000.0,
            worker_cmdline_tail="screencap recorder",
            proxy_pid=12346,
            proxy_create_time=1001.0,
            proxy_cmdline_tail="proxy_runner",
            started_at=1002.0,
            port=8080,
            recording_dir="/tmp/rec",
            snapshot_path="/tmp/rec/.proxy_state.json",
            sentinel_path=sentinel_path,
        )
        data = read_sentinel(sentinel_path)
        assert data["worker_pid"] == 12345
        assert data["proxy_pid"] == 12346
        assert data["port"] == 8080

    def test_read_missing_returns_none(self, tmp_path: Path):
        assert read_sentinel(tmp_path / "missing") is None

    def test_read_corrupted_returns_none(self, tmp_path: Path):
        path = tmp_path / "sentinel"
        path.write_text("{ not valid json")
        assert read_sentinel(path) is None

    def test_delete_idempotent(self, tmp_path: Path):
        path = tmp_path / "sentinel"
        delete_sentinel(path)  # no-op
        path.write_text("{}")
        delete_sentinel(path)
        assert not path.exists()


class TestHandoffRoundTrip:
    def test_write_read(self, tmp_path: Path):
        write_network_child_handoff(
            tmp_path,
            proxy_pid=12345,
            worker_pid=12344,
            started_at=1000.0,
        )
        data = read_network_child_handoff(tmp_path)
        assert data["proxy_pid"] == 12345
        assert data["worker_pid"] == 12344

    def test_read_missing_returns_none(self, tmp_path: Path):
        assert read_network_child_handoff(tmp_path) is None

    def test_read_corrupted_returns_none(self, tmp_path: Path):
        path = tmp_path / ".network_child.json"
        path.write_text("not json")
        assert read_network_child_handoff(tmp_path) is None

    def test_low_pids_treated_as_absent(self, tmp_path: Path):
        # PID < 100 is suspicious (partial-int truncation) — treat as absent.
        path = tmp_path / ".network_child.json"
        path.write_text(json.dumps({"proxy_pid": 12, "worker_pid": 12345, "started_at": 1.0}))
        assert read_network_child_handoff(tmp_path) is None

        path.write_text(json.dumps({"proxy_pid": 12345, "worker_pid": 5, "started_at": 1.0}))
        assert read_network_child_handoff(tmp_path) is None

    def test_atomic_write(self, tmp_path: Path):
        write_network_child_handoff(tmp_path, proxy_pid=1234, worker_pid=1235, started_at=1.0)
        # No leftover .tmp
        assert not (tmp_path / ".network_child.json.tmp").exists()
        # Mode 600
        path = tmp_path / ".network_child.json"
        assert (path.stat().st_mode & 0o777) == 0o600


# ---------------------------------------------------------------------------
# PID-reuse defense
# ---------------------------------------------------------------------------


class TestPIDReuseDefense:
    def test_low_pid_returns_false(self):
        assert is_pid_alive_with_create_time(50) is False
        assert is_pid_alive_with_create_time(0) is False

    def test_self_pid_returns_true(self):
        # Our own PID is alive; without create_time/cmdline checks, return True.
        my_pid = os.getpid()
        assert is_pid_alive_with_create_time(my_pid) is True

    def test_self_pid_with_wrong_create_time_returns_false(self):
        my_pid = os.getpid()
        assert is_pid_alive_with_create_time(my_pid, expected_create_time=0.0) is False

    def test_self_pid_with_wrong_cmdline_tail_returns_false(self):
        my_pid = os.getpid()
        assert is_pid_alive_with_create_time(
            my_pid, expected_cmdline_tail="this-string-not-in-cmdline-xyz123"
        ) is False

    def test_dead_pid_returns_false(self):
        # 999999 is unlikely to be a live PID on a normal system.
        assert is_pid_alive_with_create_time(999999) is False


# ---------------------------------------------------------------------------
# restore_orphaned_proxy_state
# ---------------------------------------------------------------------------


class TestRestoreOrphanedProxyState:
    def test_no_op_when_no_orphans(self, tmp_path: Path):
        # No sentinel, no durable dir, no recordings
        result = restore_orphaned_proxy_state(
            sentinel_path=tmp_path / "sentinel-missing",
            durable_dir=tmp_path / "snapshots-missing",
            recordings_dirs=[],
        )
        assert result == []

    def test_restores_dead_worker_orphan(self, tmp_path: Path):
        # Set up a snapshot with a dead worker_pid.
        rec_dir = tmp_path / "rec-1"
        rec_dir.mkdir()
        snapshot_path = rec_dir / ".proxy_state.json"
        snapshot = {
            "Wi-Fi": ServiceProxyState(
                service_name="Wi-Fi",
                web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                secure_web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                bypass_domains=(),
            )
        }
        write_snapshot(
            snapshot,
            snapshot_path,
            extra={
                "worker_pid": 999999,  # unlikely to be alive
                "worker_create_time": 1.0,
                "worker_cmdline_tail": "non-matching",
                "recording_dir": str(rec_dir),
            },
        )
        sentinel_path = tmp_path / "sentinel"
        durable_dir = tmp_path / "snapshots"
        durable_dir.mkdir()

        # Mock the osascript call so we don't actually prompt for admin.
        with patch("subprocess.run", return_value=_completed()) as run_mock:
            result = restore_orphaned_proxy_state(
                sentinel_path=sentinel_path,
                durable_dir=durable_dir,
                recordings_dirs=[tmp_path],
            )
        assert len(result) == 1
        assert result[0] == snapshot_path
        # Snapshot deleted post-restore.
        assert not snapshot_path.exists()
        # osascript was called with admin privileges.
        cmd = run_mock.call_args.args[0]
        assert cmd[0] == "/usr/bin/osascript"
        assert "with administrator privileges" in cmd[2]

    def test_skips_alive_worker(self, tmp_path: Path):
        rec_dir = tmp_path / "rec-2"
        rec_dir.mkdir()
        snapshot_path = rec_dir / ".proxy_state.json"
        snapshot = {"Wi-Fi": ServiceProxyState(service_name="Wi-Fi")}
        # Use OUR pid as the worker_pid — alive and matches our cmdline.
        import psutil

        my_pid = os.getpid()
        my_create = psutil.Process(my_pid).create_time()
        my_cmdline_tail = " ".join(psutil.Process(my_pid).cmdline()[-3:])
        write_snapshot(
            snapshot,
            snapshot_path,
            extra={
                "worker_pid": my_pid,
                "worker_create_time": my_create,
                "worker_cmdline_tail": my_cmdline_tail,
                "recording_dir": str(rec_dir),
            },
        )

        with patch("subprocess.run", return_value=_completed()) as run_mock:
            result = restore_orphaned_proxy_state(
                sentinel_path=tmp_path / "sentinel-missing",
                durable_dir=tmp_path / "snapshots-missing",
                recordings_dirs=[tmp_path],
            )
        # Snapshot still present (live worker — skipped).
        assert result == []
        assert snapshot_path.exists()
        # No osascript admin call.
        run_mock.assert_not_called()

    def test_handles_corrupted_snapshot(self, tmp_path: Path):
        rec_dir = tmp_path / "rec-3"
        rec_dir.mkdir()
        snapshot_path = rec_dir / ".proxy_state.json"
        snapshot_path.write_text("{ corrupted json")

        with patch("subprocess.run", return_value=_completed()) as run_mock:
            result = restore_orphaned_proxy_state(
                sentinel_path=tmp_path / "sentinel-missing",
                durable_dir=tmp_path / "snapshots-missing",
                recordings_dirs=[tmp_path],
            )
        assert result == []
        # Skipped, no osascript admin call.
        run_mock.assert_not_called()

    def test_subprocess_timeout_skips_orphan(self, tmp_path: Path):
        rec_dir = tmp_path / "rec-4"
        rec_dir.mkdir()
        snapshot_path = rec_dir / ".proxy_state.json"
        snapshot = {
            "Wi-Fi": ServiceProxyState(
                service_name="Wi-Fi",
                web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                secure_web_proxy={"Enabled": "No", "Server": "", "Port": 0},
                bypass_domains=(),
            )
        }
        write_snapshot(
            snapshot,
            snapshot_path,
            extra={
                "worker_pid": 999999,
                "worker_create_time": 1.0,
                "worker_cmdline_tail": "x",
                "recording_dir": str(rec_dir),
            },
        )

        # Simulate osascript hanging.
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=30)

        with patch("subprocess.run", side_effect=fake_run):
            result = restore_orphaned_proxy_state(
                sentinel_path=tmp_path / "sentinel-missing",
                durable_dir=tmp_path / "snapshots-missing",
                recordings_dirs=[tmp_path],
            )
        # Timed out — skipped, snapshot preserved (so a future retry can still run).
        assert result == []
        assert snapshot_path.exists()
