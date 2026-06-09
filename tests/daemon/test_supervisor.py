"""Supervisor primitive tests for daemon-owned recording sessions."""

from __future__ import annotations

import asyncio
import errno
import fcntl
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
    allow_tmp_output_dir,
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
    allow_tmp_output_dir,
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
    allow_tmp_output_dir,
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


# ---------------------------------------------------------------------------
# U6: F_SETPIPE_SZ widening on engine stderr
# ---------------------------------------------------------------------------


_HAS_F_SETPIPE_SZ = hasattr(fcntl, "F_SETPIPE_SZ") and hasattr(fcntl, "F_GETPIPE_SZ")


def test_widen_stderr_pipe_grows_buffer_above_default() -> None:
    """On Linux, the helper enlarges the kernel pipe past the default size so
    a briefly-paused ``_stderr_pump`` does not transitively block the engine's
    ``sys.stderr.write`` on burst output. Skipped where F_SETPIPE_SZ is
    unavailable (notably macOS)."""
    if not _HAS_F_SETPIPE_SZ:
        pytest.skip("F_SETPIPE_SZ not available on this platform")

    from screencap.daemon.supervisor import _widen_stderr_pipe

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stderr=subprocess.PIPE,
    )
    try:
        before = fcntl.fcntl(proc.stderr.fileno(), fcntl.F_GETPIPE_SZ)
        applied = _widen_stderr_pipe(proc)
        after = fcntl.fcntl(proc.stderr.fileno(), fcntl.F_GETPIPE_SZ)
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    assert applied is not None and applied >= (1 << 17)
    assert after > before
    assert after >= 1 << 17  # at minimum the 128 KiB fallback was applied


def test_widen_stderr_pipe_falls_back_on_einval(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the kernel rejects the 1 MiB request with EINVAL, the helper
    retries with 128 KiB and returns the smaller size."""
    if not _HAS_F_SETPIPE_SZ:
        pytest.skip("F_SETPIPE_SZ not available on this platform")

    from screencap.daemon import supervisor as supervisor_module

    real_fcntl = fcntl.fcntl
    calls: list[int] = []

    def fake_fcntl(fd: int, op: int, arg: int = 0) -> int:
        if op == fcntl.F_SETPIPE_SZ:
            calls.append(arg)
            if arg == (1 << 20):
                raise OSError(errno.EINVAL, "would-be-too-big")
            return arg
        return real_fcntl(fd, op, arg)

    monkeypatch.setattr(supervisor_module.fcntl, "fcntl", fake_fcntl)

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stderr=subprocess.PIPE,
    )
    try:
        applied = supervisor_module._widen_stderr_pipe(proc)
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    assert calls == [1 << 20, 1 << 17]
    assert applied == (1 << 17)


def test_widen_stderr_pipe_returns_none_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platforms without F_SETPIPE_SZ (e.g., macOS) get a clean no-op
    instead of an exception. The helper logs the limitation and returns
    None."""
    from screencap.daemon import supervisor as supervisor_module

    monkeypatch.delattr(supervisor_module.fcntl, "F_SETPIPE_SZ", raising=False)

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stderr=subprocess.PIPE,
    )
    try:
        applied = supervisor_module._widen_stderr_pipe(proc)
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    assert applied is None


def test_widen_stderr_pipe_swallows_other_oserrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors other than EINVAL also produce a clean no-op — the engine still
    spawns; the pipe stays at the default size."""
    if not _HAS_F_SETPIPE_SZ:
        pytest.skip("F_SETPIPE_SZ not available on this platform")

    from screencap.daemon import supervisor as supervisor_module

    def fake_fcntl(*_args: object, **_kwargs: object) -> int:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(supervisor_module.fcntl, "fcntl", fake_fcntl)

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        stderr=subprocess.PIPE,
    )
    try:
        applied = supervisor_module._widen_stderr_pipe(proc)
    finally:
        proc.terminate()
        proc.wait(timeout=2)

    assert applied is None


# ---------------------------------------------------------------------------
# U3: Supervisor.stop() TOCTOU regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_returns_fast_when_finalized_published_during_subscribe_gap(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
    allow_tmp_output_dir,
) -> None:
    """TKT-A regression: ``recording_finalized`` published in the await gap
    between ``is_alive()`` and the stop subscription must NOT cause
    ``stop()`` to block for the full ``stop_timeout``. With U1's replay
    buffer plus U3's pre-check cursor capture, the late subscription
    picks up the event from the ring and returns within 1 second.

    Reproduction: hold the supervisor's late ``subscribe()`` on a test
    barrier; publish ``recording_finalized`` directly to the bus during
    that window (mimicking what ``_exit_poll`` would do in production);
    release the barrier; assert ``stop()`` returns within 1 s.
    """
    from screencap.daemon.supervisor import Supervisor

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        # poll_interval long enough that `_exit_poll` does not interfere
        # — the test injects the race condition deterministically below.
        poll_interval=60.0,
        startup_timeout=2.0,
        stop_timeout=5.0,
    )

    await supervisor.spawn(
        schema.RecordingStartRequest(name="race", output_dir=str(tmp_path / "race"))
    )

    # Install a barrier on bus.subscribe — only on calls without `since`
    # (live-only subscriptions). U3's fix calls subscribe(since=N) for the
    # late stop subscription, so we need to capture that specific call and
    # delay it until after we publish the finalized event.
    original_subscribe = bus.subscribe
    barrier = asyncio.Event()
    subscribe_entered = asyncio.Event()
    captured_since: list[int | None] = []

    async def slow_subscribe(since: int | None = None):
        captured_since.append(since)
        # Only delay the stop's late subscribe — match by `since` being a
        # non-None int (the pre-check cursor); first calls during spawn
        # are live (since=None) and must not block.
        if since is not None:
            # Pin the moment stop() has entered subscribe() and is now
            # suspended at the barrier; the test body waits on this
            # event so the racing publish can never land before stop()'s
            # late subscription has captured its cursor.
            subscribe_entered.set()
            await barrier.wait()
        return await original_subscribe(since=since)

    monkeypatch.setattr(bus, "subscribe", slow_subscribe)

    # Launch stop() in a task — it will pause at the barrier-decorated
    # subscribe call.
    stop_task = asyncio.create_task(supervisor.stop(force=False))

    # Wait deterministically until stop() has entered the late subscribe
    # and is suspended at the barrier. Replaces a fragile asyncio.sleep
    # that would otherwise depend on scheduler timing.
    await asyncio.wait_for(subscribe_entered.wait(), timeout=2.0)

    # Publish the racing event. With U1's replay buffer this lands in
    # the ring at the next cursor; the late subscribe(since=...) will
    # pick it up via replay.
    await bus.publish(
        {
            "type": _stderr_events.EVENT_RECORDING_FINALIZED,
            "schema_version": 1,
            "ts": time.time(),
            "name": "race",
            "duration_seconds": 0.1,
            "force_stopped": False,
            "disk_full": False,
        }
    )

    # Release the barrier so the stop subscription completes.
    barrier.set()

    start = time.monotonic()
    result = await asyncio.wait_for(stop_task, timeout=2.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"stop() took {elapsed:.2f}s; should have observed replay"
    assert result["final_state"] == "stopped"
    assert any(since is not None for since in captured_since), (
        "stop() should call subscribe(since=...) with the pre-check cursor"
    )

    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_shutdown_subscribes_with_pre_terminate_cursor(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
    allow_tmp_output_dir,
) -> None:
    """Same TOCTOU shape on the ``Supervisor.shutdown()`` path: the late
    subscription must use ``subscribe(since=cursor_before_terminate)`` so a
    ``recording_finalized`` published in the await gap between
    ``proc.terminate()`` and the late subscription lands via replay."""
    from screencap.daemon.supervisor import Supervisor

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=60.0,
        startup_timeout=2.0,
        stop_timeout=2.0,
    )

    await supervisor.spawn(
        schema.RecordingStartRequest(name="srace", output_dir=str(tmp_path / "srace"))
    )

    original_subscribe = bus.subscribe
    captured_since: list[int | None] = []

    async def recording_subscribe(since: int | None = None):
        captured_since.append(since)
        return await original_subscribe(since=since)

    monkeypatch.setattr(bus, "subscribe", recording_subscribe)

    await supervisor.shutdown()

    # The pre-terminate cursor capture is what gives replay a chance to
    # cover the await gap. shutdown() must call subscribe(since=...) for
    # its late finalized-await subscription, not the legacy live-only
    # subscribe() that races with `_stderr_pump.publish`.
    assert any(since is not None for since in captured_since), (
        "shutdown() should call subscribe(since=...) with a pre-terminate cursor"
    )


@pytest.mark.asyncio
async def test_engine_stderr_burst_survives_paused_pump(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
    allow_tmp_output_dir,
) -> None:
    """End-to-end shape: a brief pause of ``_stderr_pump`` while the engine
    burst-writes ~150 KiB of events does not back-pressure the engine. With
    the F_SETPIPE_SZ widening at 1 MiB (or 128 KiB fallback), the kernel
    pipe absorbs the burst; without widening, the engine would block on
    ``sys.stderr.write`` waiting for a reader."""
    if not _HAS_F_SETPIPE_SZ:
        pytest.skip("F_SETPIPE_SZ not available on this platform")

    from screencap.daemon.supervisor import Supervisor

    # FAKE_FRAME_EVENTS=1 makes the existing fake-engine script emit a
    # `chunk_finalized` event on each tick of its inner loop. We bump the
    # poll frequency by reducing the sleep, then briefly delay the pump.
    monkeypatch.setenv("FAKE_FRAME_EVENTS", "1")

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=0.05,
        startup_timeout=2.0,
        stop_timeout=2.0,
    )

    state = await supervisor.spawn(
        schema.RecordingStartRequest(name="burst", output_dir=str(tmp_path / "burst"))
    )
    assert isinstance(state["engine_pid"], int)

    # Let the engine accumulate events while the pump is naturally draining,
    # then stop and verify clean shutdown — if the widening were missing and
    # the kernel pipe filled, the engine's stderr.write would block and
    # SIGTERM handling would be delayed past the 2 s stop_timeout.
    await asyncio.sleep(0.5)
    stopped = await supervisor.stop(force=False)
    assert stopped == {"stopped": True, "final_state": "stopped"}
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_exit_poll_before_subscribe_no_double_release(
    tmp_path: Path,
    fake_engine_script: Path,
    isolated_lock,
    monkeypatch: pytest.MonkeyPatch,
    allow_tmp_output_dir,
) -> None:
    """F4 regression: shutdown() must cancel `_exit_poll` before the
    late `subscribe(since=)` so a concurrent poll iteration cannot run
    `_handle_engine_exit` (which calls `_release_daemon_lock`) at the
    same time shutdown()'s teardown does. The exit_lock serializes the
    two callers, but the second arrival should observe `exit_handled`
    and bail — and the lock release should happen exactly once."""
    from screencap import pidfile
    from screencap.daemon.supervisor import Supervisor

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(fake_engine_script),
        reconcile_on_init=False,
        poll_interval=0.01,
        startup_timeout=2.0,
        stop_timeout=2.0,
    )

    await supervisor.spawn(
        schema.RecordingStartRequest(name="cancel", output_dir=str(tmp_path / "cancel"))
    )

    release_calls = 0
    original_release = pidfile.release_lock

    def counting_release() -> None:
        nonlocal release_calls
        release_calls += 1
        return original_release()

    monkeypatch.setattr(pidfile, "release_lock", counting_release)

    await supervisor.shutdown()

    # release_lock may run twice in the legitimate teardown (Supervisor's
    # `_release_daemon_lock` is called by `_handle_engine_exit` AND from
    # the `shutdown()` outer block after `_reset_state`). What we're
    # asserting is that the lock is *not* re-released by a phantom
    # `_exit_poll` iteration racing with shutdown's late subscribe.
    # Without F4's cancellation a third call appears.
    assert release_calls <= 2, f"release_lock called {release_calls} times; expected <= 2"


# ---------------------------------------------------------------------------
# U5: out-of-band engine ID-token seam
# ---------------------------------------------------------------------------


def _cloud_engine_script(tmp_path: Path) -> Path:
    """A fake engine that reports what it received via the out-of-band token
    channel: the token it read from SCREENCAP_ENGINE_TOKEN_FILE, whether the env
    var was set, and whether the token string leaked into its argv."""
    script = tmp_path / "cloud_engine.py"
    script.write_text(
        textwrap.dedent(
            '''
            import json, os, signal, sys, time

            def emit(t, **p):
                sys.stderr.write(json.dumps({"type": t, "schema_version": 1, "ts": time.time(), **p}) + "\\n")
                sys.stderr.flush()

            def handle_term(_s, _f):
                emit("recording_finalized", name="cloud", duration_seconds=0.1, force_stopped=False, disk_full=False)
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, handle_term)
            token_file = os.environ.get("SCREENCAP_ENGINE_TOKEN_FILE")
            token_seen = ""
            if token_file:
                try:
                    token_seen = open(token_file).read().strip()
                except OSError:
                    token_seen = ""
            argv_has_token = bool(token_seen) and any(token_seen in a for a in sys.argv)
            emit("started", claimant="daemon", token_env_set=bool(token_file),
                 token_seen=token_seen, argv_has_token=argv_has_token)
            while True:
                time.sleep(0.05)
            '''
        ),
        encoding="utf-8",
    )
    return script


@pytest.mark.asyncio
async def test_cloud_engine_receives_token_out_of_band_not_in_argv(
    tmp_path: Path, isolated_lock, allow_tmp_output_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "secret-id-token")

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(_cloud_engine_script(tmp_path)),
        reconcile_on_init=False, poll_interval=0.05, startup_timeout=2.0, stop_timeout=2.0,
    )
    sub = await bus.subscribe()
    await supervisor.spawn(
        schema.RecordingStartRequest(
            name="cloud", output_dir=str(tmp_path / "cloud"), cloud_intent=True,
        )
    )

    started = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert started["type"] == _stderr_events.EVENT_STARTED
    # The engine got the token via the out-of-band file (env-delivered path)...
    assert started["token_env_set"] is True
    assert started["token_seen"] == "secret-id-token"
    # ...and the token NEVER appeared on its command line (ps-visibility guard).
    assert started["argv_has_token"] is False

    # The staged file is mode 0600 and holds exactly the token.
    token_path = supervisor._engine_token_file
    assert token_path is not None and token_path.exists()
    assert (token_path.stat().st_mode & 0o777) == 0o600
    assert token_path.read_text().strip() == "secret-id-token"

    await supervisor.stop(force=False)
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    # Torn down on engine exit.
    assert supervisor._engine_token_file is None
    assert not token_path.exists()
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_cloud_engine_not_signed_in_fails_closed_no_token(
    tmp_path: Path, isolated_lock, allow_tmp_output_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap import auth
    from screencap.daemon.supervisor import Supervisor

    def not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", not_signed_in)

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(_cloud_engine_script(tmp_path)),
        reconcile_on_init=False, poll_interval=0.05, startup_timeout=2.0, stop_timeout=2.0,
    )
    sub = await bus.subscribe()
    await supervisor.spawn(
        schema.RecordingStartRequest(
            name="nc", output_dir=str(tmp_path / "nc"), cloud_intent=True,
        )
    )

    started = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    # Fail-closed, not fail-stop: the recording still starts (stays local); the
    # engine just gets no token, so live upload fails closed (no deletion).
    assert started["type"] == _stderr_events.EVENT_STARTED
    assert started["token_env_set"] is False
    assert supervisor._engine_token_file is None

    await supervisor.stop(force=False)
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    await supervisor.shutdown()


@pytest.mark.asyncio
async def test_local_recording_stages_no_token_even_when_signed_in(
    tmp_path: Path, isolated_lock, allow_tmp_output_dir, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "tok")

    bus = EventBus()
    supervisor = Supervisor(
        bus,
        engine_command_factory=_factory(_cloud_engine_script(tmp_path)),
        reconcile_on_init=False, poll_interval=0.05, startup_timeout=2.0, stop_timeout=2.0,
    )
    sub = await bus.subscribe()
    await supervisor.spawn(
        schema.RecordingStartRequest(name="local", output_dir=str(tmp_path / "local"))
    )

    started = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
    assert started["token_env_set"] is False  # cloud_intent defaults False → no token
    assert supervisor._engine_token_file is None

    await supervisor.stop(force=False)
    await _wait_until(lambda: not isolated_lock.lock_is_active())
    await supervisor.shutdown()


def test_write_engine_token_file_is_0600(tmp_path: Path) -> None:
    from screencap.daemon.supervisor import Supervisor

    p = tmp_path / "engine-token.jwt"
    Supervisor._write_engine_token_file(p, "abc.def.ghi")
    assert p.read_text() == "abc.def.ghi"
    assert (p.stat().st_mode & 0o777) == 0o600
    # Overwrites in place, mode re-asserted.
    Supervisor._write_engine_token_file(p, "new")
    assert p.read_text() == "new"
    assert (p.stat().st_mode & 0o777) == 0o600


def test_token_has_no_field_in_argv_encoded_worker_args(tmp_path: Path) -> None:
    # Structural guard: the worker args (base64'd into argv) carry no token field,
    # so a token can never leak into ps/argv via the start handoff.
    from screencap.daemon.supervisor import build_engine_worker_args

    req = schema.RecordingStartRequest(
        name="x", output_dir=str(tmp_path), cloud_intent=True,
    )
    args = build_engine_worker_args(req, name="x", capture_dir=tmp_path)
    assert not any("token" in key.lower() for key in args), (
        f"worker args must carry no token-bearing field; keys: {list(args)}"
    )


@pytest.fixture
def isolated_base_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``config.get_base_dir()`` at a per-test tmp dir.

    The engine-token-file helpers resolve their run dir via ``get_base_dir() / run``
    (``Path.home() / .screencap`` in production). Redirect it so token-file tests
    never touch the developer's real ``~/.screencap/run``.
    """
    import screencap.config as cfg

    base = tmp_path / "base" / ".screencap"
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    return base


@pytest.mark.asyncio
async def test_reconcile_prunes_stale_engine_token_file(
    isolated_lock, isolated_base_dir: Path,
) -> None:
    """A hard crash / SIGKILL runs no Python teardown, so an ``engine-token-*.jwt``
    0600 file can survive in the run dir. The reconcile path at daemon startup must
    unlink any survivor — no live recording can own one at startup."""
    from screencap.daemon.supervisor import Supervisor

    run_dir = isolated_base_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    stale = run_dir / "engine-token-x.jwt"
    stale.write_text("leaked.id.token", encoding="utf-8")

    # A dead daemon-claimant engine drives the standard recovery reconcile path.
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
    supervisor = Supervisor(EventBus(), reconcile_on_init=True, reconcile_grace=0.1)
    await _wait_until(lambda: not supervisor.is_recovering(), timeout=3.0)

    assert not stale.exists(), "stale engine token file should be pruned at startup"
    await supervisor.shutdown()


# ---------------------------------------------------------------------------
# U5: token re-mint timer loop (_token_refresh_loop)
# ---------------------------------------------------------------------------


class _FakeAliveProc:
    """Minimal stand-in for _PopenEngineProcess: alive until flipped."""

    def __init__(self) -> None:
        self._alive = True
        self.pid = 4242

    def is_alive(self) -> bool:
        return self._alive


async def _run_loop_briefly(supervisor, proc) -> asyncio.Task:
    """Start _token_refresh_loop as a task. Caller asserts then cancels/joins."""
    task = asyncio.create_task(supervisor._token_refresh_loop(proc))
    return task


async def _stop_loop(proc: "_FakeAliveProc", task: asyncio.Task) -> None:
    """Tear down a running _token_refresh_loop task.

    Flipping the proc dead lets the loop's ``while`` guard return on its next
    iteration; the cancel covers the case where it is mid-``asyncio.sleep``. Either
    a normal return or a CancelledError is an acceptable, non-erroring exit."""
    proc._alive = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_token_refresh_loop_rewrites_file_in_place_without_restart(
    isolated_base_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful re-mint rewrites the token file in place — same path, new
    contents — and never touches the engine process (no restart)."""
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setenv("SCREENCAP_DAEMON_TOKEN_REFRESH_INTERVAL", "0.01")
    tokens = iter(["second-token", "second-token", "third-token"])
    monkeypatch.setattr(
        "screencap.auth.get_id_token", lambda force_refresh=False: next(tokens)
    )

    path = isolated_base_dir / "run" / "engine-token-live.jwt"
    path.parent.mkdir(parents=True, exist_ok=True)
    Supervisor._write_engine_token_file(path, "first-token")

    supervisor = Supervisor(EventBus(), reconcile_on_init=False)
    proc = _FakeAliveProc()
    supervisor._proc = proc
    supervisor._engine_token_file = path

    task = await _run_loop_briefly(supervisor, proc)
    try:
        await _wait_until(
            lambda: path.read_text() == "second-token", timeout=2.0
        )
        # Same file path (rewritten in place), and the engine proc is untouched.
        assert supervisor._proc is proc
        assert proc.is_alive() is True
    finally:
        await _stop_loop(proc, task)


@pytest.mark.asyncio
async def test_token_refresh_loop_autherror_leaves_file_unchanged_no_crash(
    isolated_base_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient AuthError mid-loop is logged and the loop continues — the
    recording proceeds and the token file is left unchanged."""
    from screencap import auth
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setenv("SCREENCAP_DAEMON_TOKEN_REFRESH_INTERVAL", "0.01")

    def boom(force_refresh=False):
        raise auth.AuthError("token service hiccup")

    monkeypatch.setattr("screencap.auth.get_id_token", boom)

    path = isolated_base_dir / "run" / "engine-token-live.jwt"
    path.parent.mkdir(parents=True, exist_ok=True)
    Supervisor._write_engine_token_file(path, "stable-token")

    supervisor = Supervisor(EventBus(), reconcile_on_init=False)
    proc = _FakeAliveProc()
    supervisor._proc = proc
    supervisor._engine_token_file = path

    task = await _run_loop_briefly(supervisor, proc)
    try:
        # Let the loop spin a few intervals; it must not crash or rewrite.
        await asyncio.sleep(0.1)
        assert not task.done(), "loop must keep running after AuthError"
        assert path.read_text() == "stable-token"
    finally:
        await _stop_loop(proc, task)


@pytest.mark.asyncio
async def test_token_refresh_loop_write_oserror_does_not_crash_loop(
    isolated_base_dir: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError from the in-place rewrite is logged and the loop continues —
    the engine just keeps the prior token and fails closed on the next 401."""
    from screencap.daemon import supervisor as sv
    from screencap.daemon.supervisor import Supervisor

    monkeypatch.setenv("SCREENCAP_DAEMON_TOKEN_REFRESH_INTERVAL", "0.01")
    monkeypatch.setattr(
        "screencap.auth.get_id_token", lambda force_refresh=False: "fresh-token"
    )

    path = isolated_base_dir / "run" / "engine-token-live.jwt"
    path.parent.mkdir(parents=True, exist_ok=True)
    Supervisor._write_engine_token_file(path, "prior-token")

    def write_boom(_path, _token):
        raise OSError("disk full")

    monkeypatch.setattr(sv.Supervisor, "_write_engine_token_file", staticmethod(write_boom))

    supervisor = Supervisor(EventBus(), reconcile_on_init=False)
    proc = _FakeAliveProc()
    supervisor._proc = proc
    supervisor._engine_token_file = path

    task = await _run_loop_briefly(supervisor, proc)
    try:
        await asyncio.sleep(0.1)
        assert not task.done(), "loop must keep running after a write OSError"
        assert path.read_text() == "prior-token"  # untouched
    finally:
        await _stop_loop(proc, task)


# ---------------------------------------------------------------------------
# U7 — resume_terminal_stage: the daemon-restart resume entry point runs the
# disk-driven terminal stage behind the per-recording flock, in a worker
# thread, in non_blocking mode (skips when a live finalize / manual upload
# holds the lock — never double-uploads, AE12).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_terminal_stage_runs_in_thread(tmp_path, monkeypatch):
    from screencap.daemon.supervisor import Supervisor

    bus = EventBus()
    sup = Supervisor(bus, reconcile_on_init=False)

    called = {}

    def _fake_run(recording_dir, *, non_blocking):
        import threading
        called["non_blocking"] = non_blocking
        called["thread"] = threading.current_thread().name
        return "ran"

    monkeypatch.setattr(
        "screencap.terminal_stage.run_terminal_stage", _fake_run,
    )

    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    out = await sup.resume_terminal_stage(rec_dir)

    assert out == "ran"
    # Resume always uses the non-blocking lock so it can't stall the loop or
    # race a live finalize.
    assert called["non_blocking"] is True
    # Ran off the asyncio loop (a worker thread), not the main thread.
    assert called["thread"] != "MainThread"


@pytest.mark.asyncio
async def test_resume_terminal_stage_skips_when_busy(tmp_path, monkeypatch):
    from screencap.daemon.supervisor import Supervisor
    from screencap.terminal_stage import TerminalStageBusy

    bus = EventBus()
    sup = Supervisor(bus, reconcile_on_init=False)

    def _busy(recording_dir, *, non_blocking):
        raise TerminalStageBusy("held by live finalize")

    monkeypatch.setattr(
        "screencap.terminal_stage.run_terminal_stage", _busy,
    )

    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    # Busy → returns None (skips), does NOT raise (the holder owns the section).
    out = await sup.resume_terminal_stage(rec_dir)
    assert out is None


# ---------------------------------------------------------------------------
# SCR-125 U6 — daemon auto-resume + startup sweep + idle-busy + fail-closed.
# ---------------------------------------------------------------------------


def _make_incomplete_cloud_recording(parent: Path, name: str, *, n_chunks: int,
                                     uploaded: tuple[int, ...] = ()) -> Path:
    """A cloud recording with an ENGINE-frozen chunks_expected and an unsatisfied
    finalize gate (some chunks not UPLOADED) — a sweep/resume candidate."""
    import json as _json

    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = parent / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()
    for i in range(n_chunks):
        (rec_dir / f"chunk_{i:04d}.mp4").write_bytes(b"\x00" * 64)
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    for i in range(n_chunks):
        ledger.seed_chunk(i)
        ledger.mark_staged(i)
    for i in uploaded:
        ledger.mark_uploaded(i)
    ledger.freeze_chunks_expected(n_chunks)  # ENGINE-origin freeze
    (rec_dir / ".recording_intent").write_text(_json.dumps({
        "version": 2, "destination": "cloud",
        "retention_policy": "keep_forever", "retention_params": {},
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


@pytest.mark.asyncio
async def test_resume_fails_closed_on_not_signed_in(tmp_path, monkeypatch):
    """Research H4: a not-signed-in / promotion error in the resume returns None
    and never propagates into the asyncio loop (nothing evicted)."""
    from screencap import auth
    from screencap.daemon.supervisor import Supervisor

    sup = Supervisor(EventBus(), reconcile_on_init=False)

    def _not_signed_in(recording_dir, *, non_blocking):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.terminal_stage.run_terminal_stage", _not_signed_in)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    assert await sup.resume_terminal_stage(rec_dir) is None


@pytest.mark.asyncio
async def test_handle_engine_exit_fires_resume_for_cloud_once(tmp_path, monkeypatch):
    """The single exit funnel enqueues the resume exactly once for a cloud
    recording (graceful/crash/SystemExit all converge here)."""
    from screencap.daemon.supervisor import Supervisor

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    (rec_dir / ".recording_intent").write_text(json.dumps({"destination": "cloud"}))

    resumed: list[Path] = []

    async def _spy(d):
        resumed.append(Path(d))

    monkeypatch.setattr(sup, "resume_terminal_stage", _spy)

    proc = _FakeAliveProc()
    proc._alive = False
    sup._proc = proc
    sup._session_state = {
        "capture_dir": str(rec_dir), "recording_name": "rec", "started_at": time.time(),
    }
    await sup._handle_engine_exit(proc, 0)
    # A resume task was tracked; drain it.
    await asyncio.gather(*list(sup._resume_tasks))
    assert resumed == [rec_dir]
    # Re-entry is a no-op (single trigger): _exit_handled / _proc reset.
    await sup._handle_engine_exit(proc, 0)
    assert resumed == [rec_dir]


@pytest.mark.asyncio
async def test_handle_engine_exit_skips_resume_for_local(tmp_path, monkeypatch):
    """A LOCAL recording has nothing to upload → no resume is scheduled."""
    from screencap.daemon.supervisor import Supervisor

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    rec_dir = tmp_path / "rec"
    rec_dir.mkdir()
    (rec_dir / ".recording_intent").write_text(json.dumps({"destination": "local"}))

    resumed: list[Path] = []
    monkeypatch.setattr(sup, "resume_terminal_stage",
                        lambda d: resumed.append(Path(d)))

    proc = _FakeAliveProc()
    proc._alive = False
    sup._proc = proc
    sup._session_state = {
        "capture_dir": str(rec_dir), "recording_name": "rec", "started_at": time.time(),
    }
    await sup._handle_engine_exit(proc, 0)
    assert not sup._resume_tasks
    assert resumed == []


@pytest.mark.asyncio
async def test_has_inflight_resume_gates_idle_shutdown(tmp_path):
    """An in-flight resume keeps the idle-shutdown watchdog 'busy' so an
    auto-spawned daemon never idle-exits mid-upload."""
    from types import SimpleNamespace

    from screencap.daemon import _idle_shutdown
    from screencap.daemon.supervisor import Supervisor

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    assert sup.has_inflight_resume() is False

    gate = asyncio.Event()

    async def _slow_resume():
        await gate.wait()

    sup._track_resume(_slow_resume())
    assert sup.has_inflight_resume() is True

    app = SimpleNamespace(state=SimpleNamespace(
        event_bus=SimpleNamespace(subscriber_count=lambda: 0),
        supervisor=sup,
    ))
    assert _idle_shutdown._daemon_is_busy(app) is True

    gate.set()
    await asyncio.gather(*list(sup._resume_tasks))
    assert sup.has_inflight_resume() is False
    assert _idle_shutdown._daemon_is_busy(app) is False


@pytest.mark.asyncio
async def test_startup_sweep_resumes_incomplete_cloud(tmp_path, monkeypatch):
    """F3: the startup sweep resumes a cloud recording that is engine-frozen but
    not finalize-complete."""
    from screencap.config import get_recordings_dir
    from screencap.daemon.supervisor import Supervisor

    rec_dir = _make_incomplete_cloud_recording(
        get_recordings_dir(), "incomplete-cloud", n_chunks=2, uploaded=(0,),
    )
    monkeypatch.setattr("screencap.auth.get_id_token", lambda force_refresh=False: "tok")

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    resumed: list[Path] = []

    async def _spy(d):
        resumed.append(Path(d))

    monkeypatch.setattr(sup, "resume_terminal_stage", _spy)
    await sup._run_startup_sweep()
    assert rec_dir in resumed


@pytest.mark.asyncio
async def test_startup_sweep_skips_when_not_signed_in(tmp_path, monkeypatch):
    """Not signed in → the sweep skips entirely (recordings preserved, never a
    partial convergence without auth)."""
    from screencap import auth
    from screencap.config import get_recordings_dir
    from screencap.daemon.supervisor import Supervisor

    _make_incomplete_cloud_recording(
        get_recordings_dir(), "incomplete-cloud", n_chunks=2, uploaded=(0,),
    )

    def _not_signed_in(force_refresh=False):
        raise auth.NotSignedIn("no creds")

    monkeypatch.setattr("screencap.auth.get_id_token", _not_signed_in)

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    resumed: list[Path] = []

    async def _spy(d):
        resumed.append(Path(d))

    monkeypatch.setattr(sup, "resume_terminal_stage", _spy)
    await sup._run_startup_sweep()
    assert resumed == []


def test_recording_needs_resume_crash_before_freeze_is_false(tmp_path):
    """Prevention rules #1/#4: a recording whose chunks_expected was never frozen
    (crash before finalize) is NOT a sweep candidate — it is never auto-converted
    to 'complete' (no false sentinel); full recovery is via manual upload."""
    import json as _json

    from screencap.daemon.supervisor import Supervisor
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / "crash-orphan"
    rec_dir.mkdir()
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin",
        "monitor_width": 1920, "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    session.close()
    engine.dispose()
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    ledger.seed_chunk(0)
    ledger.mark_staged(0)
    # NOTE: chunks_expected is deliberately NOT frozen (crash before finalize).
    (rec_dir / ".recording_intent").write_text(_json.dumps({"destination": "cloud"}))

    # No engine-origin frozen count → not a sweep candidate (never auto-completed).
    assert Supervisor._recording_needs_resume(rec_dir) is False
