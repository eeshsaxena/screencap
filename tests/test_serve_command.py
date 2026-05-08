"""CLI coverage for ``screencap serve``."""

from __future__ import annotations

import os
import signal
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
