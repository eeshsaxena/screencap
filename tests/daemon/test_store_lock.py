"""SCR-258 U9 — lock/unlock verbs + the quiescence engine (KTD-15/16/20/23).

``storage.lock`` seals a LIVE store safely in bounded time (KTD-15 order:
refuse-flag FIRST → stop → signal readers → quiesce → detach → seal), and
``storage.unlock`` remounts + reconciles. These tests are Vision-free and mock
the container detach / mount seams — **no real ``hdiutil`` runs**. Coverage maps
to the plan U9 test-scenarios list:

* AE2 arm / start-race: the refuse flag is set at ENTRY, so a concurrent
  ``recording.start`` is refused (typed, before any ``started`` signal), never
  spawned-then-killed; the handler stops the recording BEFORE detaching + sealing;
* AE7: an in-flight terminal-stage upload halts at a ledger-safe boundary via the
  ``stop_event`` and the store seals within the grace budget WITHOUT waiting for
  the upload; the ledger stays consistent (no false UPLOADED, no sentinel);
* ``store.locking`` is emitted BEFORE the detach window opens;
* lock with nothing active seals fast; a second lock is an idempotent no-op;
* a detach that fails past graceful+force → typed error, store stays
  mounted+unlocked, ``_store_locked`` cleared, sentinel NOT written;
* unlock with the Keychain locked → retryable in-band error, sentinel intact;
* lifecycle events ride ``/v0/events``; lock/unlock audit records carry peer
  provenance + outcome;
* force-detach only in lock (compact never forces — contrast).
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from screencap import container
from screencap.daemon import errors, schema
from screencap.daemon import store_lifecycle as sl
from screencap.daemon.app import (
    _LOCK_QUIESCE_GRACE_S,
    EVENT_STORE_LOCKED,
    EVENT_STORE_LOCKING,
    EVENT_STORE_UNLOCKED,
    build_app,
)
from screencap.daemon.store_lifecycle import StoreResolution, StoreState
from screencap.daemon.supervisor import Supervisor

pytestmark = pytest.mark.privacy


# The concrete quiescence grace budget this unit ships. Pinned here so a change to
# the value is a deliberate, reviewed edit (KTD-15 Outstanding Question).
_EXPECTED_GRACE_BUDGET_S = 5.0


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def container_on(monkeypatch):
    """Enable the container flag on top of the daemon conftest's base isolation."""
    import screencap.config as cfg

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    cfg.invalidate_config_cache()
    yield
    cfg.invalidate_config_cache()


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _build_app(store_state: StoreState = StoreState.MOUNTED) -> "tuple":
    """A lifespan-less app + a real (reconcile-free) Supervisor at ``store_state``."""
    app = build_app()
    app.state.store_state = store_state.value
    app.state.store_reason = None
    sup = Supervisor(app.state.event_bus, reconcile_on_init=False)
    sup.set_store_state(store_state)
    app.state.supervisor = sup
    return app, sup


class _DetachSpy:
    """Records ``container.detach`` calls (optionally raising) — no hdiutil."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._raises = raises

    def __call__(self, target, *, force=True, **kw):  # noqa: ANN001
        self.calls.append({"target": target, "force": force})
        if self._raises is not None:
            raise self._raises


async def _drain_events(bus, *, count: int, timeout: float = 2.0) -> list[str]:
    """Subscribe and collect up to ``count`` event ``type`` strings."""
    sub = await bus.subscribe()
    seen: list[str] = []
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while len(seen) < count:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                ev = await asyncio.wait_for(sub.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            seen.append(ev.get("type"))
    finally:
        await bus.remove(sub)
    return seen


# ---------------------------------------------------------------------------
# Grace-budget pin (KTD-15 Outstanding Question)
# ---------------------------------------------------------------------------


def test_grace_budget_is_the_pinned_value():
    assert _LOCK_QUIESCE_GRACE_S == _EXPECTED_GRACE_BUDGET_S


# ---------------------------------------------------------------------------
# Start-race / AE2 arm: refuse flag set at ENTRY (proof-first)
# ---------------------------------------------------------------------------


async def test_begin_lock_refuses_recording_start_before_stop():
    """RED→GREEN: without begin_lock, spawn proceeds; with it, spawn is refused
    with the typed error BEFORE any ``started`` signal (the start-race guard)."""
    app, sup = _build_app(StoreState.MOUNTED)

    # Baseline (RED control): a MOUNTED store does NOT refuse on the store flag —
    # spawn gets past the lock check (it fails later for lack of a real engine,
    # but crucially NOT with StoreLockedError).
    assert not sup.is_locked()

    # KTD-15 step 1: flip the refuse flag at lock ENTRY.
    sup.begin_lock()
    assert sup.is_locked()
    assert sup.is_lock_in_flight()  # pins the idle watchdog for the operation

    with pytest.raises(errors.StoreLockedError):
        await sup.spawn(
            schema.RecordingStartRequest(name="race", output_dir=None)
        )


async def test_lock_handler_stops_recording_before_detach_and_seal(
    container_on, monkeypatch,
):
    """AE2: the handler stops the active recording BEFORE it detaches + seals.

    A spy on the real Supervisor.stop proves stop() is invoked (in real life this
    finalizes the chunk, no partial loss) and that it lands before the detach.
    """
    app, sup = _build_app(StoreState.MOUNTED)
    order: list[str] = []

    async def _stop_spy(*a, **k):
        order.append("stop")
        return {"stopped": True, "final_state": "stopped"}

    monkeypatch.setattr(sup, "stop", _stop_spy)

    detach = _DetachSpy()

    def _detach(target, *, force=True, **kw):  # noqa: ANN001
        order.append("detach")
        detach(target, force=force)

    monkeypatch.setattr(container, "detach", _detach)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] and body["store_state"] == "locked"
    assert order == ["stop", "detach"], "stop must precede detach"
    assert detach.calls and detach.calls[0]["force"] is True
    assert sl.is_sealed()


# ---------------------------------------------------------------------------
# Lock with nothing active: seals fast; idempotent second lock
# ---------------------------------------------------------------------------


async def test_lock_nothing_active_seals_fast(container_on, monkeypatch):
    detach = _DetachSpy()
    monkeypatch.setattr(container, "detach", detach)
    app, sup = _build_app(StoreState.MOUNTED)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    body = resp.json()
    assert body["ok"] and body["store_state"] == "locked" and body["sealed"] is True
    assert sl.is_sealed()
    assert sup.is_locked()
    assert not sup.is_lock_in_flight()  # the operation finished; steady state unpinned
    assert app.state.store_state == "locked"
    assert detach.calls[0]["force"] is True


async def test_lock_refuses_during_encrypt_cutover_no_detach(
    container_on, monkeypatch, tmp_path
):
    """FIX 5: ``storage.lock`` refuses (typed, retryable) while the encrypt job is
    in its non-interruptible cutover window, and NEVER runs ``hdiutil`` detach on
    the mountpoint concurrently with the cutover's swap."""
    from screencap.daemon.encrypt_job import EncryptJob
    from screencap.migration import (
        MigrationLedger,
        MigrationPhase,
        MigrationState,
    )

    detach = _DetachSpy()
    monkeypatch.setattr(container, "detach", detach)
    app, sup = _build_app(StoreState.MOUNTED)

    # An encrypt migration in its cutover swap window (ledger phase CUTTING_OVER).
    ledger = MigrationLedger(tmp_path / "m.db")
    ledger.seed(["rec-a"])
    ledger.set_run(state=MigrationState.RUNNING, phase=MigrationPhase.CUTTING_OVER)
    app.state.encrypt_job = EncryptJob(app.state.event_bus, ledger=ledger)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    assert resp.status_code == 409
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == errors.STORE_LOCK_FAILED
    assert body["reason"] == "encrypt_cutover_in_progress"
    # The lock NEVER detached the mountpoint concurrently with the cutover swap.
    assert detach.calls == []
    # Nothing sealed; the store stays usable + unlocked.
    assert not sl.is_sealed()
    assert not sup.is_locked()


async def test_second_lock_is_idempotent_noop(container_on, monkeypatch):
    detach = _DetachSpy()
    monkeypatch.setattr(container, "detach", detach)
    # Pre-seal the store.
    sl.write_sealed_sentinel()
    app, sup = _build_app(StoreState.LOCKED)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    body = resp.json()
    assert body["ok"] and body.get("already_locked") is True
    assert detach.calls == [], "an already-sealed store must not detach again"


async def test_lock_refused_on_plaintext_install(monkeypatch):
    """container disabled → typed refusal, nothing sealed (no dead sentinel)."""
    import screencap.config as cfg

    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "0")
    cfg.invalidate_config_cache()
    detach = _DetachSpy()
    monkeypatch.setattr(container, "detach", detach)
    app, sup = _build_app(StoreState.MOUNTED)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    body = resp.json()
    assert body["ok"] is False and body["error"] == errors.STORE_LOCK_FAILED
    assert body["reason"] == "container_disabled"
    assert not sl.is_sealed()
    assert detach.calls == []


# ---------------------------------------------------------------------------
# store.locking emitted BEFORE the detach window
# ---------------------------------------------------------------------------


async def test_store_locking_emitted_before_detach(container_on, monkeypatch):
    app, sup = _build_app(StoreState.MOUNTED)
    order: list[str] = []

    bus = app.state.event_bus
    orig_publish = bus.publish

    async def _publish_spy(event):
        order.append(f"event:{event.get('type')}")
        return await orig_publish(event)

    monkeypatch.setattr(bus, "publish", _publish_spy)

    def _detach(target, *, force=True, **kw):  # noqa: ANN001
        order.append("detach")

    monkeypatch.setattr(container, "detach", _detach)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    assert resp.json()["ok"]
    assert f"event:{EVENT_STORE_LOCKING}" in order
    assert "detach" in order
    assert order.index(f"event:{EVENT_STORE_LOCKING}") < order.index("detach"), (
        "store.locking (the direct-reader release signal) must precede the detach"
    )
    # store.locked is announced only after a successful seal.
    assert order.index("detach") < order.index(f"event:{EVENT_STORE_LOCKED}")


async def test_lock_lifecycle_events_on_event_bus(container_on, monkeypatch):
    monkeypatch.setattr(container, "detach", _DetachSpy())
    app, sup = _build_app(StoreState.MOUNTED)
    bus = app.state.event_bus

    async with _client(app) as c:
        collector = asyncio.create_task(_drain_events(bus, count=10, timeout=2.0))
        await asyncio.sleep(0.05)
        await c.post("/v0/storage.lock", json={})
        seen = await collector

    assert EVENT_STORE_LOCKING in seen
    assert EVENT_STORE_LOCKED in seen


# ---------------------------------------------------------------------------
# Failed detach unwinds cleanly (proof-first)
# ---------------------------------------------------------------------------


async def test_failed_detach_unwinds_to_mounted_unlocked(container_on, monkeypatch):
    """RED→GREEN: a detach that fails past graceful+force must NOT seal — the store
    stays mounted+unlocked, the refuse flag is cleared, no sentinel is written."""
    boom = container.ContainerError("detach failed after force")
    monkeypatch.setattr(container, "detach", _DetachSpy(raises=boom))
    app, sup = _build_app(StoreState.MOUNTED)

    async with _client(app) as c:
        resp = await c.post("/v0/storage.lock", json={})

    body = resp.json()
    assert resp.status_code == 409
    assert body["ok"] is False and body["error"] == errors.STORE_LOCK_FAILED
    assert body["reason"] == "detach_failed"
    # Never a half-sealed state:
    assert not sl.is_sealed(), "no sentinel on a failed detach"
    assert not sup.is_locked(), "_store_locked cleared on unwind"
    assert not sup.is_lock_in_flight()
    assert app.state.store_state == "mounted"


# ---------------------------------------------------------------------------
# Force-detach only in lock (compact never forces — contrast)
# ---------------------------------------------------------------------------


async def test_lock_forces_detach_compact_does_not(container_on, monkeypatch):
    """Contrast: LOCK detaches with force=True; compact's discipline is force=False.

    KTD-15 permits force in the lock precisely because every writer is
    ledger-disciplined and readers were signalled; ``storage compact`` (KTD-12)
    never forces past a busy volume. The lock verb explicitly passes ``force=True``.
    """
    real_detach = container.detach  # capture the real fn before we patch it
    detach = _DetachSpy()
    monkeypatch.setattr(container, "detach", detach)
    app, sup = _build_app(StoreState.MOUNTED)

    async with _client(app) as c:
        await c.post("/v0/storage.lock", json={})

    assert detach.calls[0]["force"] is True, "lock uses force=True (KTD-15)"

    # Contrast the compact discipline: a busy volume under force=False must RAISE
    # (never be force-detached), which is what the compact flow relies on (KTD-12).
    def _busy_hdiutil(args, **kw):
        if args[:1] == ["detach"] and "-force" not in args:
            raise container.ContainerBusyError("volume busy")
        pytest.fail("compact discipline must NOT reach the -force detach")

    monkeypatch.setattr(container, "_run_hdiutil", _busy_hdiutil)
    with pytest.raises(container.ContainerBusyError):
        real_detach("/some/mnt", force=False)


# ---------------------------------------------------------------------------
# Audit records carry peer provenance + outcome (KTD-23)
# ---------------------------------------------------------------------------


async def test_lock_audit_record_has_peer_and_outcome(container_on, monkeypatch):
    monkeypatch.setattr(container, "detach", _DetachSpy())
    app, sup = _build_app(StoreState.MOUNTED)

    async with _client(app) as c:
        await c.post("/v0/storage.lock", json={})

    from screencap.daemon import audit_log

    log = audit_log._audit_log_path()
    assert log.exists()
    lines = [json.loads(x) for x in log.read_text().splitlines() if x.strip()]
    lock_lines = [r for r in lines if r["verb"] == "storage.lock"]
    assert lock_lines, "a storage.lock audit record must be written"
    rec = lock_lines[-1]
    assert rec["outcome"] == "ok"
    assert "peer_pid" in rec and "classification" in rec


# ---------------------------------------------------------------------------
# Unlock: reconcile + events; Keychain-locked retryable in-band (sentinel intact)
# ---------------------------------------------------------------------------


async def test_unlock_reconciles_and_emits(container_on, monkeypatch):
    sl.write_sealed_sentinel()
    app, sup = _build_app(StoreState.LOCKED)

    monkeypatch.setattr(
        sl, "mount_now", lambda: StoreResolution(StoreState.MOUNTED, mountpoint="/mnt")
    )
    reconcile_called = {"n": 0}
    monkeypatch.setattr(
        sup, "start_reconcile", lambda: reconcile_called.__setitem__("n", 1)
    )
    bus = app.state.event_bus

    async with _client(app) as c:
        collector = asyncio.create_task(_drain_events(bus, count=1, timeout=2.0))
        await asyncio.sleep(0.05)
        resp = await c.post("/v0/storage.unlock", json={})
        seen = await collector

    body = resp.json()
    assert body["ok"] and body["store_state"] == "mounted" and body["sealed"] is False
    assert not sl.is_sealed(), "unlock clears the sentinel on success"
    assert sup.is_locked() is False
    assert reconcile_called["n"] == 1, "unlock re-runs the start-time reconcile"
    assert EVENT_STORE_UNLOCKED in seen


async def test_unlock_keychain_locked_is_retryable_sentinel_intact(
    container_on, monkeypatch,
):
    sl.write_sealed_sentinel()
    app, sup = _build_app(StoreState.LOCKED)

    monkeypatch.setattr(
        sl,
        "mount_now",
        lambda: StoreResolution(StoreState.ERROR, reason=sl.ERROR_KEYCHAIN_LOCKED),
    )
    monkeypatch.setattr(
        sup, "start_reconcile", lambda: pytest.fail("must not reconcile on failure")
    )

    async with _client(app) as c:
        resp = await c.post("/v0/storage.unlock", json={})

    body = resp.json()
    assert resp.status_code == 409
    assert body["ok"] is False and body["error"] == errors.STORE_LOCK_FAILED
    assert body["reason"] == sl.ERROR_KEYCHAIN_LOCKED
    assert body["retryable"] is True
    assert sl.is_sealed(), "a failed unlock leaves the sentinel INTACT"


async def test_unlock_audit_record(container_on, monkeypatch):
    sl.write_sealed_sentinel()
    app, sup = _build_app(StoreState.LOCKED)
    monkeypatch.setattr(
        sl, "mount_now", lambda: StoreResolution(StoreState.MOUNTED, mountpoint="/mnt")
    )
    monkeypatch.setattr(sup, "start_reconcile", lambda: None)

    async with _client(app) as c:
        await c.post("/v0/storage.unlock", json={})

    from screencap.daemon import audit_log

    lines = [
        json.loads(x)
        for x in audit_log._audit_log_path().read_text().splitlines()
        if x.strip()
    ]
    unlock_lines = [r for r in lines if r["verb"] == "storage.unlock"]
    assert unlock_lines and unlock_lines[-1]["outcome"] == "ok"


# ---------------------------------------------------------------------------
# The quiescence engine (AE7 half 1): quiesce_for_lock bounds the wait
# ---------------------------------------------------------------------------


async def test_quiesce_for_lock_returns_true_when_workers_stop():
    """A resume that respects the shared quiesce Event clears within grace."""
    app, sup = _build_app(StoreState.MOUNTED)

    async def _cooperative_resume():
        # Mirrors a terminal-stage resume that polls the stop flag at its
        # ledger-safe boundaries and returns once it is set.
        while not sup._quiesce_event.is_set():
            await asyncio.sleep(0.01)

    sup._resume_tasks.add(asyncio.create_task(_cooperative_resume()))
    # Prune callback parity with _track_resume:
    for t in list(sup._resume_tasks):
        t.add_done_callback(sup._resume_tasks.discard)

    quiesced = await sup.quiesce_for_lock(grace=2.0)
    assert quiesced is True


async def test_quiesce_for_lock_times_out_when_worker_is_stubborn():
    """A resume that ignores the flag → quiesce returns False (force is the backstop)."""
    app, sup = _build_app(StoreState.MOUNTED)

    async def _stubborn_resume():
        await asyncio.sleep(5.0)  # ignores the quiesce flag

    task = asyncio.create_task(_stubborn_resume())
    sup._resume_tasks.add(task)
    task.add_done_callback(sup._resume_tasks.discard)
    try:
        quiesced = await sup.quiesce_for_lock(grace=0.1)
        assert quiesced is False, "grace expired → force detach is the backstop"
    finally:
        task.cancel()


# ---------------------------------------------------------------------------
# AE7 half 2: the terminal-stage stop_event halts at a ledger-safe boundary
# ---------------------------------------------------------------------------


def _make_cloud_recording(tmp_path: Path, *, name="ae7") -> Path:
    """A cloud-routed recording with a seeded (STAGED, not UPLOADED) ledger."""
    from screencap.engine.db import create_db, crud
    from screencap.pipeline_state import PipelineLedger, ensure_pipeline_state_schema

    rec_dir = tmp_path / name
    rec_dir.mkdir(parents=True, exist_ok=True)
    db_path = rec_dir / "recording.db"
    engine, Session = create_db(str(db_path))
    session = Session()
    recording = crud.insert_recording(session, {
        "timestamp": 1000.0, "platform": "darwin", "monitor_width": 1920,
        "monitor_height": 1080, "pixel_ratio": 2.0,
        "double_click_interval_seconds": 0.5, "double_click_distance_pixels": 5.0,
    })
    crud.insert_action_event(session, recording, 1001.0, {
        "name": "click", "mouse_x": 10.0, "mouse_y": 20.0,
        "mouse_button_name": "left", "mouse_pressed": True,
    })
    session.close()
    engine.dispose()
    (rec_dir / "chunk_0000.mp4").write_bytes(b"\x00" * 1024)
    (rec_dir / "events_0000.jsonl").write_text('{"_meta": 1}\n')
    (rec_dir / "chunk_0000_manifest.json").write_text("{}")
    ensure_pipeline_state_schema(db_path)
    ledger = PipelineLedger(db_path)
    ledger.seed_chunk(0)
    ledger.mark_staged(0)
    ledger.freeze_chunks_expected(1)
    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2, "destination": "cloud", "retention_policy": "keep_forever",
        "retention_params": {}, "show_on_website": True,
    }))
    (rec_dir / ".recording_id").write_text(name)
    return rec_dir


def test_terminal_stage_stop_event_halts_before_upload(tmp_path, monkeypatch):
    """AE7 RED→GREEN: a lock that fires DURING produce halts the terminal stage at
    the pre-upload ledger-safe boundary — the upload never runs and no chunk is
    marked UPLOADED — so the store can seal WITHOUT waiting for the upload."""
    import screencap.terminal_stage as ts
    from screencap.pipeline_state import PipelineLedger, UploadState

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "ts-run")
    rec_dir = _make_cloud_recording(tmp_path)
    stop_event = threading.Event()

    # Neutralize the GCS-touching reconcile / hole-probe seams.
    monkeypatch.setattr(ts, "assert_promotable_to_cloud", lambda *a, **k: None)
    monkeypatch.setattr(ts, "_reconcile_ledger_against_gcs", lambda *a, **k: 0)
    monkeypatch.setattr(ts, "_revalidate_uploaded_chunks", lambda *a, **k: 0)

    class _FakeProducer:
        def __init__(self, rec, console=None):  # noqa: ANN001
            self._rec = rec

        def produce(self, *, ledger=None, force=False):
            # The store lock fires WHILE we are producing the scrubbed copy.
            stop_event.set()
            (self._rec.parent / f"{self._rec.name}-scrubbed").mkdir(exist_ok=True)
            return SimpleNamespace(
                failed_chunks=[],
                scrubbed_dir=self._rec.parent / f"{self._rec.name}-scrubbed",
            )

    monkeypatch.setattr(ts, "CloudCopyProducer", _FakeProducer)

    import screencap.upload as upload_mod

    def _no_upload(*a, **k):
        pytest.fail("upload_recording must NOT run after a stop_event halt")

    monkeypatch.setattr(upload_mod, "upload_recording", _no_upload)

    with pytest.raises(ts.TerminalStageInterrupted):
        ts.run_terminal_stage(rec_dir, non_blocking=True, stop_event=stop_event)

    # Ledger consistent: the chunk is NOT falsely marked UPLOADED, and no
    # completeness sentinel exists (the recording resumes cleanly on unlock).
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_chunk(0).upload_state != UploadState.UPLOADED
    assert not (rec_dir / ".terminal_complete").exists()


def test_terminal_stage_no_stop_event_is_unchanged(tmp_path, monkeypatch):
    """Regression: with no stop_event the stage behaves exactly as before (local
    route, no interruption)."""
    import screencap.terminal_stage as ts
    from screencap.pipeline_state import Lifecycle, PipelineLedger

    monkeypatch.setattr(ts, "_RUN_DIR", tmp_path / "ts-run2")
    rec_dir = _make_cloud_recording(tmp_path, name="localrec")
    # Re-point to a LOCAL recording so no cloud/network seam is touched.
    (rec_dir / ".recording_intent").write_text(json.dumps({
        "version": 2, "destination": "local", "retention_policy": "keep_forever",
        "retention_params": {}, "show_on_website": False,
    }))
    result = ts.run_terminal_stage(rec_dir, non_blocking=True)  # stop_event=None
    assert result.routed is True
    ledger = PipelineLedger(rec_dir / "recording.db")
    assert ledger.get_chunk(0).lifecycle is Lifecycle.LOCAL_DONE
