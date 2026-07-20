"""HTTP-layer tests for the ambient.status / ambient.set daemon verbs (SCR-214 U12).

These drive the two runtime ambient-control verbs the macOS app uses to
enable/disable + observe always-on ambient capture at runtime:

* ``ambient.status`` (READ) — surfaces the persisted enabled/autostart toggles
  plus the live active/paused/degraded state + the ambient recording's name.
* ``ambient.set`` (MUTATE, audited) — persists the enabled/autostart config AND
  drives the supervisor to START (``_maybe_autostart_ambient``) or STOP
  (``stop_ambient_now``) ambient now.

They use a REAL ``Supervisor`` wired to the ``fake_engine.py`` script (mirroring
tests/daemon/test_supervisor_ambient.py) injected into a ``build_app()`` app over
an in-process ASGI transport — so the enable→spawn and disable→stop→no-re-arm
behaviours are exercised end-to-end, not mocked. Recordings/base/config dirs are
isolated by the autouse ``_isolate_recordings_and_run_dir`` fixture in
tests/daemon/conftest.py; the pidfile lock is isolated per test below.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable

import pytest
from httpx import ASGITransport, AsyncClient

from screencap.daemon import audit_log, errors, provenance, schema
from screencap.daemon.app import build_app
from screencap.daemon.event_bus import EventBus

_API_V = schema._RECORDING_START_API_VERSION


# ---------------------------------------------------------------------------
# Fixtures / helpers (self-contained; mirrors tests/daemon/test_supervisor_ambient.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the config-file path for ambient toggles.

    ``get_ambient_enabled`` / ``get_ambient_autostart`` read env FIRST, then
    config. These tests persist via ``set_ambient_*`` (config.toml, isolated by
    conftest), so any real env value would mask the write — clear both."""
    monkeypatch.delenv("SCREENCAP_AMBIENT_ENABLED", raising=False)
    monkeypatch.delenv("SCREENCAP_AMBIENT_AUTOSTART", raising=False)


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
def audit_log_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the audit log to a tmp path so tests don't write to ~/.screencap."""
    target = tmp_path / "audit.log"
    monkeypatch.setattr(audit_log, "_audit_log_path", lambda: target)
    return target


@pytest.fixture
def fake_engine_script(tmp_path: Path) -> Path:
    """Emit ``started`` then sleep; SIGTERM finalizes + exits 0."""
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


async def _pass_gate() -> None:
    """A start gate that always allows (granted permission + entitled)."""
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
    )
    defaults.update(kw)
    return Supervisor(EventBus(), **defaults)


def _count_spawns(supervisor, monkeypatch: pytest.MonkeyPatch) -> list:
    """Wrap ``supervisor.spawn`` to record every (ambient + explicit) call."""
    calls: list = []
    real_spawn = supervisor.spawn

    async def counting(request):
        calls.append(request)
        return await real_spawn(request)

    monkeypatch.setattr(supervisor, "spawn", counting)
    return calls


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    pytest.fail("condition did not become true before timeout")


def _app_with(supervisor) -> object:
    """Build the daemon app and inject ``supervisor`` (no lifespan needed: the
    ambient handlers only read ``app.state.supervisor`` + the request)."""
    app = build_app()
    app.state.supervisor = supervisor
    return app


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _patch_peer(
    monkeypatch: pytest.MonkeyPatch, descriptor: provenance.PeerDescriptor
) -> None:
    monkeypatch.setattr(
        provenance,
        "derive_peer_descriptor_from_asgi_scope",
        lambda _scope: descriptor,
    )


# ---------------------------------------------------------------------------
# ambient.status — surfaces enabled / active / paused / recording / degraded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reflects_enabled_active_paused_recording(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.config import set_ambient_enabled

    set_ambient_enabled(True)
    sup = _make_supervisor(fake_engine_script)
    sup._maybe_autostart_ambient()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)

    app = _app_with(sup)
    async with _client(app) as client:
        resp = await client.get("/v0/ambient.status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["enabled"] is True
        assert body["autostart"] is True  # config default
        assert body["active"] is True
        assert body["paused"] is False
        assert body["degraded"] is None
        assert isinstance(body["recording"], str)
        assert body["recording"].startswith("ambient-")

        # Simulate the engine's CONFIRMED pause event (the sole writer of the
        # snapshot's ``paused`` field, U4/KTD7) → the status reflects it.
        sup._session_state["paused"] = True
        paused_body = (await client.get("/v0/ambient.status")).json()
        assert paused_body["paused"] is True

    await sup.shutdown()


@pytest.mark.asyncio
async def test_status_surfaces_degraded_and_null_recording_on_permission_denied(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap.config import set_ambient_enabled

    set_ambient_enabled(True)

    async def deny_permission() -> None:
        raise errors.PermissionRequiredError(
            ["screen_recording"], schema_version=_API_V
        )

    sup = _make_supervisor(fake_engine_script, start_gate=deny_permission)
    sup._maybe_autostart_ambient()
    await _wait_until(lambda: sup.ambient_state()["degraded"] is not None)

    app = _app_with(sup)
    async with _client(app) as client:
        body = (await client.get("/v0/ambient.status")).json()
        assert body["enabled"] is True
        assert body["active"] is False
        assert body["recording"] is None
        assert body["degraded"] is not None
        assert errors.PERMISSION_REQUIRED in body["degraded"]

    await sup.shutdown()


@pytest.mark.asyncio
async def test_status_defaults_when_ambient_off_and_idle(
    fake_engine_script: Path, isolated_lock
) -> None:
    """Off + nothing live → all inert defaults (no recording mis-reported)."""
    sup = _make_supervisor(fake_engine_script)
    app = _app_with(sup)
    async with _client(app) as client:
        body = (await client.get("/v0/ambient.status")).json()
        assert body["ok"] is True
        assert body["schema_version"] == schema._AMBIENT_STATUS_API_VERSION
        # All the ambient fields are at their inert defaults.
        assert {k: body[k] for k in ("enabled", "autostart", "active", "paused",
                                     "degraded", "recording")} == {
            "enabled": False,
            "autostart": True,
            "active": False,
            "paused": False,
            "degraded": None,
            "recording": None,
        }
    await sup.shutdown()


# ---------------------------------------------------------------------------
# ambient.set enabled=True — persists opt-in AND starts ambient now
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_enabled_true_persists_and_starts_ambient(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap import config

    assert config.get_ambient_enabled() is False  # off by default
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)

    app = _app_with(sup)
    async with _client(app) as client:
        resp = await client.post("/v0/ambient.set", json={"enabled": True})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["enabled"] is True

    # Config persisted, and ambient auto-started EXACTLY once via the shared gate.
    assert config.get_ambient_enabled() is True
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    assert len(calls) == 1
    assert calls[0].ambient is True
    assert calls[0].cloud_intent is False
    await sup.shutdown()


@pytest.mark.asyncio
async def test_set_enabled_true_is_idempotent_when_already_active(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second enable=true while ambient is already live does NOT double-spawn."""
    from screencap import config

    config.set_ambient_enabled(True)
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)
    sup._maybe_autostart_ambient()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    assert len(calls) == 1

    app = _app_with(sup)
    async with _client(app) as client:
        resp = await client.post("/v0/ambient.set", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.json()["active"] is True

    await asyncio.sleep(0.2)
    assert len(calls) == 1  # still exactly one — no double-spawn
    await sup.shutdown()


# ---------------------------------------------------------------------------
# ambient.set enabled=False — persists opt-out, STOPS ambient, no re-arm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_enabled_false_stops_ambient_and_does_not_rearm(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap import config

    # A fast re-arm delay so a (wrongly) scheduled respawn would fire inside the
    # post-stop window this test waits out.
    monkeypatch.setenv("SCREENCAP_AMBIENT_REARM_BASE_DELAY", "0.01")
    config.set_ambient_enabled(True)
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)
    sup._maybe_autostart_ambient()
    await _wait_until(lambda: sup.ambient_state()["active"] is True)
    assert len(calls) == 1

    app = _app_with(sup)
    async with _client(app) as client:
        resp = await client.post("/v0/ambient.set", json={"enabled": False})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["enabled"] is False
        # ``stop_ambient_now`` awaits the full engine-exit teardown before the
        # response payload is built, so ``active`` is already False.
        assert body["active"] is False
        assert body["recording"] is None

    assert config.get_ambient_enabled() is False
    # Config now False → the engine-exit re-arm re-reads it and does NOT respawn.
    await asyncio.sleep(0.3)
    assert sup.ambient_state()["active"] is False
    assert len(calls) == 1  # no second spawn: the stop is final, not a bounce
    await sup.shutdown()


@pytest.mark.asyncio
async def test_set_enabled_false_when_idle_is_a_noop(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=false with nothing live just persists the opt-out (no stop error)."""
    from screencap import config

    config.set_ambient_enabled(True)
    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)

    app = _app_with(sup)
    async with _client(app) as client:
        resp = await client.post("/v0/ambient.set", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["active"] is False

    assert config.get_ambient_enabled() is False
    assert calls == []
    await sup.shutdown()


# ---------------------------------------------------------------------------
# ambient.set autostart — persists config only (no start/stop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_autostart_persists_without_touching_enabled(
    fake_engine_script: Path, isolated_lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from screencap import config

    sup = _make_supervisor(fake_engine_script)
    calls = _count_spawns(sup, monkeypatch)
    app = _app_with(sup)

    async with _client(app) as client:
        off = await client.post("/v0/ambient.set", json={"autostart": False})
        assert off.status_code == 200
        assert off.json()["autostart"] is False
        # autostart-only did not enable or start anything.
        assert off.json()["enabled"] is False
        assert config.get_ambient_autostart() is False

        on = await client.post("/v0/ambient.set", json={"autostart": True})
        assert on.status_code == 200
        assert on.json()["autostart"] is True

    assert config.get_ambient_autostart() is True
    assert calls == []  # autostart never spawns
    await sup.shutdown()


# ---------------------------------------------------------------------------
# ambient.set — malformed body is a typed 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_malformed_body_returns_400(
    fake_engine_script: Path, isolated_lock
) -> None:
    from screencap import config

    sup = _make_supervisor(fake_engine_script)
    app = _app_with(sup)
    async with _client(app) as client:
        # A non-bool value for a bool field → 400 invalid_request.
        bad_type = await client.post("/v0/ambient.set", json={"enabled": ["x"]})
        assert bad_type.status_code == 400
        assert bad_type.json()["ok"] is False
        assert bad_type.json()["error"] == errors.INVALID_REQUEST

        # A non-dict body → 400 invalid_request.
        non_dict = await client.post("/v0/ambient.set", json=[1, 2])
        assert non_dict.status_code == 400
        assert non_dict.json()["ok"] is False

    # A rejected request must not have mutated config.
    assert config.get_ambient_enabled() is False
    await sup.shutdown()


# ---------------------------------------------------------------------------
# ambient.set — every exit path is audited (peer + outcome)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_records_audit_line_on_ok_and_on_error(
    fake_engine_script: Path,
    isolated_lock,
    audit_log_at: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_peer(
        monkeypatch,
        provenance.PeerDescriptor(
            pid=4242,
            path="/Applications/Screencap.app/Contents/MacOS/screencap",
            classification=provenance.STARTED_BY_SWIFTUI,
        ),
    )
    sup = _make_supervisor(fake_engine_script)
    app = _app_with(sup)
    async with _client(app) as client:
        ok = await client.post("/v0/ambient.set", json={"autostart": True})
        assert ok.status_code == 200
        bad = await client.post("/v0/ambient.set", json=[1])
        assert bad.status_code == 400

    lines = [
        json.loads(line)
        for line in audit_log_at.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 2
    ok_rec, err_rec = lines
    assert ok_rec["verb"] == "ambient.set"
    assert ok_rec["outcome"] == "ok"
    assert ok_rec["peer_pid"] == 4242
    assert ok_rec["classification"] == provenance.STARTED_BY_SWIFTUI
    # The malformed request still audits, with the typed error code as outcome.
    assert err_rec["verb"] == "ambient.set"
    assert err_rec["outcome"] == errors.INVALID_REQUEST
    await sup.shutdown()


# ---------------------------------------------------------------------------
# Idle-shutdown classification: set is activity, status is not
# ---------------------------------------------------------------------------


def test_activity_paths_membership() -> None:
    """ambient.set is a lifecycle mutation (activity); ambient.status is a read
    that must NOT keep an auto-spawned daemon alive."""
    from screencap.daemon._idle_shutdown import _ACTIVITY_PATHS

    assert "/v0/ambient.set" in _ACTIVITY_PATHS
    assert "/v0/ambient.status" not in _ACTIVITY_PATHS
