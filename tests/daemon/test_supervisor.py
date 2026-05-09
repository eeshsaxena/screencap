"""Supervisor primitive tests for daemon-owned recording sessions."""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable, Iterator

import pytest

from screencap import _stderr_events
from screencap.daemon import schema
from screencap.daemon.event_bus import EventBus


@pytest.fixture
def isolated_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from screencap import pidfile

    lock_dir = tmp_path / "home" / ".screencap" / "run"
    monkeypatch.setattr(pidfile, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(pidfile, "LOCK_FILE", lock_dir / "recording.lock")
    monkeypatch.setattr(pidfile, "PID_FILE", lock_dir.parent / "recording.pid")
    pidfile._reset_for_tests()
    yield pidfile
    pidfile._reset_for_tests()


@pytest.fixture
def fake_engine_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import base64
            import json
            import os
            import signal
            import sys
            import time


            def _args() -> dict:
                if len(sys.argv) < 2:
                    return {}
                return json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))


            ARGS = _args()
            NAME = ARGS.get("name") or "fake"


            def emit(event_type: str, **payload) -> None:
                sys.stderr.write(
                    json.dumps(
                        {
                            "type": event_type,
                            "schema_version": 1,
                            "ts": time.time(),
                            **payload,
                        }
                    )
                    + "\\n"
                )
                sys.stderr.flush()


            def handle_term(_signum, _frame) -> None:
                if os.environ.get("FAKE_FINALIZE_ON_TERM", "1") == "1":
                    emit(
                        "recording_finalized",
                        name=NAME,
                        duration_seconds=0.25,
                        force_stopped=False,
                        disk_full=False,
                    )
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, handle_term)
            emit("started", claimant="daemon")
            mode = os.environ.get("FAKE_ENGINE_MODE", "sleep")
            if mode == "exit_nonzero":
                raise SystemExit(7)
            if mode == "segv":
                os.kill(os.getpid(), signal.SIGSEGV)
            while True:
                if os.environ.get("FAKE_FRAME_EVENTS") == "1":
                    emit("chunk_finalized", chunk_index=1, frames_written=12)
                time.sleep(0.1)
            """
        ),
        encoding="utf-8",
    )
    return script


def _factory(script: Path, *, env: dict[str, str] | None = None) -> Callable[[str], list[str]]:
    if env:
        # The production supervisor command factory only returns argv. Tests
        # set process-wide env vars before spawn so this helper mirrors that
        # narrower interface.
        for key, value in env.items():
            assert isinstance(key, str)
            assert isinstance(value, str)

    def build(encoded_args: str) -> list[str]:
        return [sys.executable, str(script), encoded_args]

    return build


async def _next_of_type(bus: EventBus, event_type: str, *, timeout: float = 3.0) -> dict:
    sub = await bus.subscribe()
    try:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"timed out waiting for {event_type}"
            event = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
            if event.get("type") == event_type:
                return event
    finally:
        await bus.remove(sub)


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition did not become true before timeout")


@pytest.mark.asyncio
async def test_spawn_stop_fake_worker_reports_current_session(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
) -> None:
    from screencap.daemon.supervisor import Supervisor

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=0.05,
        startup_timeout=2.0,
        stop_timeout=2.0,
    )
    sub = await bus.subscribe()

    state = await supervisor.spawn(
        schema.RecordingStartRequest(name="demo", output_dir=str(tmp_path / "demo"))
    )

    started = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert started["type"] == _stderr_events.EVENT_STARTED
    assert state["session_id"] == "demo"
    assert isinstance(state["engine_pid"], int)
    assert supervisor.current_session()["engine_pid"] == state["engine_pid"]
    assert isolated_lock.read_lock_metadata()["claimant"] == "daemon"

    final_sub = await bus.subscribe()
    stopped = await supervisor.stop(force=False)

    finalized = await asyncio.wait_for(final_sub.queue.get(), timeout=2.0)
    assert finalized["type"] == _stderr_events.EVENT_RECORDING_FINALIZED
    assert stopped == {"stopped": True, "final_state": "stopped"}
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    assert supervisor.current_session() is None
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_worker_nonzero_exit_publishes_crash_and_finalized(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setenv("FAKE_ENGINE_MODE", "exit_nonzero")
    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=0.05,
        startup_timeout=2.0,
        stop_timeout=1.0,
    )
    sub = await bus.subscribe()

    await supervisor.spawn(
        schema.RecordingStartRequest(name="crash", output_dir=str(tmp_path / "crash"))
    )

    seen = [await asyncio.wait_for(sub.queue.get(), timeout=3.0) for _ in range(3)]
    types = [event["type"] for event in seen]
    assert _stderr_events.EVENT_ENGINE_CRASHED in types
    assert types.index(_stderr_events.EVENT_ENGINE_CRASHED) < types.index(
        _stderr_events.EVENT_RECORDING_FINALIZED
    )
    assert seen[types.index(_stderr_events.EVENT_RECORDING_FINALIZED)]["force_stopped"] is True
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_worker_sigsegv_publishes_crash_and_finalized(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setenv("FAKE_ENGINE_MODE", "segv")
    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=0.05,
        startup_timeout=2.0,
        stop_timeout=1.0,
    )
    sub = await bus.subscribe()

    await supervisor.spawn(
        schema.RecordingStartRequest(name="segv", output_dir=str(tmp_path / "segv"))
    )

    events = [await asyncio.wait_for(sub.queue.get(), timeout=3.0) for _ in range(3)]
    by_type = {event["type"]: event for event in events}
    crashed = by_type[_stderr_events.EVENT_ENGINE_CRASHED]
    finalized = by_type[_stderr_events.EVENT_RECORDING_FINALIZED]
    assert crashed["exit_code"] < 0
    assert finalized["force_stopped"] is True
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    await supervisor.shutdown()


def _write_lock(lock_file: Path, payload: dict) -> None:
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def sleep_process() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


@pytest.mark.asyncio
async def test_orphan_reconciliation_terminates_live_daemon_engine(
    isolated_lock,
    sleep_process: subprocess.Popen[bytes],
) -> None:
    from screencap.daemon.supervisor import Supervisor

    _write_lock(
        isolated_lock.LOCK_FILE,
        {
            "claimant": "daemon",
            "pid": 999999,
            "engine_pid": sleep_process.pid,
            "started_at": 1778198400.0,
            "recording_started_at": 1778198401.0,
            "recording_name": "orphan",
        },
    )
    bus = EventBus()
    sub = await bus.subscribe()

    supervisor = Supervisor(
        bus,
        reconcile_on_init=True,
        reconcile_grace=0.1,
        poll_interval=0.05,
    )

    assert supervisor.is_recovering() is True
    await _wait_until(lambda: not supervisor.is_recovering(), timeout=3.0)
    assert sleep_process.poll() is not None
    event = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert event["type"] == _stderr_events.EVENT_PREVIOUS_SESSION_FORCE_TERMINATED
    assert isolated_lock.read_lock_metadata() is None
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_orphan_reconciliation_recovers_dead_daemon_engine(isolated_lock) -> None:
    from screencap.daemon.supervisor import Supervisor

    _write_lock(
        isolated_lock.LOCK_FILE,
        {
            "claimant": "daemon",
            "pid": 999999,
            "engine_pid": 999999,
            "started_at": 1778198400.0,
            "recording_started_at": 1778198401.0,
            "recording_name": "dead",
        },
    )
    bus = EventBus()
    sub = await bus.subscribe()
    supervisor = Supervisor(bus, reconcile_on_init=True, reconcile_grace=0.1)

    await _wait_until(lambda: not supervisor.is_recovering(), timeout=3.0)

    event = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert event["type"] == _stderr_events.EVENT_PREVIOUS_SESSION_RECOVERED
    assert isolated_lock.read_lock_metadata() is None
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_orphan_reconciliation_leaves_cli_claimant_alone(isolated_lock) -> None:
    from screencap.daemon.supervisor import Supervisor

    payload = {
        "claimant": "cli",
        "pid": 999999,
        "started_at": 1778198400.0,
        "recording_started_at": 1778198401.0,
        "recording_name": "cli",
    }
    _write_lock(isolated_lock.LOCK_FILE, payload)
    supervisor = Supervisor(EventBus(), reconcile_on_init=True, reconcile_grace=0.1)

    await _wait_until(lambda: not supervisor.is_recovering(), timeout=3.0)

    assert isolated_lock.read_lock_metadata() == payload
    await supervisor.shutdown()
