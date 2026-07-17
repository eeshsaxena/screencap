"""SCR-214 U6: the periodic incremental-segmentation daemon task.

Covers the supervisor timer sweep that re-segments the LIVE ambient day so today's
Journal fills as the day progresses (R7, KTD4):

* with ambient enabled, the watch fires on its interval and runs
  ``run_incremental_segmentation`` over the ACTIVE ambient recording dir (the one
  the engine is writing), on a worker thread (not blocking the event loop),
* it is cancelled on shutdown (no leaked timer task),
* a pass that RAISES (a finalize holds the flock → ``TerminalStageBusy``, or any
  error) is swallowed — the watch keeps ticking and the daemon never crashes
  (fail-open outer belt),
* ambient DISABLED → the watch is never started and no pass runs.

The real ``run_incremental_segmentation`` is patched to a recorder so the timer
wiring is exercised without a real on-device model / DB. The engine subprocess is
a fake script (no real capture). Isolation via ``tests/daemon/conftest.py``.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable

import pytest

from screencap.daemon.event_bus import EventBus

# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors tests/daemon/test_ambient_day_roll.py)
# ---------------------------------------------------------------------------


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
    """Emit ``started`` then sleep; SIGTERM finalizes (name-stamped) + exits 0."""
    script = tmp_path / "fake_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            from __future__ import annotations

            import base64
            import json
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
                        {"type": event_type, "schema_version": 1,
                         "ts": time.time(), **payload}
                    )
                    + "\\n"
                )
                sys.stderr.flush()


            def handle_term(_signum, _frame) -> None:
                emit("recording_finalized", name=NAME, duration_seconds=0.25,
                     force_stopped=False, disk_full=False)
                raise SystemExit(0)


            signal.signal(signal.SIGTERM, handle_term)
            emit("started", claimant="daemon")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    return script


def _factory(script: Path) -> Callable[[str], list[str]]:
    def build(encoded_args: str) -> list[str]:
        return [sys.executable, str(script), encoded_args]

    return build


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition did not become true before timeout")


async def _pass_gate() -> None:
    return None


def _make_supervisor(script: Path, *, start_gate=_pass_gate, **kw):
    from screencap.daemon.supervisor import Supervisor

    defaults = dict(
        engine_command_factory=_factory(script),
        reconcile_on_init=False,
        poll_interval=0.02,
        startup_timeout=2.0,
        stop_timeout=1.0,
        start_gate=start_gate,
        ambient_day_tick_interval=100.0,  # keep the day-roll watch quiet.
        ambient_seg_tick_interval=0.02,   # tick the segmentation watch fast.
    )
    defaults.update(kw)
    return Supervisor(EventBus(), **defaults)


class _SegRecorder:
    """Records every ``run_incremental_segmentation`` call (thread-safe append)."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.stop_events: list = []
        self._raises = raises

    def __call__(self, recording_dir, *, non_blocking: bool = True, **kw):  # noqa: ANN001
        self.calls.append(str(recording_dir))
        self.stop_events.append(kw.get("stop_event"))
        # The daemon always passes non_blocking=True for the best-effort sweep.
        assert non_blocking is True
        if self._raises is not None:
            raise self._raises
        return None


def _install_recorder(monkeypatch, recorder: _SegRecorder) -> None:
    """Patch the terminal-stage entry the supervisor worker imports lazily."""
    import screencap.terminal_stage as ts

    monkeypatch.setattr(ts, "run_incremental_segmentation", recorder)


# ---------------------------------------------------------------------------
# The watch runs over the active ambient dir, periodically, on a worker thread.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segmentation_watch_runs_over_active_ambient_dir(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    recorder = _SegRecorder()
    _install_recorder(monkeypatch, recorder)

    sup = _make_supervisor(fake_engine_script)
    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    capture_dir = sup.current_session()["capture_dir"]

    # The timer fires and re-segments the ACTIVE ambient dir (more than once).
    await _wait_until(lambda: recorder.calls.count(capture_dir) >= 2)
    assert all(c == capture_dir for c in recorder.calls)

    await sup.shutdown()


# ---------------------------------------------------------------------------
# The watch is cancelled on shutdown (no leaked timer task).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segmentation_watch_cancelled_on_shutdown(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    _install_recorder(monkeypatch, _SegRecorder())

    sup = _make_supervisor(fake_engine_script)
    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    seg_task = sup._ambient_seg_task
    assert seg_task is not None and not seg_task.done()

    await sup.shutdown()

    assert seg_task.done()


# ---------------------------------------------------------------------------
# A raising pass (busy flock / any error) is swallowed — the watch keeps going.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_segmentation_watch_swallows_a_busy_pass(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent finalize holding the flock makes the pass raise
    ``TerminalStageBusy``; the worker swallows it and the watch keeps ticking so
    the daemon never crashes and later passes still run."""
    from screencap.terminal_stage import TerminalStageBusy

    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    recorder = _SegRecorder(raises=TerminalStageBusy("held by a live finalize"))
    _install_recorder(monkeypatch, recorder)

    sup = _make_supervisor(fake_engine_script)
    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    # Despite every pass raising, the watch keeps firing (swallowed, not fatal).
    await _wait_until(lambda: len(recorder.calls) >= 3)
    # The watch task is still alive and the ambient stream is still active.
    assert sup._ambient_seg_task is not None and not sup._ambient_seg_task.done()
    assert sup.ambient_state()["active"] is True

    await sup.shutdown()


# ---------------------------------------------------------------------------
# A store-lock interrupt is swallowed too — and every pass carries the shared
# quiesce flag as its stop_event (SCR-275 U4 / KTD-7).
# ---------------------------------------------------------------------------


@pytest.mark.privacy
@pytest.mark.asyncio
async def test_segmentation_watch_swallows_interrupt_and_threads_quiesce_event(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass halted for a store lock raises ``TerminalStageInterrupted``; the
    worker logs it and the watch keeps ticking (it resumes on a later tick).
    Every pass must also receive the Supervisor's shared ``_quiesce_event`` as
    ``stop_event`` — the channel a ``storage.lock`` uses to halt the windowed
    naming pass at its next safe boundary."""
    from screencap.terminal_stage import TerminalStageInterrupted

    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    recorder = _SegRecorder(raises=TerminalStageInterrupted("store lock"))
    _install_recorder(monkeypatch, recorder)

    sup = _make_supervisor(fake_engine_script)
    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    # Despite every pass raising the interrupt, the watch keeps firing.
    await _wait_until(lambda: len(recorder.calls) >= 3)
    assert sup._ambient_seg_task is not None and not sup._ambient_seg_task.done()
    assert sup.ambient_state()["active"] is True
    # Each pass received the SAME shared quiesce flag, by identity.
    assert recorder.stop_events
    assert all(ev is sup._quiesce_event for ev in recorder.stop_events)

    await sup.shutdown()


# ---------------------------------------------------------------------------
# Ambient DISABLED → the watch is never started and no pass runs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_watch_when_ambient_disabled(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SCREENCAP_AMBIENT_ENABLED", raising=False)
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "0")
    recorder = _SegRecorder()
    _install_recorder(monkeypatch, recorder)

    sup = _make_supervisor(fake_engine_script)
    await sup.reconcile_orphans()
    # Give the (non-existent) watch ample time to fire had it been started.
    await asyncio.sleep(0.15)

    assert sup._ambient_seg_task is None
    assert recorder.calls == []
    assert sup.ambient_state()["active"] is False

    await sup.shutdown()
