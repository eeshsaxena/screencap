"""CLI coverage for ``screencap serve``."""

from __future__ import annotations

import os
import re
import signal
import socket as stdlib_socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.daemon.conftest import short_socket_path, wait_for_socket


@pytest.fixture
def cli_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }


def _cli_command(*args: str) -> list[str]:
    return [sys.executable, "-m", "screencap.cli", "--no-update-check", *args]


def test_serve_self_test_exits_zero_within_5s(cli_env: dict[str, str]) -> None:
    start = time.perf_counter()
    proc = subprocess.run(
        _cli_command("serve", "--self-test"),
        env=cli_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    elapsed = time.perf_counter() - start

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 5


def test_help_lists_serve_and_stays_responsive(cli_env: dict[str, str]) -> None:
    start = time.perf_counter()
    proc = subprocess.run(
        _cli_command("--help"),
        env=cli_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    elapsed = time.perf_counter() - start

    assert proc.returncode == 0
    assert "serve" in proc.stdout
    assert elapsed < 0.75


def test_second_serve_against_same_socket_exits_nonzero(
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    socket_path = short_socket_path(tmp_path)
    first = subprocess.Popen(
        _cli_command("serve", "--socket", str(socket_path)),
        env=cli_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_socket(socket_path, proc=first)
    try:
        second = subprocess.run(
            _cli_command("serve", "--socket", str(socket_path)),
            env=cli_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        assert second.returncode != 0
        assert "another daemon is running" in second.stderr.lower()
    finally:
        if first.poll() is None:
            first.send_signal(signal.SIGTERM)
            first.wait(timeout=5)


def test_serve_against_pre_bound_socket_exits_75_and_logs_pid(
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A rogue same-EUID listener on the socket → daemon exits with EX_TEMPFAIL=75
    and logs the offending PID via lsof (TKT-C AE-C items 1 + 2)."""
    socket_path = short_socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    rogue = stdlib_socket.socket(stdlib_socket.AF_UNIX, stdlib_socket.SOCK_STREAM)
    rogue.bind(str(socket_path))
    rogue.listen(1)
    try:
        result = subprocess.run(
            _cli_command("serve", "--socket", str(socket_path)),
            env=cli_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    finally:
        rogue.close()
        socket_path.unlink(missing_ok=True)

    assert result.returncode == 75, (
        f"expected EX_TEMPFAIL=75; got {result.returncode}\nstderr:\n{result.stderr}"
    )
    # The PID line is "daemon socket already bound by pid=<int|unknown>".
    # On Linux without lsof installed the value falls back to "unknown"; on
    # macOS lsof should populate a real PID. Accept both shapes.
    assert re.search(r"daemon socket already bound by pid=(\d+|unknown)", result.stderr), (
        f"expected rogue-PID log line in stderr:\n{result.stderr}"
    )


def test_serve_against_perm_drifted_parent_dir_exits_75_with_drift_message(
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """If the socket parent dir is pre-created at a relaxed mode (e.g. 0o755),
    bind_unix_socket re-chmods to 0o700 but a verify failure after bind would
    raise SocketPermsDrift. To trigger the drift path deterministically without
    racing bind, pre-create the parent dir at 0o755 with a symlinked socket
    parent: bind_unix_socket calls os.chmod(parent, 0o700) which on a symlink
    target succeeds; the more reliable path is to inject the drift via a wrapper.
    The simplest end-to-end form: a parent dir already containing a stale
    surrogate that triggers the verify failure. Use a Python harness to force
    SocketPermsDrift, then assert the daemon's serve() loop exits EX_TEMPFAIL=75."""
    from screencap.daemon import socket as daemon_socket
    from screencap.daemon.server import EX_TEMPFAIL, serve

    socket_path = short_socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)

    original_ensure = daemon_socket._ensure_socket_directory

    def ensure_then_drift(path: Path) -> None:
        original_ensure(path)
        os.chmod(path.parent, 0o755)

    daemon_socket._ensure_socket_directory = ensure_then_drift
    try:
        rc = serve(socket_path)
    finally:
        daemon_socket._ensure_socket_directory = original_ensure
        socket_path.unlink(missing_ok=True)

    assert rc == EX_TEMPFAIL, f"expected EX_TEMPFAIL={EX_TEMPFAIL}, got {rc}"


def test_serve_against_rogue_file_at_socket_path_exits_1_not_75(
    cli_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """A non-socket file at the socket path must continue to exit with 1, not
    collapse into the EX_TEMPFAIL=75 already-running path (TKT-C regression
    guard: rogue-file and rogue-bind are distinct failure modes)."""
    socket_path = short_socket_path(tmp_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.write_text("rogue\n", encoding="utf-8")
    try:
        result = subprocess.run(
            _cli_command("serve", "--socket", str(socket_path)),
            env=cli_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    finally:
        # The rogue file must be left in place: bind must not have unlinked it.
        assert socket_path.exists()
        assert socket_path.read_text(encoding="utf-8") == "rogue\n"
        socket_path.unlink(missing_ok=True)

    assert result.returncode == 1, (
        f"rogue-file path must remain exit 1; got {result.returncode}\nstderr:\n{result.stderr}"
    )
