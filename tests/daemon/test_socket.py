"""AF_UNIX socket lifecycle tests for the daemon."""

from __future__ import annotations

import os
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_bind_creates_run_directory_with_strict_permissions(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import bind_unix_socket

    listener = bind_unix_socket(daemon_socket_path)
    try:
        assert daemon_socket_path.exists()
        assert stat.S_IMODE(daemon_socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(daemon_socket_path.stat().st_mode) == 0o600
        assert stat.S_ISSOCK(daemon_socket_path.stat().st_mode)
    finally:
        listener.close()
        daemon_socket_path.unlink(missing_ok=True)


def test_stale_socket_is_unlinked_and_rebound(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import bind_unix_socket

    daemon_socket_path.parent.mkdir(parents=True)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(daemon_socket_path))
    stale.close()

    listener = bind_unix_socket(daemon_socket_path)
    try:
        assert stat.S_ISSOCK(daemon_socket_path.stat().st_mode)
    finally:
        listener.close()
        daemon_socket_path.unlink(missing_ok=True)


def test_running_socket_raises_already_running(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import DaemonAlreadyRunning, bind_unix_socket

    daemon_socket_path.parent.mkdir(parents=True)
    running = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    running.bind(str(daemon_socket_path))
    running.listen(1)
    try:
        with pytest.raises(DaemonAlreadyRunning, match="another daemon is running"):
            bind_unix_socket(daemon_socket_path)
    finally:
        running.close()
        daemon_socket_path.unlink(missing_ok=True)


def test_rogue_regular_file_is_not_overwritten(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import RogueFileAtSocketPath, bind_unix_socket

    daemon_socket_path.parent.mkdir(parents=True)
    daemon_socket_path.write_text("keep me", encoding="utf-8")

    with pytest.raises(RogueFileAtSocketPath, match="rogue file"):
        bind_unix_socket(daemon_socket_path)

    assert daemon_socket_path.read_text(encoding="utf-8") == "keep me"


def test_peer_euid_check_is_invoked_and_rejects_mismatch(
    daemon_socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.daemon import socket as daemon_socket

    calls: list[int] = []

    def fake_peer_matches_current_euid(fd: int) -> bool:
        calls.append(fd)
        return False

    monkeypatch.setattr(daemon_socket, "peer_matches_current_euid", fake_peer_matches_current_euid)

    listener = daemon_socket.bind_unix_socket(daemon_socket_path)
    listener.setblocking(False)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(daemon_socket_path))
            with pytest.raises(BlockingIOError):
                listener.accept()
        finally:
            client.close()
    finally:
        listener.close()
        daemon_socket_path.unlink(missing_ok=True)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_serve_accepts_same_euid_and_returns_default_404(
    serve_process: subprocess.Popen[bytes],
    uds_client_factory,
) -> None:
    async with uds_client_factory() as client:
        response = await client.get("/unknown")
    assert response.status_code == 404
    assert serve_process.poll() is None


def test_sigterm_during_accept_loop_unlinks_socket(
    serve_process: subprocess.Popen[bytes],
    daemon_socket_path: Path,
) -> None:
    serve_process.send_signal(signal.SIGTERM)
    serve_process.wait(timeout=10)

    assert serve_process.returncode == 0
    assert not daemon_socket_path.exists()


def test_sigterm_immediately_after_process_start_is_honored(
    daemon_env: dict[str, str],
    daemon_socket_path: Path,
    tmp_path: Path,
) -> None:
    ready_marker = tmp_path / "signal-ready"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "screencap.cli",
            "--no-update-check",
            "serve",
            "--socket",
            str(daemon_socket_path),
        ],
        env={**daemon_env, "SCREENCAP_DAEMON_SIGNAL_READY_MARKER": str(ready_marker)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 5
    while not ready_marker.exists():
        if time.time() > deadline:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            pytest.fail(
                "daemon never installed its early signal handler\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            )
        time.sleep(0.01)
    os.kill(proc.pid, signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(
            "daemon did not honor immediate SIGTERM\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )

    assert proc.returncode == 0
    assert not daemon_socket_path.exists()
