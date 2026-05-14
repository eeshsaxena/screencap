"""Tests for the CLI F3 auto-spawn fallback.

We isolate the autospawn decision flow by stubbing the four external
edges: socket reachability probes, ``launchctl print`` invocation,
``os.posix_spawn``, and the auto-serve log read. The OS surface is not
exercised; the daemon's own startup is covered by daemon-side tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from screencap.cli import _autospawn


@pytest.fixture
def isolated_log(tmp_path, monkeypatch):
    log_path = tmp_path / "auto-serve.log"
    monkeypatch.setattr(_autospawn, "_AUTO_LOG_PATH", log_path)
    return log_path


def test_returns_when_daemon_reachable(monkeypatch):
    monkeypatch.setattr(_autospawn, "_socket_reachable", lambda _p: True)
    monkeypatch.setattr(
        _autospawn,
        "_launchagent_installed",
        lambda: pytest.fail("should not probe launchctl when daemon reachable"),
    )

    _autospawn.ensure_daemon_or_spawn(socket_path=Path("/tmp/never-used.sock"))


def test_launchagent_installed_but_not_running_raises_kickstart(monkeypatch):
    monkeypatch.setattr(_autospawn, "_socket_reachable", lambda _p: False)
    monkeypatch.setattr(_autospawn, "_launchagent_installed", lambda: True)

    with pytest.raises(_autospawn.LaunchAgentNotRunningError) as exc:
        _autospawn.ensure_daemon_or_spawn(socket_path=Path("/tmp/never-used.sock"))
    assert "kickstart" in str(exc.value)
    assert "launchctl" in str(exc.value)


def test_no_launchagent_spawns_and_polls_ready(monkeypatch, isolated_log):
    spawned: dict = {}
    reachable_state = {"after_spawn": False}

    def fake_reachable(_p: Path) -> bool:
        return reachable_state["after_spawn"]

    def fake_spawn(path: str, argv, env, **kwargs):
        spawned["path"] = path
        spawned["argv"] = argv
        spawned["setsid"] = kwargs.get("setsid")
        reachable_state["after_spawn"] = True
        return 9999  # pretend-spawned PID

    monkeypatch.setattr(_autospawn, "_socket_reachable", fake_reachable)
    monkeypatch.setattr(_autospawn, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(os, "posix_spawn", fake_spawn)

    emitted: list[str] = []
    _autospawn.ensure_daemon_or_spawn(
        socket_path=Path("/tmp/never-used.sock"),
        readiness_timeout_s=1.0,
        stderr_emitter=emitted.append,
    )

    assert "serve" in spawned["argv"]
    assert any("--idle-shutdown=" in arg for arg in spawned["argv"])
    # ``setsid`` is the detachment guarantee from the auto-spawn plan;
    # asserting it pins the contract so a regression that drops it
    # cannot land silently.
    assert spawned["setsid"] is True
    # The spawn binary MUST be an absolute path so a hostile PATH cannot
    # inject a sibling that inherits TCC grants. Pin this explicitly.
    assert os.path.isabs(spawned["path"])
    assert emitted == ["Starting ScreenCap daemon...", "Daemon ready."]


def test_readiness_timeout_kills_pid_and_surfaces_log_tail(monkeypatch, isolated_log):
    killed: dict = {}

    def fake_spawn(path: str, argv, env, **kwargs):
        return 12345

    def fake_kill(pid: int, sig: int) -> None:
        killed["pid"] = pid
        killed["sig"] = sig

    isolated_log.parent.mkdir(parents=True, exist_ok=True)
    isolated_log.write_text("oops 1\noops 2\noops 3\n", encoding="utf-8")

    monkeypatch.setattr(_autospawn, "_socket_reachable", lambda _p: False)
    monkeypatch.setattr(_autospawn, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(os, "posix_spawn", fake_spawn)
    monkeypatch.setattr(os, "kill", fake_kill)
    monkeypatch.setattr(os, "waitpid", lambda pid, opts: (0, 0))

    with pytest.raises(_autospawn.DaemonAutoSpawnError) as exc_info:
        _autospawn.ensure_daemon_or_spawn(
            socket_path=Path("/tmp/never-used.sock"),
            readiness_timeout_s=0.1,
        )
    assert "oops" in (exc_info.value.log_tail or "")
    assert killed["pid"] == 12345


def test_spawn_race_post_check_recovers_without_second_spawn(monkeypatch, isolated_log):
    """First spawn's poll loop times out; before retrying, the post-spawn
    reachability check finds the winner of a parallel race already
    serving, and ensure_daemon_or_spawn returns without spawning twice.
    """
    spawn_calls = {"count": 0}

    def fake_spawn(path: str, argv, env, **kwargs):
        spawn_calls["count"] += 1
        return 11111

    # Poll loop sees reachable=False (deadline expires after ~0.1s);
    # the post-loop reachability re-check then sees the winning
    # daemon and we return early without a retry spawn.
    call_state = {"calls": 0}

    def fake_reachable(_p: Path) -> bool:
        call_state["calls"] += 1
        # Pre-spawn check (call 1): False (drives into the spawn branch).
        # During _poll_socket_ready (calls 2-N): False (deadline expires).
        # Post-loop re-check after _kill_pid_if_alive: True (winner is up).
        return call_state["called_post_loop"]

    call_state["called_post_loop"] = False

    def fake_kill(*_args, **_kwargs):
        # Mark the post-loop reachability re-check as the winner-found
        # moment. ``_kill_pid_if_alive`` runs exactly between the poll
        # deadline and the retry-or-return reachability check.
        call_state["called_post_loop"] = True

    monkeypatch.setattr(_autospawn, "_socket_reachable", fake_reachable)
    monkeypatch.setattr(_autospawn, "_launchagent_installed", lambda: False)
    monkeypatch.setattr(os, "posix_spawn", fake_spawn)
    monkeypatch.setattr(os, "waitpid", lambda pid, opts: (0, 0))
    monkeypatch.setattr(os, "kill", fake_kill)

    _autospawn.ensure_daemon_or_spawn(
        socket_path=Path("/tmp/never-used.sock"),
        readiness_timeout_s=0.05,
    )
    assert spawn_calls["count"] == 1


def test_auto_spawn_disabled_returns_without_probing_launchagent(monkeypatch):
    monkeypatch.setattr(_autospawn, "_socket_reachable", lambda _p: False)
    monkeypatch.setattr(
        _autospawn,
        "_launchagent_installed",
        lambda: pytest.fail("auto_spawn=False but launchctl was probed"),
    )

    _autospawn.ensure_daemon_or_spawn(
        socket_path=Path("/tmp/never-used.sock"),
        auto_spawn=False,
    )


def test_resolve_daemon_binary_returns_absolute(monkeypatch):
    # Force the dev path where argv[0] exists.
    monkeypatch.setattr("sys.frozen", False, raising=False)
    monkeypatch.setattr("sys.argv", ["/usr/local/bin/screencap"])
    resolved = _autospawn._resolve_daemon_binary()
    assert resolved.is_absolute()


def test_open_auto_log_falls_back_on_symlink_redirect(tmp_path, monkeypatch):
    real_logs = tmp_path / "real"
    real_logs.mkdir()
    fake_run = tmp_path / "fake_run"
    fake_run.symlink_to(real_logs)
    log_path = fake_run / "auto-serve.log"
    monkeypatch.setattr(_autospawn, "_AUTO_LOG_PATH", log_path)
    fd = _autospawn._open_auto_log()
    try:
        # Symlink-redirect detected → fd should point at /dev/null. We
        # can't easily introspect the fd target portably, but writing
        # bytes to /dev/null is a no-op while writing to a real file
        # would fail when the parent dir doesn't exist. Both paths are
        # fd-write-safe; the real assertion is that no log file is
        # created at the real_logs path because we routed elsewhere.
        os.write(fd, b"contents that should never land in real_logs")
    finally:
        os.close(fd)
    assert not (real_logs / "auto-serve.log").exists()
