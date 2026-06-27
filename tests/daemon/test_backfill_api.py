"""SCR-178 U5 — daemon backfill verbs, progress events, idle-shutdown deferral.

Drives ``/v0/backfill.start`` / ``backfill.status`` / ``backfill.cancel`` against
the in-process ASGI app with a FAKE engine (``run_backfill`` monkeypatched), so no
real OCR runs. Each test holds ONE ``build_app()`` instance so ``app.state`` (the
single ``BackfillJob``) persists across requests, mirroring a live daemon.
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest

from screencap.backfill.ledger import BackfillLedger, RunState, UnitStatus
from screencap.daemon import schema
from screencap.daemon._idle_shutdown import _daemon_is_busy
from screencap.daemon.backfill_job import (
    EVENT_BACKFILL_PROGRESS,
    BackfillJob,
    should_auto_resume,
)


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _build_app():
    from screencap.daemon.app import build_app

    return build_app()


# ---------------------------------------------------------------------------
# Fake engines (injected via monkeypatch of run_backfill).
# ---------------------------------------------------------------------------


def _make_summary(state: RunState, *, done=2, skipped=0, failed=0, total=2, rows=5):
    from screencap.backfill.engine import BackfillSummary

    return BackfillSummary(
        state=state,
        done=done,
        skipped=skipped,
        failed=failed,
        total=total,
        rows_written=rows,
    )


def _fast_completed_engine(
    *, stop_event, progress_cb, ledger, **_kw
):
    """Seed a tiny closed set, emit two progress ticks, return COMPLETED."""
    ledger.seed([("recA", 0), ("recB", 0)])
    ledger.set_run_state(RunState.RUNNING)
    ledger.mark("recA", 0, UnitStatus.DONE, rows_written=3)
    if progress_cb is not None:
        progress_cb(1, 0, 0, 2, 0)
    ledger.mark("recB", 0, UnitStatus.DONE, rows_written=2)
    if progress_cb is not None:
        progress_cb(2, 0, 0, 2, 1)
    ledger.set_run_state(RunState.COMPLETED)
    return _make_summary(RunState.COMPLETED)


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_runs_and_status_reaches_completed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "screencap.backfill.engine.run_backfill", _fast_completed_engine
    )
    app = _build_app()
    # Point the job's ledger at a tmp DB so the test never touches the real one.
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    async with _client(app) as client:
        # Subscribe BEFORE start so we observe the initial progress events
        # (late-listener replay safety — mirror the eventbus replay doc).
        cursor = app.state.event_bus.current_cursor()

        resp = await client.post("/v0/backfill.start", json={})
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["schema_version"] == schema._BACKFILL_API_VERSION
        # The opaque ordinal + counts are present; state is running or already
        # terminal depending on scheduling.
        assert set(payload) >= {
            "state", "done", "skipped", "failed", "total", "current_unit_index",
        }

        # Drain to terminal.
        final = await _poll_until_terminal(client)
        assert final["state"] == RunState.COMPLETED.value
        assert final["done"] == 2
        assert final["total"] == 2

        # Progress events were published on the bus (replay from cursor).
        events = await _drain_events(app, since=cursor)
        progress = [e for e in events if e.get("type") == EVENT_BACKFILL_PROGRESS]
        assert progress, "expected backfill.progress events on the bus"
        terminal = [e for e in events if e.get("type") == "backfill.completed"]
        assert terminal, "expected a terminal backfill.completed event"


# ---------------------------------------------------------------------------
# Idempotency: a second start while running returns status, no second task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_start_is_idempotent(monkeypatch, tmp_path):
    gate = threading.Event()
    spawn_count = {"n": 0}

    def _blocking_engine(*, stop_event, progress_cb, ledger, **_kw):
        spawn_count["n"] += 1
        ledger.seed([("rec", 0)])
        ledger.set_run_state(RunState.RUNNING)
        if progress_cb is not None:
            progress_cb(0, 0, 0, 1, 0)
        # Block until the test releases (or cancel/stop is signalled).
        while not gate.is_set() and not stop_event.is_set():
            time.sleep(0.005)
        ledger.set_run_state(RunState.COMPLETED)
        return _make_summary(RunState.COMPLETED, done=1, total=1)

    monkeypatch.setattr("screencap.backfill.engine.run_backfill", _blocking_engine)
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    async with _client(app) as client:
        r1 = await client.post("/v0/backfill.start", json={})
        assert r1.status_code == 200
        await _wait_running(client)

        # Second start while running: returns status, does NOT spawn a 2nd task.
        r2 = await client.post("/v0/backfill.start", json={})
        assert r2.status_code == 200
        assert r2.json()["state"] == RunState.RUNNING.value

        gate.set()
        final = await _poll_until_terminal(client)
        assert final["state"] == RunState.COMPLETED.value

    assert spawn_count["n"] == 1, "a second start must not spawn a second engine run"


# ---------------------------------------------------------------------------
# Cancel: stop flag flips status to cancelled; ledger reflects partial progress.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_transitions_to_cancelled(monkeypatch, tmp_path):
    def _cancellable_engine(*, stop_event, progress_cb, ledger, **_kw):
        ledger.seed([("rec", 0), ("rec", 1)])
        ledger.set_run_state(RunState.RUNNING)
        ledger.mark("rec", 0, UnitStatus.DONE, rows_written=4)
        if progress_cb is not None:
            progress_cb(1, 0, 0, 2, 0)
        # Wait for the cancel signal, then return CANCELLED with the 2nd unit
        # still PENDING (partial progress preserved on the ledger).
        while not stop_event.is_set():
            time.sleep(0.005)
        ledger.set_run_state(RunState.CANCELLED)
        return _make_summary(RunState.CANCELLED, done=1, total=2, rows=4)

    monkeypatch.setattr("screencap.backfill.engine.run_backfill", _cancellable_engine)
    app = _build_app()
    led = BackfillLedger(tmp_path / "ledger.db")
    app.state.backfill_job = BackfillJob(app.state.event_bus, ledger=led)

    async with _client(app) as client:
        await client.post("/v0/backfill.start", json={})
        await _wait_running(client)

        rc = await client.post("/v0/backfill.cancel", json={})
        assert rc.status_code == 200

        final = await _poll_until_terminal(client)
        assert final["state"] == RunState.CANCELLED.value
        assert final["done"] == 1
        assert final["total"] == 2

    # Ledger reflects partial progress: unit 0 DONE, unit 1 still PENDING.
    assert led.unit_status("rec", 0) == UnitStatus.DONE
    assert led.unit_status("rec", 1) == UnitStatus.PENDING


# ---------------------------------------------------------------------------
# Liveness: _daemon_is_busy True while running, False once terminal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_is_busy_while_running(monkeypatch, tmp_path):
    gate = threading.Event()

    def _engine(*, stop_event, progress_cb, ledger, **_kw):
        ledger.seed([("rec", 0)])
        ledger.set_run_state(RunState.RUNNING)
        if progress_cb is not None:
            progress_cb(0, 0, 0, 1, 0)
        while not gate.is_set():
            time.sleep(0.005)
        ledger.set_run_state(RunState.COMPLETED)
        return _make_summary(RunState.COMPLETED, done=1, total=1)

    monkeypatch.setattr("screencap.backfill.engine.run_backfill", _engine)
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    assert _daemon_is_busy(app) is False  # nothing running yet

    async with _client(app) as client:
        await client.post("/v0/backfill.start", json={})
        await _wait_running(client)
        assert _daemon_is_busy(app) is True  # in-flight run defers idle-shutdown

        gate.set()
        await _poll_until_terminal(client)

    assert _daemon_is_busy(app) is False  # terminal → no longer busy


# ---------------------------------------------------------------------------
# Error: engine exception → terminal failed status, daemon stays up.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_exception_yields_failed_status(monkeypatch, tmp_path):
    def _boom(*, stop_event, progress_cb, ledger, **_kw):
        raise RuntimeError("engine blew up")

    monkeypatch.setattr("screencap.backfill.engine.run_backfill", _boom)
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    async with _client(app) as client:
        r = await client.post("/v0/backfill.start", json={})
        assert r.status_code == 200  # start itself succeeds; failure is async

        final = await _poll_until_terminal(client)
        assert final["state"] == "failed"

        # Daemon stays up: another verb still answers 200.
        again = await client.get("/v0/backfill.status")
        assert again.status_code == 200
    assert _daemon_is_busy(app) is False


@pytest.mark.asyncio
async def test_status_with_no_run_is_idle(tmp_path):
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )
    async with _client(app) as client:
        r = await client.get("/v0/backfill.status")
        assert r.status_code == 200
        assert r.json()["state"] == "idle"


# ---------------------------------------------------------------------------
# Replay: a subscriber joining after start still gets buffered progress events.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_subscriber_replays_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "screencap.backfill.engine.run_backfill", _fast_completed_engine
    )
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    async with _client(app) as client:
        cursor_before = app.state.event_bus.current_cursor()
        await client.post("/v0/backfill.start", json={})
        await _poll_until_terminal(client)

        # Late subscriber (joins after start) still sees the buffered events.
        events = await _drain_events(app, since=cursor_before)
        types = [e.get("type") for e in events]
        assert EVENT_BACKFILL_PROGRESS in types
        assert "backfill.completed" in types


# ---------------------------------------------------------------------------
# Auto-resume rule (encoded predicate, no boot wiring in U5).
# ---------------------------------------------------------------------------


def test_should_auto_resume_only_for_paused_with_pending(tmp_path):
    # CANCELLED with pending units → must NOT auto-resume.
    cancelled = BackfillLedger(tmp_path / "cancelled.db")
    cancelled.seed([("rec", 0)])
    cancelled.set_run_state(RunState.CANCELLED)
    assert should_auto_resume(cancelled) is False

    # PAUSED with a pending unit → MAY auto-resume.
    paused = BackfillLedger(tmp_path / "paused.db")
    paused.seed([("rec", 0)])
    paused.set_run_state(RunState.PAUSED)
    assert should_auto_resume(paused) is True

    # PAUSED but fully terminal → nothing to resume.
    done = BackfillLedger(tmp_path / "done.db")
    done.seed([("rec", 0)])
    done.mark("rec", 0, UnitStatus.DONE)
    done.set_run_state(RunState.PAUSED)
    assert should_auto_resume(done) is False

    # COMPLETED never resumes.
    completed = BackfillLedger(tmp_path / "completed.db")
    completed.seed([("rec", 0)])
    completed.mark("rec", 0, UnitStatus.DONE)
    completed.set_run_state(RunState.COMPLETED)
    assert should_auto_resume(completed) is False


# ---------------------------------------------------------------------------
# Privacy (R9): no event payload carries a recording directory name.
# ---------------------------------------------------------------------------


# SCR-178 R9: this is the daemon-layer guard that a recording directory name never
# crosses the EventBus to a same-EUID subscriber (incl. MCP). CI runs only
# `pytest -m privacy`, so mark it so the guard actually runs on CI rather than
# rotting dev-only (the engine-layer name-free guard is separately marked). Uses a
# fake engine (no Vision), so it runs on both the Vision-backed and Vision-free lanes.
@pytest.mark.privacy
@pytest.mark.asyncio
async def test_no_event_payload_contains_recording_name(monkeypatch, tmp_path):
    secret_name = "super-secret-recording-dirname"

    def _engine(*, stop_event, progress_cb, ledger, **_kw):
        # The engine knows the dir name internally but must surface only the
        # opaque ordinal through progress_cb (we pass unit_index, not the name).
        ledger.seed([(secret_name, 0)])
        ledger.set_run_state(RunState.RUNNING)
        ledger.mark(secret_name, 0, UnitStatus.DONE, rows_written=1)
        if progress_cb is not None:
            progress_cb(1, 0, 0, 1, 0)
        ledger.set_run_state(RunState.COMPLETED)
        return _make_summary(RunState.COMPLETED, done=1, total=1, rows=1)

    monkeypatch.setattr("screencap.backfill.engine.run_backfill", _engine)
    app = _build_app()
    app.state.backfill_job = BackfillJob(
        app.state.event_bus, ledger=BackfillLedger(tmp_path / "ledger.db")
    )

    async with _client(app) as client:
        cursor = app.state.event_bus.current_cursor()
        await client.post("/v0/backfill.start", json={})
        final = await _poll_until_terminal(client)
        # The status response must not carry the name either.
        assert secret_name not in str(final)

        events = await _drain_events(app, since=cursor)
        assert events, "expected events on the bus"
        for ev in events:
            assert secret_name not in str(ev), f"recording name leaked: {ev}"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

_TERMINAL_STATES = {
    RunState.COMPLETED.value,
    RunState.PAUSED.value,
    RunState.CANCELLED.value,
    "failed",
}


async def _wait_running(client: httpx.AsyncClient, *, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get("/v0/backfill.status")
        if r.json()["state"] == RunState.RUNNING.value:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("backfill did not reach running state")


async def _poll_until_terminal(client: httpx.AsyncClient, *, timeout=3.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        r = await client.get("/v0/backfill.status")
        last = r.json()
        if last["state"] in _TERMINAL_STATES:
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"backfill did not reach a terminal state; last={last}")


async def _drain_events(app, *, since: int) -> list[dict]:
    """Subscribe with replay and drain everything currently queued."""
    bus = app.state.event_bus
    sub = await bus.subscribe(since=since)
    out: list[dict] = []
    try:
        while True:
            try:
                out.append(sub.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
    finally:
        await bus.remove(sub)
    return out
