"""SCR-228 U3/U4 — daemon storage.migrate verb, mutual exclusion, reconcile.

Drives ``POST /v0/storage.migrate`` against the in-process ASGI app with config
paths pointed at a tmp dir, plus focused Supervisor tests for the recording /
migration mutual-exclusion guard.
"""

from __future__ import annotations

import fcntl
import os

import httpx
import pytest

from screencap import storage_migration as sm
from screencap.daemon import errors, schema
from screencap.daemon.app import build_app
from screencap.daemon.event_bus import EventBus
from screencap.daemon.supervisor import Supervisor


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point config at a tmp base and seed a recordings library at source."""
    import screencap.config as cfg

    base = tmp_path / "screencap"
    (base / "run").mkdir(parents=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", base / "recordings")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    cfg._config_cache = None

    source = cfg.get_recordings_dir()  # base/recordings, created
    rec = source / "rec-1"
    rec.mkdir(parents=True)
    (rec / "recording.db").write_text("LOCAL-ONLY")
    yield base
    cfg._config_cache = None


def _app_with_supervisor():
    app = build_app()
    # lifespan (which creates the supervisor) does not run under ASGITransport;
    # attach one directly, like the backfill tests attach their job.
    app.state.supervisor = Supervisor(app.state.event_bus, reconcile_on_init=False)
    return app


# --- happy path + config flip ---


@pytest.mark.asyncio
async def test_same_volume_move_succeeds(isolated_config, monkeypatch):
    import screencap.config as cfg
    from screencap.daemon import app as appmod

    monkeypatch.setattr(appmod, "_storage_recording_active", lambda: False)
    target = isolated_config / "new_recordings"
    app = _app_with_supervisor()

    async with _client(app) as client:
        resp = await client.post(
            "/v0/storage.migrate", json={"target": str(target)}
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["moved_to"] == str(target.resolve())
    # Library relocated; config flipped to the new path.
    assert (target / "rec-1" / "recording.db").read_text() == "LOCAL-ONLY"
    assert not (isolated_config / "recordings" / "rec-1").exists()
    assert cfg.get_recordings_dir() == target.resolve()


# --- validation refusals surface as typed errors with a reason ---


@pytest.mark.asyncio
async def test_cross_volume_rejected(isolated_config, monkeypatch):
    from screencap.daemon import app as appmod

    monkeypatch.setattr(appmod, "_storage_recording_active", lambda: False)
    source = (isolated_config / "recordings").resolve()

    def fake_st_dev(path):
        from pathlib import Path

        return 1 if Path(path) == source else 2

    monkeypatch.setattr(sm, "_st_dev", fake_st_dev)
    target = isolated_config / "external" / "recs"
    app = _app_with_supervisor()

    async with _client(app) as client:
        resp = await client.post(
            "/v0/storage.migrate", json={"target": str(target)}
        )

    assert resp.status_code == 409
    payload = resp.json()
    assert payload["error"] == errors.STORAGE_MIGRATION_FAILED
    assert payload["reason"] == sm.Reason.CROSS_VOLUME
    assert (isolated_config / "recordings" / "rec-1").exists()  # untouched


@pytest.mark.asyncio
async def test_cloud_synced_rejected(isolated_config, monkeypatch):
    from screencap.daemon import app as appmod

    monkeypatch.setattr(appmod, "_storage_recording_active", lambda: False)
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda t: True)
    target = isolated_config / "synced"
    app = _app_with_supervisor()

    async with _client(app) as client:
        resp = await client.post(
            "/v0/storage.migrate", json={"target": str(target)}
        )

    assert resp.status_code == 409
    assert resp.json()["reason"] == sm.Reason.CLOUD_SYNCED


@pytest.mark.asyncio
async def test_recording_active_rejected(isolated_config, monkeypatch):
    from screencap.daemon import app as appmod

    # A recording is in progress (non-daemon, caught by the pidfile check).
    monkeypatch.setattr(appmod, "_storage_recording_active", lambda: True)
    target = isolated_config / "new_recordings"
    app = _app_with_supervisor()

    async with _client(app) as client:
        resp = await client.post(
            "/v0/storage.migrate", json={"target": str(target)}
        )

    assert resp.status_code == 409
    assert resp.json()["reason"] == "recording_active"
    assert (isolated_config / "recordings" / "rec-1").exists()  # untouched


@pytest.mark.asyncio
async def test_terminal_stage_in_flight_rejected(isolated_config, monkeypatch):
    # A just-stopped recording's terminal stage (scrub/upload/finalize) holds a
    # terminal-<name>.lock; migration must refuse rather than rename the tree out
    # from under it, even though no recording pidfile is active.
    from screencap.daemon import app as appmod

    monkeypatch.setattr(appmod, "_storage_recording_active", lambda: False)
    run_dir = isolated_config / "run"
    lock_path = run_dir / "terminal-rec-1.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # simulate a live terminal stage
    try:
        target = isolated_config / "new_recordings"
        app = _app_with_supervisor()
        async with _client(app) as client:
            resp = await client.post(
                "/v0/storage.migrate", json={"target": str(target)}
            )
        assert resp.status_code == 409
        assert resp.json()["reason"] == "recording_active"
        assert (isolated_config / "recordings" / "rec-1").exists()  # untouched
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@pytest.mark.asyncio
async def test_daemon_is_busy_while_migrating(isolated_config):
    # An auto-spawned daemon must not idle-shutdown mid-migration.
    from screencap.daemon._idle_shutdown import _daemon_is_busy

    app = _app_with_supervisor()
    assert _daemon_is_busy(app) is False
    await app.state.supervisor.acquire_migration(schema_version=1)
    try:
        assert _daemon_is_busy(app) is True
    finally:
        app.state.supervisor.release_migration()
    assert _daemon_is_busy(app) is False


@pytest.mark.asyncio
async def test_missing_target_is_invalid_request(isolated_config):
    app = _app_with_supervisor()
    async with _client(app) as client:
        resp = await client.post("/v0/storage.migrate", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == errors.INVALID_REQUEST


# --- U4: recording start is refused while a migration holds the daemon ---


@pytest.mark.asyncio
async def test_spawn_refused_during_migration():
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    sup._migration_active = True
    req = schema.RecordingStartRequest.model_validate({})
    with pytest.raises(errors.MigrationInProgressError):
        await sup.spawn(req)


# --- acquire/release migration guard ---


@pytest.mark.asyncio
async def test_acquire_migration_refuses_active_recording():
    sup = Supervisor(EventBus(), reconcile_on_init=False)

    class _AliveProc:
        def is_alive(self):
            return True

    sup._proc = _AliveProc()
    with pytest.raises(errors.StorageMigrationError) as ei:
        await sup.acquire_migration(schema_version=1)
    assert ei.value.reason == "recording_active"


@pytest.mark.asyncio
async def test_acquire_migration_refuses_double():
    sup = Supervisor(EventBus(), reconcile_on_init=False)
    await sup.acquire_migration(schema_version=1)
    try:
        with pytest.raises(errors.StorageMigrationError) as ei:
            await sup.acquire_migration(schema_version=1)
        assert ei.value.reason == "migration_in_progress"
    finally:
        sup.release_migration()


# --- start-time reconciliation wiring ---


@pytest.mark.asyncio
async def test_lifespan_invokes_reconcile(monkeypatch):
    from screencap import config
    from screencap.daemon import app as appmod

    calls = []
    monkeypatch.setattr(
        sm, "reconcile_pending", lambda commit, **kw: calls.append(commit)
    )

    async def _noop_warm(app):
        return None

    monkeypatch.setattr(appmod, "_warm_grant_cache", _noop_warm)

    application = appmod.build_app()
    async with appmod.lifespan(application):
        pass

    assert calls and calls[0] is config.set_recordings_dir
