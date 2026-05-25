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


def test_running_socket_attaches_existing_pid_from_lsof(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import DaemonAlreadyRunning, bind_unix_socket

    daemon_socket_path.parent.mkdir(parents=True)
    running = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    running.bind(str(daemon_socket_path))
    running.listen(1)
    try:
        with pytest.raises(DaemonAlreadyRunning) as exc_info:
            bind_unix_socket(daemon_socket_path)
    finally:
        running.close()
        daemon_socket_path.unlink(missing_ok=True)

    # lsof may or may not return a PID depending on platform support; the
    # attribute must always exist and be int-or-None.
    assert hasattr(exc_info.value, "existing_pid")
    assert exc_info.value.existing_pid is None or isinstance(exc_info.value.existing_pid, int)


def test_running_socket_falls_back_when_lsof_missing(
    daemon_socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If lsof can't run, DaemonAlreadyRunning still raises with existing_pid=None."""
    from screencap.daemon import socket as daemon_socket

    def fake_capture(_path: Path) -> int | None:
        # Simulate FileNotFoundError / TimeoutExpired / any failure path.
        return None

    monkeypatch.setattr(daemon_socket, "_capture_socket_pid", fake_capture)

    daemon_socket_path.parent.mkdir(parents=True)
    running = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    running.bind(str(daemon_socket_path))
    running.listen(1)
    try:
        with pytest.raises(daemon_socket.DaemonAlreadyRunning) as exc_info:
            daemon_socket.bind_unix_socket(daemon_socket_path)
    finally:
        running.close()
        daemon_socket_path.unlink(missing_ok=True)

    assert exc_info.value.existing_pid is None


def test_rogue_regular_file_is_not_overwritten(daemon_socket_path: Path) -> None:
    from screencap.daemon.socket import RogueFileAtSocketPath, bind_unix_socket

    daemon_socket_path.parent.mkdir(parents=True)
    daemon_socket_path.write_text("keep me", encoding="utf-8")

    with pytest.raises(RogueFileAtSocketPath, match="rogue file"):
        bind_unix_socket(daemon_socket_path)

    assert daemon_socket_path.read_text(encoding="utf-8") == "keep me"


def test_bind_succeeds_when_perm_verification_matches(daemon_socket_path: Path) -> None:
    """Happy path: bind_unix_socket leaves both perms at expected modes and
    the new verify step accepts them."""
    from screencap.daemon.socket import bind_unix_socket

    listener = bind_unix_socket(daemon_socket_path)
    try:
        assert stat.S_IMODE(daemon_socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(daemon_socket_path.stat().st_mode) == 0o600
    finally:
        listener.close()
        daemon_socket_path.unlink(missing_ok=True)


def test_parent_dir_perm_drift_raises_and_cleans_up(
    daemon_socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the parent directory's mode drifts from 0o700 before bind verifies
    it, bind aborts with SocketPermsDrift and removes the half-bound socket.
    """
    from screencap.daemon import socket as daemon_socket

    original_ensure = daemon_socket._ensure_socket_directory

    def ensure_then_drift(path: Path) -> None:
        original_ensure(path)
        # Simulate umask drift / external chmod between _ensure_socket_directory
        # and the verify step.
        os.chmod(path.parent, 0o755)

    monkeypatch.setattr(daemon_socket, "_ensure_socket_directory", ensure_then_drift)

    with pytest.raises(daemon_socket.SocketPermsDrift, match=r"0o755"):
        daemon_socket.bind_unix_socket(daemon_socket_path)

    # The half-bound socket file must not survive a failed verify.
    assert not daemon_socket_path.exists()


def test_socket_file_perm_drift_raises_and_cleans_up(
    daemon_socket_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the socket-file mode lands at something other than 0o600 (e.g.,
    umask race against chmod), bind aborts with SocketPermsDrift."""
    import os as os_module

    from screencap.daemon import socket as daemon_socket

    original_chmod = os_module.chmod
    target = str(daemon_socket_path)

    def drift_socket_chmod(path, mode, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(path) == target and mode == 0o600:
            return original_chmod(path, 0o644, *args, **kwargs)
        return original_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os_module, "chmod", drift_socket_chmod)

    with pytest.raises(daemon_socket.SocketPermsDrift, match=r"0o644"):
        daemon_socket.bind_unix_socket(daemon_socket_path)

    assert not daemon_socket_path.exists()


def test_verify_socket_perms_translates_missing_paths_to_drift_error(
    tmp_path: Path,
) -> None:
    """The verify helper must surface a SocketPermsDrift (not bare
    FileNotFoundError) when stat fails — defends against the parent dir
    being cleaned out between bind and verify."""
    from screencap.daemon.socket import SocketPermsDrift, _verify_socket_perms

    nonexistent_socket = tmp_path / "missing-parent" / "api.sock"
    with pytest.raises(SocketPermsDrift):
        _verify_socket_perms(nonexistent_socket)


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
