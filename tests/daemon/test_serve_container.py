"""SCR-258 U4 — sealed-capable daemon serve model (KTD-14).

The daemon binds its socket FIRST, then resolves the encrypted-store state, and
serves ``mounted`` / ``locked`` / ``absent`` / ``error`` as HEALTHY states with
typed envelopes — never exiting or crash-looping because the store is sealed or
not yet initialized. These tests are Vision-free and mock the container /
Keychain seams; **no real ``hdiutil`` runs** (the sealed / absent / key-error /
disabled paths short-circuit before any subprocess, and the one healthy-mount
test mocks ``container.attach`` / ``harden_mount``).

Coverage maps to the plan U4 test-scenarios list:

* sealed-sentinel start binds + serves ``locked`` with NO mount attempt;
* AE4 (daemon arm): a sealed sentinel survives a plain restart — plain serve
  never clears it;
* AE5: key genuinely-missing vs entitlement-mismatch -> distinct typed ``error``
  reasons, bundle bytes untouched, no plaintext dir;
* AE3 (daemon side): each read verb (incl. ``chat.answer``) returns
  ``store_state`` + empty results, never a 500;
* ``recording.start`` refused while LOCKED and while ABSENT — typed, before any
  ``started`` signal, no plaintext ``mkdir``;
* ABSENT serves ``store_state=absent``;
* idle-shutdown fires normally while locked;
* ``container_enabled=false`` no-bundle byte-identical vs bundle-present
  downgrade-unsupported;
* bind-before-mount ORDERING in ``serve()``;
* disk guards evaluate the HOST volume backing the bundle (KTD-13).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from screencap import container
from screencap.daemon import errors
from screencap.daemon import store_lifecycle as sl
from screencap.daemon.app import build_app
from screencap.daemon.store_lifecycle import StoreResolution, StoreState, resolve_store_state
from screencap.daemon.supervisor import Supervisor

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def container_base(tmp_path, monkeypatch):
    """Point the base dir + recordings dir at a tmp tree with the container ON.

    The autouse daemon conftest sets ``SCREENCAP_RECORDINGS_DIR`` (which bypasses
    container-aware data-root resolution) and deletes ``SCREENCAP_CONTAINER_ENABLED``.
    Here we want the *serve-model* resolver exercised, so we re-enable the flag and
    keep ``SCREENCAP_RECORDINGS_DIR`` pointed at a tmp recordings dir (the
    resolver reads it as the mountpoint; the flag drives ``get_container_enabled``).
    """
    import screencap.config as cfg

    base = tmp_path / "screencap"
    (base / "run").mkdir(parents=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    recordings = tmp_path / "recordings"
    recordings.mkdir(parents=True)
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(recordings))
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    cfg.invalidate_config_cache()
    yield base
    cfg.invalidate_config_cache()


def _make_bundle(base: Path) -> Path:
    """Create a fake sparse bundle dir + a band, returning the bundle path.

    A directory (not a real image) is enough for the resolver's ``.exists()``
    gate; the "untouched bytes" assertions read the band file we seed here.
    """
    bundle = sl.store_bundle_path()
    (bundle / "bands").mkdir(parents=True)
    (bundle / "bands" / "0").write_bytes(b"ENCRYPTED-BAND-BYTES")
    return bundle


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _app_locked(state: StoreState = StoreState.LOCKED, reason: str | None = None):
    """In-process app with the store state stamped directly (lifespan-less)."""
    app = build_app()
    app.state.store_state = state.value
    app.state.store_reason = reason
    sup = Supervisor(app.state.event_bus, reconcile_on_init=False)
    sup.set_store_state(state, reason)
    app.state.supervisor = sup
    return app


# ---------------------------------------------------------------------------
# Resolver: sealed sentinel -> LOCKED, NO mount attempt (bind-before-mount core)
# ---------------------------------------------------------------------------


def test_sealed_sentinel_resolves_locked_without_mount(container_base, monkeypatch):
    _make_bundle(container_base)
    sl.sealed_sentinel_path().parent.mkdir(parents=True, exist_ok=True)
    sl.sealed_sentinel_path().write_text("sealed\n")

    # Any attempt to touch the key or attach the image while sealed is a bug.
    def _boom(*a, **k):
        raise AssertionError("mount/key path must not be reached while sealed")

    monkeypatch.setattr(container, "require_container_key", _boom)
    monkeypatch.setattr(container, "attach", _boom)
    monkeypatch.setattr(container, "status", _boom)

    resolution = resolve_store_state()

    assert resolution.state is StoreState.LOCKED
    assert resolution.reason is None


def test_sealed_sentinel_survives_plain_resolve(container_base, monkeypatch):
    """AE4 (daemon arm): a plain serve never clears the sealed sentinel."""
    _make_bundle(container_base)
    sentinel = sl.sealed_sentinel_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("sealed\n")
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))

    first = resolve_store_state()
    # A reboot-shaped second resolve (fresh call) still sees the sentinel.
    second = resolve_store_state()

    assert first.state is second.state is StoreState.LOCKED
    assert sentinel.exists(), "plain serve must not clear the sealed sentinel"


# ---------------------------------------------------------------------------
# Resolver: ABSENT / disabled / downgrade
# ---------------------------------------------------------------------------


def test_absent_when_enabled_and_no_bundle(container_base):
    assert not sl.store_bundle_path().exists()
    resolution = resolve_store_state()
    assert resolution.state is StoreState.ABSENT


def test_disabled_no_bundle_is_byte_identical(container_base, monkeypatch):
    """container OFF + no bundle -> MOUNTED, and no container machinery engaged."""
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "0")
    import screencap.config as cfg

    cfg.invalidate_config_cache()
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))
    monkeypatch.setattr(
        container, "require_container_key", lambda: pytest.fail("read key")
    )

    resolution = resolve_store_state()

    assert resolution.state is StoreState.MOUNTED
    assert resolution.reason is None
    # No mount.lock is acquired on the disabled path (byte-identical to today).
    assert not sl.mount_lock_path().exists()


def test_disabled_but_bundle_present_is_downgrade_unsupported(container_base, monkeypatch):
    """container OFF on a bundle-present install -> typed error, no plaintext (KTD-19)."""
    _make_bundle(container_base)
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "0")
    import screencap.config as cfg

    cfg.invalidate_config_cache()
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))

    resolution = resolve_store_state()

    assert resolution.state is StoreState.ERROR
    assert resolution.reason == sl.ERROR_DOWNGRADE_UNSUPPORTED


# ---------------------------------------------------------------------------
# Resolver: AE5 — key missing vs entitlement mismatch (distinct typed errors)
# ---------------------------------------------------------------------------


def _band_bytes(bundle: Path) -> bytes:
    return (bundle / "bands" / "0").read_bytes()


def test_key_genuinely_missing_is_error_untouched(container_base, monkeypatch):
    bundle = _make_bundle(container_base)
    before = _band_bytes(bundle)

    def _missing():
        raise container.ContainerKeyMissingError("no key anywhere")

    monkeypatch.setattr(container, "require_container_key", _missing)
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))

    resolution = resolve_store_state()

    assert resolution.state is StoreState.ERROR
    assert resolution.reason == sl.ERROR_KEY_MISSING
    # bundle bytes untouched; no plaintext recordings created inside the mountpoint.
    assert _band_bytes(bundle) == before
    from screencap.config import get_recordings_dir

    assert list(get_recordings_dir().iterdir()) == []


def test_entitlement_mismatch_is_distinct_error_untouched(container_base, monkeypatch):
    bundle = _make_bundle(container_base)
    before = _band_bytes(bundle)

    def _unreachable():
        raise container.ContainerKeyUnreachableError("entitlement mismatch")

    monkeypatch.setattr(container, "require_container_key", _unreachable)
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))

    resolution = resolve_store_state()

    assert resolution.state is StoreState.ERROR
    assert resolution.reason == sl.ERROR_ENTITLEMENT_MISMATCH
    assert resolution.reason != sl.ERROR_KEY_MISSING  # distinct from genuine loss
    assert _band_bytes(bundle) == before


def test_keychain_locked_is_retryable_in_band(container_base, monkeypatch):
    """A locked Keychain at mount is a retryable in-band error, NOT a daemon exit."""
    _make_bundle(container_base)

    def _locked():
        raise container.KeychainLockedError("keychain locked")

    monkeypatch.setattr(container, "require_container_key", _locked)

    resolution = resolve_store_state()  # must NOT raise

    assert resolution.state is StoreState.ERROR
    assert resolution.reason == sl.ERROR_KEYCHAIN_LOCKED


# ---------------------------------------------------------------------------
# Resolver: ROGUE mountpoint / healthy mount (operator stop vs mounted)
# ---------------------------------------------------------------------------


def test_rogue_mountpoint_raises_operator_stop(container_base, monkeypatch):
    _make_bundle(container_base)
    monkeypatch.setattr(container, "require_container_key", lambda: b"k" * 32)
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached over rogue"))
    # Make the mountpoint a NON-empty plain directory that is not a mounted volume.
    from screencap.config import get_recordings_dir

    (get_recordings_dir() / "leftover.txt").write_text("plaintext leftover")

    with pytest.raises(container.RogueMountpointError):
        resolve_store_state()


def test_healthy_path_attaches_and_hardens(container_base, monkeypatch):
    _make_bundle(container_base)
    monkeypatch.setattr(container, "require_container_key", lambda: b"k" * 32)
    from screencap.config import get_recordings_dir

    mp = str(get_recordings_dir())
    attach_calls: list[tuple] = []
    harden_calls: list[str] = []

    def _attach(bundle, key, mountpoint, **k):
        attach_calls.append((bundle, mountpoint))
        return container.AttachInfo(device="/dev/disk9", mountpoint=mountpoint)

    monkeypatch.setattr(container, "attach", _attach)
    monkeypatch.setattr(container, "harden_mount", lambda m, **k: harden_calls.append(m))

    resolution = resolve_store_state()

    assert resolution.state is StoreState.MOUNTED
    assert resolution.mountpoint == mp
    assert attach_calls and attach_calls[0][1] == mp
    assert harden_calls == [mp]


# ---------------------------------------------------------------------------
# serve() bind-before-mount ordering
# ---------------------------------------------------------------------------


def test_serve_binds_socket_before_resolving_store(tmp_path, monkeypatch):
    """serve() binds the socket FIRST, then resolves store state (KTD-14).

    Proven by recording the call order: an operator hard-stop from the resolver
    makes serve() return the container exit code before uvicorn ever runs, so the
    ordering is observable without standing up the server. If resolution ran
    before bind, ``order`` would start with ``resolve``.
    """
    from screencap.daemon import server
    from screencap.daemon import socket as socket_mod

    order: list[str] = []

    class _DummyListener:
        def close(self) -> None:
            order.append("listener_close")

    def _fake_bind(path):
        order.append("bind")
        return _DummyListener()

    def _fake_resolve(**kwargs):
        order.append("resolve")
        raise container.RogueMountpointError("rogue mountpoint")

    monkeypatch.setattr(socket_mod, "bind_unix_socket", _fake_bind)
    monkeypatch.setattr(socket_mod, "cleanup_socket", lambda p: order.append("cleanup"))
    monkeypatch.setattr(sl, "resolve_store_state", _fake_resolve)

    rc = server.serve(socket_path=tmp_path / "api.sock")

    assert rc == 1  # RogueMountpointError.exit_code (operator hard-stop)
    assert order[0] == "bind"
    assert order.index("bind") < order.index("resolve")


def test_serve_locked_store_does_not_exit(tmp_path, monkeypatch):
    """A sealed store is a healthy serving state — serve() proceeds past resolve.

    We stop just short of standing up uvicorn by having ``build_app`` raise a
    sentinel AFTER resolution succeeds, proving the LOCKED resolution did not
    short-circuit serve() to an exit code.
    """
    from screencap.daemon import server
    from screencap.daemon import socket as socket_mod

    class _DummyListener:
        def close(self) -> None:
            pass

    monkeypatch.setattr(socket_mod, "bind_unix_socket", lambda p: _DummyListener())
    monkeypatch.setattr(socket_mod, "cleanup_socket", lambda p: None)
    monkeypatch.setattr(
        sl, "resolve_store_state", lambda **k: StoreResolution(StoreState.LOCKED)
    )

    sentinel = RuntimeError("reached run()/build_app past a LOCKED resolution")

    def _build_app_boom():
        raise sentinel

    monkeypatch.setattr(server, "build_app", _build_app_boom, raising=False)
    # build_app is imported inside serve() from screencap.daemon.app; patch there.
    import screencap.daemon.app as appmod

    monkeypatch.setattr(appmod, "build_app", _build_app_boom)

    with pytest.raises(RuntimeError, match="past a LOCKED resolution"):
        server.serve(socket_path=tmp_path / "api.sock")


# ---------------------------------------------------------------------------
# AE3 — read verbs return store_state + empty results, never a 500
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [StoreState.LOCKED, StoreState.ABSENT])
async def test_read_verbs_carry_store_state_and_empty(state):
    app = _app_locked(state)
    async with _client(app) as client:
        # recording.list (GET)
        r = await client.get("/v0/recording.list")
        assert r.status_code == 200
        assert r.json()["store_state"] == state.value
        assert r.json()["recordings"] == []

        # content.search
        r = await client.post("/v0/content.search", json={"query": "anything"})
        assert r.status_code == 200
        body = r.json()
        assert body["store_state"] == state.value
        assert body["hits"] == []
        assert body["index_state"] == "store_unavailable"

        # transcript.search
        r = await client.post("/v0/transcript.search", json={"query": "hi"})
        assert r.status_code == 200
        assert r.json()["store_state"] == state.value
        assert r.json()["hits"] == []

        # timeline.query
        r = await client.post("/v0/timeline.query", json={})
        assert r.status_code == 200
        assert r.json()["store_state"] == state.value
        assert r.json()["rows"] == []

        # frame.nearest
        r = await client.post(
            "/v0/frame.nearest", json={"recording": "demo", "timestamp_ms": 1000}
        )
        assert r.status_code == 200
        assert r.json()["store_state"] == state.value
        assert r.json()["stem"] is None

        # chat.answer (fail-safe refusal, not a 500)
        r = await client.post("/v0/chat.answer", json={"question": "what did I do?"})
        assert r.status_code == 200
        body = r.json()
        assert body["store_state"] == state.value
        assert body["refusal"] is True
        assert body["sources"] == []


@pytest.mark.asyncio
async def test_daemon_info_reports_store_state():
    app = _app_locked(StoreState.ABSENT)
    async with _client(app) as client:
        r = await client.get("/v0/daemon.info")
    assert r.status_code == 200
    assert r.json()["store_state"] == "absent"


@pytest.mark.asyncio
async def test_read_verbs_omit_error_when_mounted():
    """Default (mounted) app still carries store_state=mounted on read verbs."""
    app = build_app()  # no store_state stamped -> defaults to mounted
    async with _client(app) as client:
        r = await client.get("/v0/recording.list")
    assert r.status_code == 200
    assert r.json()["store_state"] == "mounted"


# ---------------------------------------------------------------------------
# recording.start refused while LOCKED / ABSENT (typed, before started, no mkdir)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,code",
    [
        (StoreState.LOCKED, errors.STORE_LOCKED),
        (StoreState.ABSENT, errors.STORE_ABSENT),
        (StoreState.ERROR, errors.STORE_ABSENT),
    ],
)
async def test_recording_start_refused_before_started(state, code, monkeypatch):
    import screencap.config as cfg

    app = _app_locked(state)
    recordings = cfg.get_recordings_dir()
    cursor_before = app.state.event_bus.current_cursor()

    async with _client(app) as client:
        r = await client.post("/v0/recording.start", json={"name": "demo"})

    assert r.status_code == 409
    assert r.json()["error"] == code
    # No `started` (or any) event was published — the refusal fired before spawn.
    assert app.state.event_bus.current_cursor() == cursor_before
    # No plaintext recording directory was created at the mountpoint.
    assert list(recordings.iterdir()) == []


@pytest.mark.asyncio
async def test_recording_start_refusal_surfaces_error_reason():
    """SCR-258 U4/U9 (KTD-14): when the store is in an ERROR sub-state the
    recording.start refusal envelope carries the ERROR_* ``reason`` (e.g.
    ``key_missing``) so the client can distinguish the cause, mirroring
    StorageMigrationError. Backward-compatible: no reason -> the key is omitted."""
    from screencap.daemon import store_lifecycle as sl

    app = _app_locked(StoreState.ERROR, reason=sl.ERROR_KEY_MISSING)
    async with _client(app) as client:
        r = await client.post("/v0/recording.start", json={"name": "demo"})

    assert r.status_code == 409
    body = r.json()
    assert body["error"] == errors.STORE_ABSENT
    assert body["reason"] == sl.ERROR_KEY_MISSING

    # A refusal with no known sub-cause omits the reason key entirely (compat).
    app2 = _app_locked(StoreState.LOCKED)
    async with _client(app2) as client:
        r2 = await client.post("/v0/recording.start", json={"name": "demo"})
    assert r2.status_code == 409
    assert "reason" not in r2.json()


# ---------------------------------------------------------------------------
# idle-shutdown fires normally while locked
# ---------------------------------------------------------------------------


def test_idle_shutdown_not_pinned_by_locked_store():
    from screencap.daemon import _idle_shutdown

    app = build_app()
    sup = Supervisor(app.state.event_bus, reconcile_on_init=False)
    sup.set_store_state(StoreState.LOCKED)
    app.state.supervisor = sup

    assert sup.is_locked() is True
    # A sealed steady state does NOT pin the idle watchdog (KTD-15): no active
    # session, no resume, no migration -> the daemon is idle and may exit.
    assert sup.is_migrating() is False
    assert _idle_shutdown._daemon_is_busy(app) is False


# ---------------------------------------------------------------------------
# Disk guards evaluate the HOST volume backing the bundle (KTD-13)
# ---------------------------------------------------------------------------


def test_disk_preflight_checks_host_volume(tmp_path, monkeypatch):
    from screencap.engine import disk_policy
    from screencap.engine.screen_recorder import DiskTooLowAtStart

    host = tmp_path / "host"
    host.mkdir()
    mounted_capture = tmp_path / "mounted" / "rec"
    mounted_capture.mkdir(parents=True)
    monkeypatch.setenv(sl.DISK_HOST_PATH_ENV, str(host))
    monkeypatch.setenv("SCREENCAP_DISK_WARN_MB", "1000")
    monkeypatch.setenv("SCREENCAP_DISK_STOP_MB", "500")

    seen: list[str] = []

    class _Usage:
        free = 10 * 1_048_576  # 10 MB — far below the 1000 MB warn bound

    def _fake_usage(path):
        seen.append(str(path))
        return _Usage()

    monkeypatch.setattr(disk_policy.shutil, "disk_usage", _fake_usage)

    pol = disk_policy.MonitorAndStop()
    pol.bind(mounted_capture)
    with pytest.raises(DiskTooLowAtStart):
        pol.preflight()

    # The free-space check evaluated the HOST volume, NOT the (virtual-size)
    # mounted capture dir.
    assert seen == [str(host)]


def test_disk_preflight_uses_capture_dir_without_host_env(tmp_path, monkeypatch):
    from screencap.engine import disk_policy

    monkeypatch.delenv(sl.DISK_HOST_PATH_ENV, raising=False)
    monkeypatch.setenv("SCREENCAP_DISK_WARN_MB", "1")
    monkeypatch.setenv("SCREENCAP_DISK_STOP_MB", "0")
    capture = tmp_path / "rec"
    capture.mkdir()

    seen: list[str] = []

    class _Usage:
        free = 999 * 1_048_576 * 1_048_576  # plenty

    monkeypatch.setattr(
        disk_policy.shutil, "disk_usage", lambda p: seen.append(str(p)) or _Usage()
    )

    pol = disk_policy.MonitorAndStop()
    pol.bind(capture)
    pol.preflight()

    assert seen == [str(capture)]  # today's behavior: the capture dir itself
