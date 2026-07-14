"""SCR-214 U2: supervised always-on ambient capture.

Covers the daemon-side always-on ambient lifecycle:

* auto-start after reconcile clears (gated on ``get_ambient_enabled``),
* the SHARED start gate (permission + paywall) — a denial surfaces a degraded
  state and does NOT spawn,
* re-arm after the engine exits, with exponential backoff + a retry ceiling so a
  crash-looping engine surfaces a degraded state instead of thrash-respawning,
* the idle-shutdown watchdog treating an active/pending ambient as non-idle.

The engine subprocess is a fake script (no real capture); the ``start_gate`` is
injected per test. Recordings/base dirs are isolated by the autouse
``_isolate_recordings_and_run_dir`` fixture in ``tests/daemon/conftest.py``.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable

import pytest

from screencap.daemon import errors, schema
from screencap.daemon.event_bus import EventBus

_API_V = schema._RECORDING_START_API_VERSION


# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained; mirrors tests/daemon/test_supervisor.py)
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
    """Emit ``started`` then sleep; ``FAKE_ENGINE_MODE=exit_nonzero`` crashes
    immediately (rapid-crash simulation); SIGTERM finalizes + exits 0."""
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
            if os.environ.get("FAKE_ENGINE_MODE") == "exit_nonzero":
                raise SystemExit(7)
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
    """A start gate that always allows (granted permission + entitled)."""
    return None


def _count_spawns(supervisor, monkeypatch) -> list:
    """Wrap ``supervisor.spawn`` to record every (ambient + explicit) call.

    ``_spawn_ambient`` calls ``self.spawn`` so the instance-attribute override is
    picked up; each entry is the ``RecordingStartRequest`` that was spawned.
    """
    calls: list = []
    real_spawn = supervisor.spawn

    async def counting(request):
        calls.append(request)
        return await real_spawn(request)

    monkeypatch.setattr(supervisor, "spawn", counting)
    return calls


def _make_supervisor(script: Path, *, start_gate=_pass_gate, **kw):
    from screencap.daemon.supervisor import Supervisor

    defaults = dict(
        engine_command_factory=_factory(script),
        reconcile_on_init=False,
        poll_interval=0.02,
        startup_timeout=2.0,
        stop_timeout=1.0,
        start_gate=start_gate,
    )
    defaults.update(kw)
    return Supervisor(EventBus(), **defaults)


# ---------------------------------------------------------------------------
# Auto-start gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambient_disabled_no_autospawn(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient off (the default) → reconcile clears with no ambient spawn and no
    supervision task scheduled."""
    # ambient env explicitly unset → get_ambient_enabled() is False
    monkeypatch.delenv("SCREENCAP_AMBIENT_ENABLED", raising=False)
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await asyncio.sleep(0.2)

    assert calls == []
    assert sup._ambient_spawn_task is None
    assert sup.ambient_supervision_active() is False
    await sup.shutdown()


@pytest.mark.asyncio
async def test_ambient_enabled_spawns_exactly_one_via_shared_gate(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient on → reconcile clears and auto-starts EXACTLY one ambient
    recording, routed through the shared start gate."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")

    gate_calls = {"n": 0}

    async def counting_gate() -> None:
        gate_calls["n"] += 1

    sup = _make_supervisor(fake_engine_script, start_gate=counting_gate)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: len(calls) == 1)
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    # Exactly one spawn, and it was gated first (KTD8) with a local-only ambient
    # request whose dir is the deterministic per-day container.
    assert len(calls) == 1
    assert gate_calls["n"] == 1
    assert calls[0].ambient is True
    assert calls[0].cloud_intent is False
    session = sup.current_session()
    assert session is not None
    assert session["recording_name"].startswith("ambient-")
    assert sup.ambient_state()["degraded"] is None
    # No thrash: it stays at exactly one while the engine runs.
    await asyncio.sleep(0.2)
    assert len(calls) == 1
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Gate denials surface a degraded state and never spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permission_denied_no_spawn_surfaces_degraded(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")

    async def deny_permission() -> None:
        raise errors.PermissionRequiredError(
            ["screen_recording"], schema_version=_API_V
        )

    sup = _make_supervisor(fake_engine_script, start_gate=deny_permission)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["degraded"] is not None)

    assert calls == []
    assert sup.ambient_state()["active"] is False
    assert errors.PERMISSION_REQUIRED in sup.ambient_state()["degraded"]
    # A denied ambient holds nothing pending → does not keep the daemon alive.
    assert sup.ambient_supervision_active() is False
    await sup.shutdown()


@pytest.mark.asyncio
async def test_paywall_blocked_no_spawn_surfaces_degraded(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")

    async def deny_paywall() -> None:
        raise errors.SubscriptionRequiredError(schema_version=_API_V)

    sup = _make_supervisor(fake_engine_script, start_gate=deny_paywall)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_state()["degraded"] is not None)

    assert calls == []
    assert sup.ambient_state()["active"] is False
    assert errors.SUBSCRIPTION_REQUIRED in sup.ambient_state()["degraded"]
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Re-arm: a single (clean) exit re-arms exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_engine_exit_re_arms_exactly_once(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration proof (mocks alone can't show this): a booted, ambient-enabled
    daemon auto-spawns one recording, and after the engine exits it re-arms
    exactly once — then stays put (no thrash)."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_BASE_DELAY", "0.01")
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: len(calls) == 1)
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    # Simulate the engine exiting on its own (SIGTERM → finalize → exit 0). This
    # is NOT an operator stop(): _stopping stays False, so the exit funnel drives
    # the re-arm.
    proc = sup._proc
    assert proc is not None
    proc.terminate()

    await _wait_until(lambda: len(calls) == 2)
    # Re-armed exactly once; the re-armed engine runs → no further re-arm.
    await asyncio.sleep(0.3)
    assert len(calls) == 2
    assert sup.ambient_state()["active"] is True
    assert sup.ambient_state()["degraded"] is None
    assert sup.ambient_state()["retry_count"] == 0  # clean exit reset the budget
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Re-arm: repeated rapid crashes back off and STOP at the ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapid_crash_loop_backs_off_and_stops_at_ceiling(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently crash-looping engine must NOT thrash-respawn: exponential
    backoff engages and re-arm stops at the retry ceiling, surfacing a degraded
    state. Total spawns = 1 initial + max_retries re-arms."""
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    monkeypatch.setenv("FAKE_ENGINE_MODE", "exit_nonzero")  # every spawn crashes
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_BASE_DELAY", "0.01")
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_MAX_DELAY", "0.05")
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_MAX_RETRIES", "3")
    # A high quiescence window means every (immediate) crash counts as "rapid".
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_QUIESCENCE", "100")

    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(
        lambda: sup.ambient_state()["degraded"] is not None, timeout=10.0
    )

    # 1 initial + 3 re-arms, then the 4th rapid crash hits the ceiling and stops.
    assert len(calls) == 4
    # No thrash after the ceiling.
    await asyncio.sleep(0.4)
    assert len(calls) == 4
    assert sup.ambient_state()["active"] is False
    assert sup.ambient_supervision_active() is False  # nothing pending
    assert "ceiling" in sup.ambient_state()["degraded"]
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Deferred auto-start: a held lock defers ambient, and the lock-holder's exit
# (NOT a daemon restart) eventually starts it — exactly once (reliability fix).
# ---------------------------------------------------------------------------


def _spawn_recorder(supervisor, monkeypatch) -> list:
    """Wrap ``supervisor.spawn`` to record every attempt as ``(request, ok)``.

    Unlike ``_count_spawns`` (which records the attempt BEFORE calling through, so
    a deferred spawn that raises ``LockContendedError`` still counts), this records
    whether the underlying spawn SUCCEEDED — so a deferred (raised) ambient attempt
    is distinguishable from an ambient recording that actually started.
    """
    attempts: list = []
    real_spawn = supervisor.spawn

    async def recording(request):
        try:
            result = await real_spawn(request)
        except BaseException:
            attempts.append((request, False))
            raise
        attempts.append((request, True))
        return result

    monkeypatch.setattr(supervisor, "spawn", recording)
    return attempts


@pytest.mark.asyncio
async def test_deferred_ambient_starts_when_explicit_lock_holder_exits(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reliability: ambient DEFERRED by a held lock starts when the lock frees.

    An explicit (non-ambient) recording holds the recording lock, so the ambient
    auto-start (``_maybe_autostart_ambient`` — exactly what reconcile calls once it
    clears) DEFERS on ``LockContendedError``: no ambient recording starts, but
    ambient is NOT permanently degraded. Before the fix, the exit-funnel re-arm
    only fired for ambient's OWN exit (``was_ambient``), so when the explicit
    lock-holder exited (``was_ambient=False``) ambient stayed silently down until
    the next daemon restart. Now the explicit recording's exit auto-starts the
    deferred ambient — exactly once, with no double-spawn.
    """
    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    sup = _make_supervisor(fake_engine_script)
    attempts = _spawn_recorder(sup, monkeypatch)

    def _ambient_started() -> int:
        return sum(1 for req, ok in attempts if ok and getattr(req, "ambient", False))

    def _ambient_deferred() -> int:
        return sum(
            1 for req, ok in attempts if not ok and getattr(req, "ambient", False)
        )

    # An explicit recording claims the lock first.
    await sup.spawn(schema.RecordingStartRequest(name="explicit"))
    assert sup.current_session()["recording_name"] == "explicit"

    # The ambient auto-start fires (as it would at the tail of reconcile) while the
    # explicit recording holds the lock → it DEFERS, starting nothing.
    sup._maybe_autostart_ambient()
    assert sup._ambient_spawn_task is not None
    await sup._ambient_spawn_task  # the deferred spawn returns cleanly (LockContended)

    assert _ambient_started() == 0, "ambient must not start while the lock is held"
    assert _ambient_deferred() == 1, "the deferred attempt was made and swallowed"
    state = sup.ambient_state()
    assert state["active"] is False
    assert state["degraded"] is None, "a lock-defer is NOT a permanent degrade"
    # The explicit recording is still the live session.
    assert sup.current_session()["recording_name"] == "explicit"

    # The explicit lock-holder exits → the lock frees → ambient auto-starts.
    await sup.stop()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    assert _ambient_started() == 1, "ambient auto-starts exactly once after the exit"
    session = sup.current_session()
    assert session is not None and session["recording_name"].startswith("ambient-")
    assert sup.ambient_state()["degraded"] is None

    # No double-spawn / thrash: it stays at exactly one while the engine runs.
    await asyncio.sleep(0.2)
    assert _ambient_started() == 1
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Idle-shutdown treats an active/pending ambient as non-idle
# ---------------------------------------------------------------------------


class _AmbientBusySupervisor:
    """A fake supervisor that is non-idle ONLY via the ambient predicate (no
    active session, no inflight resume) — proves the new predicate alone holds
    the daemon alive."""

    def current_session(self):
        return None

    def has_inflight_resume(self) -> bool:
        return False

    def ambient_supervision_active(self) -> bool:
        return True


class _AppShim:
    def __init__(self) -> None:
        self.state = type("S", (), {})()


class _FakeBus:
    def subscriber_count(self) -> int:
        return 0


def test_idle_shutdown_defers_while_ambient_supervised() -> None:
    from screencap.daemon._idle_shutdown import _daemon_is_busy

    app = _AppShim()
    app.state.event_bus = _FakeBus()
    app.state.supervisor = _AmbientBusySupervisor()
    assert _daemon_is_busy(app) is True


@pytest.mark.asyncio
async def test_idle_watchdog_does_not_fire_while_ambient_active(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: with a real ambient recording running, the idle watchdog
    never fires even though the idle deadline has elapsed."""
    from screencap.daemon._idle_shutdown import run_watchdog

    monkeypatch.setenv("SCREENCAP_AMBIENT_ENABLED", "1")
    sup = _make_supervisor(fake_engine_script)
    _count_spawns(sup, monkeypatch)

    await sup.reconcile_orphans()
    await _wait_until(lambda: sup.ambient_supervision_active() is True)

    app = _AppShim()
    app.state.event_bus = _FakeBus()
    app.state.supervisor = sup
    app.state.idle_shutdown_seconds = 0.05
    app.state.idle_last_activity = time.monotonic() - 1.0  # already past the deadline

    fired = asyncio.Event()
    task = asyncio.create_task(
        run_watchdog(app, request_shutdown=lambda: fired.set(), poll_interval_s=0.01)
    )
    await asyncio.sleep(0.15)
    assert not fired.is_set()
    task.cancel()
    await asyncio.wait_for(task, timeout=0.5)
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Local-only invariant on the internal ambient request (KTD2, privacy)
# ---------------------------------------------------------------------------


@pytest.mark.privacy
def test_internal_ambient_request_is_local_only(
    fake_engine_script: Path,
) -> None:
    """KTD2: the internal ambient request is hard-pinned local-only regardless of
    upload_default — no cloud_intent, no force_mode — so the always-on stream can
    never route to an upload seam."""
    from screencap.daemon.event_bus import EventBus
    from screencap.daemon.supervisor import Supervisor

    sup = Supervisor(EventBus(), reconcile_on_init=False)
    request = sup._build_ambient_request()
    assert request.ambient is True
    assert request.cloud_intent is False
    assert request.force_mode is None
    assert request.keep_local is True
