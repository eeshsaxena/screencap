"""End-to-end exit-2 + stopped-event tests for `screencap start`.

Covers todos 021, 022:

- 021: invoking the CLI under a held lock must propagate exit code 2
  through cli.py's start command.
- 022: the `stopped` event must fire on all three exit paths from the
  start command (clean SystemExit, exception, normal return).

These exercise the cli.py:553-567 control flow that earlier reviews flagged
as untested. The previous `TestExitCodeContract.test_documented_exit_codes`
was a tautology and has been replaced.
"""

from __future__ import annotations

import json
import sys

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate_lock(tmp_path, monkeypatch):
    """Redirect lockfile + recordings dir to a per-test tmp path so a stale
    ``recording.lock`` in ``~/.screencap/run/`` (e.g., from an aborted real
    recording) doesn't poison these tests by raising LockContended before our
    stub controller even gets instantiated. Likewise for stale recording
    directories at ``~/.screencap/recordings/<name>/``."""
    from screencap import pidfile as _pidfile

    lock_dir = tmp_path / "run"
    monkeypatch.setattr(_pidfile, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(_pidfile, "LOCK_FILE", lock_dir / "recording.lock")
    monkeypatch.setattr(_pidfile, "PID_FILE", tmp_path / "recording.pid")
    if _pidfile._LOCKED_FD is not None:
        try:
            _pidfile.release_lock()
        except Exception:
            pass
        _pidfile._LOCKED_FD = None

    # Also isolate the recordings dir so cli.py's "directory already exists"
    # check doesn't fire on a leftover ~/.screencap/recordings/test/ from a
    # prior aborted run.
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings_dir))
    import screencap.config
    screencap.config._config_cache = None

    # ``conftest.py`` sets SCREENCAP_LEGACY_START=1 which routes the start
    # command through ``_legacy_start_recording`` (which calls the real
    # ``start_recording`` directly). For these tests we want the
    # SessionController path so the stub gets exercised.
    monkeypatch.delenv("SCREENCAP_LEGACY_START", raising=False)

    yield
    if _pidfile._LOCKED_FD is not None:
        try:
            _pidfile.release_lock()
        except Exception:
            pass
        _pidfile._LOCKED_FD = None


def _stopped_events(stderr: str) -> list[dict]:
    out = []
    for line in stderr.splitlines():
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "stopped":
            out.append(payload)
    return out


def _patch_session_controller(monkeypatch, stub_class):
    """Replace SessionController at the module path the start command imports
    from (`screencap.session.SessionController`), so the deferred
    `from screencap.session import SessionController` inside cli.py picks up
    the stub."""
    from screencap import session as _session_module
    monkeypatch.setattr(_session_module, "SessionController", stub_class)


def test_lock_contention_propagates_exit_code_2(monkeypatch):
    """SessionController(__init__) raising SystemExit(2) propagates through
    the cli.py start command and fires the terminal `stopped` event."""
    from screencap import cli

    class _StubSessionController:
        def __init__(self, args):
            raise SystemExit(2)

        def run(self):
            raise AssertionError("run() should never be called")

    _patch_session_controller(monkeypatch, _StubSessionController)

    runner = CliRunner()
    result = runner.invoke(cli.cli, ["start", "--name", "test", "--no-audio"])

    assert result.exit_code == 2, result.output
    events = _stopped_events(result.output)
    assert any(evt.get("exit_code") == 2 for evt in events), (
        "Expected stopped event with exit_code=2, got: " + result.output
    )


def test_uncaught_exception_emits_stopped_with_exit_code_1(monkeypatch):
    """An exception inside controller.run() (or its construction) emits
    `stopped` with exit_code=1 and propagates as SystemExit(1)."""
    from screencap import cli

    class _CrashingController:
        def __init__(self, args):
            pass

        def run(self):
            raise RuntimeError("boom — synthetic failure")

    _patch_session_controller(monkeypatch, _CrashingController)

    runner = CliRunner()
    result = runner.invoke(cli.cli, ["start", "--name", "test", "--no-audio"])

    assert result.exit_code == 1, result.output
    events = _stopped_events(result.output)
    assert any(evt.get("exit_code") == 1 for evt in events), (
        "Expected stopped event with exit_code=1, got: " + result.output
    )


def test_clean_exit_emits_stopped_with_exit_code_0(monkeypatch):
    """When controller.run() returns normally, `stopped` fires with exit 0."""
    from screencap import cli

    class _CleanController:
        def __init__(self, args):
            pass

        def run(self):
            return None  # normal return

    _patch_session_controller(monkeypatch, _CleanController)

    runner = CliRunner()
    result = runner.invoke(cli.cli, ["start", "--name", "test", "--no-audio"])

    assert result.exit_code == 0, result.output
    events = _stopped_events(result.output)
    assert any(evt.get("exit_code") == 0 for evt in events), (
        "Expected stopped event with exit_code=0, got: " + result.output
    )
