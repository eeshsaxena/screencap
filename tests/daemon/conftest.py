"""Shared daemon test helpers."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def daemon_socket_path(tmp_path: Path) -> Path:
    return short_socket_path(tmp_path)


def short_socket_path(tmp_path: Path) -> Path:
    """Return a short UDS path whose backing directory is still test-owned."""
    link = Path("/tmp") / f"sc-{uuid.uuid4().hex[:12]}"
    link.symlink_to(tmp_path, target_is_directory=True)
    atexit.register(lambda: link.unlink(missing_ok=True))
    return link / "run" / "api.sock"


@pytest.fixture
def daemon_env() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
    }


def wait_for_socket(path: Path, proc: subprocess.Popen[bytes] | None = None, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            pytest.fail(
                f"daemon exited before socket appeared: {proc.returncode}\n"
                f"stdout:\n{stdout.decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}"
            )
        time.sleep(0.025)
    pytest.fail(f"socket did not appear within {timeout}s: {path}")


@pytest.fixture
def serve_process(daemon_env: dict[str, str], daemon_socket_path: Path) -> Iterator[subprocess.Popen[bytes]]:
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
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    wait_for_socket(daemon_socket_path, proc=proc)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def uds_client_factory(daemon_socket_path: Path) -> Callable[[], httpx.AsyncClient]:
    def factory() -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=str(daemon_socket_path))
        return httpx.AsyncClient(transport=transport, base_url="http://screencap")

    return factory
