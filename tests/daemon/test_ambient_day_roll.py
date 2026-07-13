"""SCR-214 U3: day-boundary ambient recording roll.

Covers the supervisor rolling the always-on ambient stream at the LOCAL calendar
day boundary — a RECORDING-level stop→start into the next ``ambient-YYYYMMDD``
dir (KTD1, R5):

* crossing local midnight closes day N and opens day N+1 with the right names,
* sleep-across-midnight then wake rolls on the FIRST post-wake tick (wake-safety:
  the roll is driven by an observed date change, not a midnight timer),
* a chunk open at the boundary is force-closed under exactly ONE day (the stop
  finalizes day N before day N+1 opens — no straddling chunk),
* day N is driven through its normal terminal path (a ``recording_finalized`` for
  the day-N container fires — the final segmentation pass, not reimplemented),
* a roll is NOT treated as an engine crash — it never trips the U2 re-arm/backoff
  ceiling.

The clock is INJECTED (``FakeClock``) so midnight crossing is deterministic; the
engine subprocess is a fake script (no real capture). Recordings/base dirs are
isolated by the autouse fixtures in ``tests/daemon/conftest.py``.
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

# Two adjacent LOCAL calendar days at safe times (mid-June dodges DST edges).
# Epochs are built with ``time.mktime`` (local tz) and the expected dir names are
# derived with the SAME ``time.strftime``/``time.localtime`` the code uses, so the
# assertions hold regardless of the test host's timezone.
_DAY_N_EPOCH = time.mktime((2026, 6, 15, 12, 0, 0, 0, 0, -1))  # 2026-06-15 12:00
_DAY_N1_EPOCH = time.mktime((2026, 6, 16, 0, 30, 0, 0, 0, -1))  # 2026-06-16 00:30
_SLEEP_EPOCH = time.mktime((2026, 6, 15, 23, 59, 0, 0, 0, -1))  # 2026-06-15 23:59
_WAKE_EPOCH = time.mktime((2026, 6, 16, 8, 0, 0, 0, 0, -1))  # 2026-06-16 08:00


def _day_name(epoch: float) -> str:
    return time.strftime("ambient-%Y%m%d", time.localtime(epoch))


DAY_N = _day_name(_DAY_N_EPOCH)
DAY_N1 = _day_name(_DAY_N1_EPOCH)


# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained; mirrors tests/daemon/test_supervisor_ambient.py)
# ---------------------------------------------------------------------------


class FakeClock:
    """A settable wall clock injected into the supervisor for deterministic rolls."""

    def __init__(self, epoch: float) -> None:
        self._epoch = float(epoch)

    def __call__(self) -> float:
        return self._epoch

    def set(self, epoch: float) -> None:
        self._epoch = float(epoch)


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


def _track_spawn_names(supervisor, monkeypatch) -> list[str]:
    """Record the resulting recording name of every ``spawn`` (ambient + roll).

    ``_spawn_ambient`` / ``_roll_ambient_day`` route through ``self.spawn``, so the
    instance-attribute override is picked up; each entry is the dir name the spawn
    landed in (read from ``current_session`` right after spawn returns).
    """
    names: list[str] = []
    real_spawn = supervisor.spawn

    async def wrapped(request):
        result = await real_spawn(request)
        session = supervisor.current_session()
        if session and session.get("recording_name"):
            names.append(session["recording_name"])
        return result

    monkeypatch.setattr(supervisor, "spawn", wrapped)
    return names


def _make_supervisor(script: Path, *, start_gate=_pass_gate, **kw):
    from screencap.daemon.supervisor import Supervisor

    defaults = dict(
        engine_command_factory=_factory(script),
        reconcile_on_init=False,
        poll_interval=0.02,
        startup_timeout=2.0,
        stop_timeout=1.0,
        start_gate=start_gate,
        ambient_day_tick_interval=0.02,
    )
    defaults.update(kw)
    return Supervisor(EventBus(), **defaults)


# ---------------------------------------------------------------------------
# Crossing local midnight rolls day N → day N+1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crossing_local_midnight_rolls_to_next_day(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    clock = FakeClock(_DAY_N_EPOCH)
    sup = _make_supervisor(fake_engine_script, clock=clock)
    names = _track_spawn_names(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    # The always-on stream opened in day N's per-day container.
    assert names == [DAY_N]
    assert sup.current_session()["recording_name"] == DAY_N

    # Advance the injected clock past local midnight into day N+1.
    clock.set(_DAY_N1_EPOCH)

    # The next tick observes the advanced date and rolls to day N+1.
    await _wait_until(lambda: len(names) == 2)
    assert names == [DAY_N, DAY_N1]
    await _wait_until(
        lambda: sup.current_session() is not None
        and sup.current_session()["recording_name"] == DAY_N1
    )
    assert sup.ambient_state()["active"] is True
    # Rolls at most once per boundary: no re-roll while the day is stable.
    await asyncio.sleep(0.15)
    assert names == [DAY_N, DAY_N1]

    await sup.shutdown()


# ---------------------------------------------------------------------------
# Sleep-across-midnight then wake rolls on the first post-wake tick (wake-safety)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_across_midnight_rolls_on_wake(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon is suspended while the Mac sleeps, so NO tick fires at the real
    midnight — the clock jumps 23:59 → 08:00 next day between ticks. The first
    post-wake tick sees the advanced date and performs the (single) missed roll,
    proving the roll is wake-safe without any midnight timer."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    clock = FakeClock(_SLEEP_EPOCH)
    sup = _make_supervisor(fake_engine_script, clock=clock)
    names = _track_spawn_names(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    assert names == [DAY_N]  # 2026-06-15 23:59 → day N

    # Let a few ticks run at 23:59 (still day N) to prove they do NOT roll.
    await asyncio.sleep(0.1)
    assert names == [DAY_N]

    # Simulate suspend/resume: the clock jumps straight across midnight to 08:00.
    clock.set(_WAKE_EPOCH)

    await _wait_until(lambda: len(names) == 2)
    assert names == [DAY_N, DAY_N1]  # rolled to 2026-06-16 on the post-wake tick
    await _wait_until(
        lambda: sup.current_session() is not None
        and sup.current_session()["recording_name"] == DAY_N1
    )

    await sup.shutdown()


# ---------------------------------------------------------------------------
# A chunk open at the boundary is force-closed to a single day
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boundary_chunk_force_closed_to_single_day(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roll STOPS day N (its normal terminal path force-closes the open chunk
    via ChunkedVideoWriter.close(), attributing it to day N's dir) BEFORE day N+1
    opens. So any chunk open across the boundary belongs to exactly one per-day
    container — it cannot straddle two day dirs. With a mocked engine this is the
    honest observable: distinct day dirs + stop(day N) strictly happens-before
    spawn(day N+1)."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    clock = FakeClock(_DAY_N_EPOCH)
    sup = _make_supervisor(fake_engine_script, clock=clock)

    events: list[tuple[str, str | None]] = []
    real_stop = sup.stop
    real_spawn = sup.spawn

    async def wrapped_stop(*a, **k):
        session = sup.current_session()
        events.append(("stop", session["recording_name"] if session else None))
        return await real_stop(*a, **k)

    async def wrapped_spawn(request):
        result = await real_spawn(request)
        session = sup.current_session()
        events.append(("spawn", session["recording_name"] if session else None))
        return result

    monkeypatch.setattr(sup, "stop", wrapped_stop)
    monkeypatch.setattr(sup, "spawn", wrapped_spawn)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    clock.set(_DAY_N1_EPOCH)
    await _wait_until(lambda: ("spawn", DAY_N1) in events)

    # The two days occupy DISTINCT per-day containers.
    assert DAY_N != DAY_N1
    # Day N was stopped (open chunk force-closed under day N) BEFORE day N+1 opened.
    assert ("stop", DAY_N) in events
    stop_idx = events.index(("stop", DAY_N))
    spawn_idx = events.index(("spawn", DAY_N1))
    assert stop_idx < spawn_idx

    await sup.shutdown()


# ---------------------------------------------------------------------------
# Day N finalizes (normal terminal path) on the roll
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_n_finalizes_on_roll(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    clock = FakeClock(_DAY_N_EPOCH)
    sup = _make_supervisor(fake_engine_script, clock=clock)
    names = _track_spawn_names(sup, monkeypatch)

    sub = await sup._bus.subscribe()
    collected: list[dict] = []

    async def collector() -> None:
        try:
            while True:
                collected.append(await sub.queue.get())
        except asyncio.CancelledError:
            return

    ctask = asyncio.create_task(collector())
    try:
        await sup.reconcile_orphans()
        await _wait_until(lambda: sup.ambient_state()["active"] is True)

        clock.set(_DAY_N1_EPOCH)
        await _wait_until(lambda: len(names) == 2)

        # Day N was driven through its NORMAL terminal path on the roll: a
        # recording_finalized for the day-N container fired (finalize → the final
        # segmentation pass the terminal stage owns — not reimplemented here).
        await _wait_until(
            lambda: any(
                e.get("type") == "recording_finalized" and e.get("name") == DAY_N
                for e in collected
            )
        )
    finally:
        ctask.cancel()
        await sup._bus.remove(sub)
        await sup.shutdown()


# ---------------------------------------------------------------------------
# A roll is NOT counted as an engine crash (no re-arm/backoff)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_roll_is_not_treated_as_crash(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The roll's intentional stop must NOT engage the U2 crash-loop backoff. The
    exit funnel's re-arm/backoff path is skipped entirely (the roll owns the
    respawn), so retry_count stays 0 and no degraded state surfaces."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    clock = FakeClock(_DAY_N_EPOCH)
    sup = _make_supervisor(fake_engine_script, clock=clock)
    names = _track_spawn_names(sup, monkeypatch)

    rearm_calls = {"n": 0}
    real_rearm = sup._schedule_ambient_rearm

    def counting_rearm(rc, run_duration):
        rearm_calls["n"] += 1
        return real_rearm(rc, run_duration)

    monkeypatch.setattr(sup, "_schedule_ambient_rearm", counting_rearm)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    clock.set(_DAY_N1_EPOCH)
    await _wait_until(lambda: len(names) == 2)
    await _wait_until(
        lambda: sup.current_session() is not None
        and sup.current_session()["recording_name"] == DAY_N1
    )

    # The roll's stop was NOT routed into the exit funnel's re-arm/backoff.
    assert rearm_calls["n"] == 0
    state = sup.ambient_state()
    assert state["active"] is True
    assert state["retry_count"] == 0
    assert state["degraded"] is None
    # No thrash: exactly the initial spawn + the single roll spawn.
    await asyncio.sleep(0.15)
    assert len(names) == 2

    await sup.shutdown()
