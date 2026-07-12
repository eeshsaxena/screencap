"""SCR-258 U11 (KTD-19) — ``storage.migrate`` moves the encrypted BUNDLE.

Makes the shipped "Change storage location" (SCR-228) work for vault installs:
when the container is active, the move detaches the store, relocates the bundle
(the recordings *mountpoint* is unchanged), flips config, and remounts. The
plaintext bypass narrows to legacy custom dirs + the ``SCREENCAP_RECORDINGS_DIR``
override, whose behavior stays byte-identical.

All ``@pytest.mark.privacy`` + Vision-free. The container attach/detach seam is
mocked (no real ``hdiutil``); the bundle-move core moves REAL directories between
temp dirs, so the same-volume rename and cross-volume copy-verify-delete logic is
exercised end to end.
"""

from __future__ import annotations

import httpx
import pytest

from screencap import container
from screencap import storage_migration as sm
from screencap.daemon import store_lifecycle as sl
from screencap.daemon.supervisor import Supervisor

pytestmark = pytest.mark.privacy

BUNDLE = container.BUNDLE_NAME
TOKEN = "SECRET-BANDS"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bundle(dir_path) -> "object":
    """Create a fake sparse bundle (a dir with a token file) at ``dir_path``."""
    b = dir_path / BUNDLE
    b.mkdir(parents=True, exist_ok=True)
    (b / "token").write_text(TOKEN)
    return b


@pytest.fixture
def vault_config(tmp_path, monkeypatch):
    """Point config at a tmp base, enable the container, seed a bundle + mountpoint."""
    import screencap.config as cfg

    base = tmp_path / "screencap"
    (base / "run").mkdir(parents=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", base / "recordings")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    cfg._config_cache = None

    # The recordings mountpoint (kept unchanged by the move).
    mountpoint = cfg.get_recordings_dir()  # base/recordings
    # The bundle currently lives in the base dir (default location).
    _make_bundle(base)
    yield {"cfg": cfg, "base": base, "mountpoint": mountpoint, "tmp": tmp_path}
    cfg._config_cache = None


# ---------------------------------------------------------------------------
# migrate_bundle — pure data-movement core (real dirs)
# ---------------------------------------------------------------------------


def test_migrate_bundle_same_volume_rename(tmp_path):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    _make_bundle(src_dir)
    target = tmp_path / "new"

    committed: list = []
    outcome = sm.migrate_bundle(
        src_dir / BUNDLE, target / BUNDLE, committed.append
    )

    assert outcome.ok
    assert (target / BUNDLE / "token").read_text() == TOKEN
    assert not (src_dir / BUNDLE).exists()  # old location empty
    assert committed == [target.resolve()]  # config flipped to the new dir


def test_migrate_bundle_cross_volume_copy_verify_delete(tmp_path, monkeypatch):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    _make_bundle(src_dir)
    target = tmp_path / "new"

    # Force the cross-volume path even though both live on one real fs.
    monkeypatch.setattr(
        sm, "_st_dev", lambda p: 1 if str(src_dir) in str(p) else 2
    )

    committed: list = []
    outcome = sm.migrate_bundle(
        src_dir / BUNDLE, target / BUNDLE, committed.append
    )

    assert outcome.ok
    assert (target / BUNDLE / "token").read_text() == TOKEN
    assert not (src_dir / BUNDLE).exists()
    assert committed == [target.resolve()]
    # No staging dir left behind.
    assert not (target / (BUNDLE + sm.BUNDLE_PARTIAL_SUFFIX)).exists()


def test_migrate_bundle_cross_volume_interrupted_then_retry_converges(
    tmp_path, monkeypatch
):
    """Interrupted mid-copy → original intact + authoritative; retry converges."""
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    _make_bundle(src_dir)
    target = tmp_path / "new"

    monkeypatch.setattr(
        sm, "_st_dev", lambda p: 1 if str(src_dir) in str(p) else 2
    )

    real_copytree = sm.shutil.copytree

    def _exploding_copytree(s, d, *a, **k):
        # Simulate a crash after the staging dir starts filling.
        real_copytree(s, d, *a, **k)
        raise RuntimeError("simulated power loss mid-copy")

    committed: list = []
    monkeypatch.setattr(sm.shutil, "copytree", _exploding_copytree)
    with pytest.raises(RuntimeError):
        sm.migrate_bundle(src_dir / BUNDLE, target / BUNDLE, committed.append)

    # Original bundle intact + authoritative; config never flipped; no FINAL dst.
    assert (src_dir / BUNDLE / "token").read_text() == TOKEN
    assert committed == []
    assert not (target / BUNDLE).exists()

    # Retry with a working copy → converges: stale partial cleaned, dst complete.
    monkeypatch.setattr(sm.shutil, "copytree", real_copytree)
    outcome = sm.migrate_bundle(
        src_dir / BUNDLE, target / BUNDLE, committed.append
    )
    assert outcome.ok
    assert (target / BUNDLE / "token").read_text() == TOKEN
    assert not (src_dir / BUNDLE).exists()
    assert committed == [target.resolve()]


# ---------------------------------------------------------------------------
# validate_bundle_target — cross-volume allowed, cloud-sync RETAINED
# ---------------------------------------------------------------------------


def test_validate_bundle_target_cross_volume_allowed(tmp_path, monkeypatch):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    target = tmp_path / "external"  # would be a different volume in reality
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: False)
    monkeypatch.setattr(
        sm, "_st_dev", lambda p: 1 if str(src_dir) in str(p) else 2
    )
    res = sm.validate_bundle_target(src_dir, target)
    assert res.ok  # unlike the plaintext move, cross-volume is NOT rejected


def test_validate_bundle_target_cloud_synced_retained(tmp_path, monkeypatch):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    target = tmp_path / "cloud"
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: True)
    res = sm.validate_bundle_target(src_dir, target)
    assert not res.ok
    assert res.code == sm.Reason.CLOUD_SYNCED


def test_validate_bundle_target_same_as_source(tmp_path, monkeypatch):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: False)
    res = sm.validate_bundle_target(src_dir, src_dir)
    assert not res.ok
    assert res.code == sm.Reason.SAME_AS_SOURCE


# ---------------------------------------------------------------------------
# relocate_bundle — quiescent cycle; NEVER force-detach (proof-first)
# ---------------------------------------------------------------------------


def _patch_container(monkeypatch, *, detach=None):
    """Mock the container attach/detach seam; record every detach call."""
    calls: dict = {"detach": [], "attach": []}

    def _detach(target, *, force=True, **k):
        calls["detach"].append({"target": target, "force": force})
        if detach is not None:
            detach()

    def _attempt_mount(bundle, mountpoint, key):
        calls["attach"].append({"bundle": str(bundle), "mountpoint": str(mountpoint)})
        return sl.StoreResolution(sl.StoreState.MOUNTED, mountpoint=str(mountpoint))

    monkeypatch.setattr(container, "require_container_key", lambda: b"k" * 32)
    monkeypatch.setattr(container, "detach", _detach)
    monkeypatch.setattr(sl, "_attempt_mount", _attempt_mount)
    return calls


def test_relocate_bundle_moves_bundle_mountpoint_unchanged(vault_config, monkeypatch):
    cfg = vault_config["cfg"]
    base = vault_config["base"]
    mountpoint = vault_config["mountpoint"]
    target = vault_config["tmp"] / "moved-here"
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: False)
    calls = _patch_container(monkeypatch)

    outcome = sl.relocate_bundle(target)

    assert outcome.ok
    # Bundle physically moved; old location empty; token readable at new location.
    assert (target / BUNDLE / "token").read_text() == TOKEN
    assert not (base / BUNDLE).exists()
    # Config now points the bundle dir at the target; mountpoint unchanged.
    cfg._config_cache = None
    assert cfg.get_store_bundle_dir() == target.resolve()
    assert cfg.get_recordings_dir() == mountpoint
    # Quiescent detach was NEVER forced; store was remounted at the mountpoint.
    assert calls["detach"] == [{"target": str(mountpoint), "force": False}]
    assert calls["attach"][-1]["mountpoint"] == str(mountpoint)
    assert calls["attach"][-1]["bundle"] == str(target / BUNDLE)


def test_relocate_bundle_never_force_detaches_on_busy(vault_config, monkeypatch):
    """A busy detach → typed refusal, NOTHING moved, force never used."""
    base = vault_config["base"]
    mountpoint = vault_config["mountpoint"]
    target = vault_config["tmp"] / "moved-here"
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: False)

    def _raise_busy():
        raise container.ContainerBusyError("volume busy")

    calls = _patch_container(monkeypatch, detach=_raise_busy)
    # If the move ever proceeded past the busy detach it would call migrate_bundle;
    # make that explode so a regression is loud rather than silently moving.
    monkeypatch.setattr(
        sm,
        "migrate_bundle",
        lambda *a, **k: pytest.fail("moved past a busy (force) detach"),
    )

    outcome = sl.relocate_bundle(target)

    assert not outcome.ok
    assert outcome.code == "recording_active"
    # Detach was attempted with force=False and never retried with force.
    assert calls["detach"] == [{"target": str(mountpoint), "force": False}]
    # Nothing moved: bundle still at the original location; target untouched.
    assert (base / BUNDLE / "token").read_text() == TOKEN
    assert not (target / BUNDLE).exists()


# ---------------------------------------------------------------------------
# Daemon verb — refusals (typed reason, nothing moved) + plaintext regression
# ---------------------------------------------------------------------------


class _FakeEncryptJob:
    def __init__(self, running: bool) -> None:
        self._running = running

    def is_running(self) -> bool:
        return self._running


def _app_with_supervisor():
    from screencap.daemon.app import build_app

    app = build_app()
    app.state.supervisor = Supervisor(app.state.event_bus, reconcile_on_init=False)
    return app


def _client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _post_migrate(app, target) -> httpx.Response:
    async with _client(app) as client:
        return await client.post(
            "/v0/storage.migrate", json={"target": str(target)}
        )


async def test_verb_refuses_when_recording_active(vault_config, monkeypatch):
    from screencap.daemon import app as app_module

    base = vault_config["base"]
    target = vault_config["tmp"] / "moved-here"
    monkeypatch.setattr(app_module, "_storage_recording_active", lambda: True)
    monkeypatch.setattr(
        sl, "relocate_bundle", lambda *a, **k: pytest.fail("relocated while recording")
    )

    app = _app_with_supervisor()
    resp = await _post_migrate(app, target)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "recording_active"
    assert (base / BUNDLE / "token").read_text() == TOKEN  # nothing moved


async def test_verb_refuses_when_encrypt_job_running(vault_config, monkeypatch):
    base = vault_config["base"]
    target = vault_config["tmp"] / "moved-here"
    monkeypatch.setattr(
        sl, "relocate_bundle", lambda *a, **k: pytest.fail("relocated while encrypting")
    )

    app = _app_with_supervisor()
    app.state.encrypt_job = _FakeEncryptJob(running=True)
    resp = await _post_migrate(app, target)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "encrypt_in_progress"
    assert (base / BUNDLE / "token").read_text() == TOKEN


async def test_verb_refuses_when_store_sealed(vault_config, monkeypatch):
    base = vault_config["base"]
    target = vault_config["tmp"] / "moved-here"
    sl.write_sealed_sentinel()
    monkeypatch.setattr(
        sl, "relocate_bundle", lambda *a, **k: pytest.fail("relocated while sealed")
    )

    app = _app_with_supervisor()
    resp = await _post_migrate(app, target)

    assert resp.status_code == 409
    assert resp.json()["reason"] == "store_sealed"
    assert (base / BUNDLE / "token").read_text() == TOKEN


async def test_verb_refuses_cloud_synced_target(vault_config, monkeypatch):
    base = vault_config["base"]
    target = vault_config["tmp"] / "cloud-target"
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: True)
    monkeypatch.setattr(
        sl, "relocate_bundle", lambda *a, **k: pytest.fail("relocated to cloud target")
    )

    app = _app_with_supervisor()
    resp = await _post_migrate(app, target)

    assert resp.status_code == 409
    assert resp.json()["reason"] == sm.Reason.CLOUD_SYNCED
    assert (base / BUNDLE / "token").read_text() == TOKEN


async def test_verb_vault_happy_path(vault_config, monkeypatch):
    """End-to-end: bundle moved, mountpoint unchanged, config flipped, 200."""
    cfg = vault_config["cfg"]
    base = vault_config["base"]
    mountpoint = vault_config["mountpoint"]
    target = vault_config["tmp"] / "moved-here"
    monkeypatch.setattr(sm, "_is_cloud_synced", lambda p: False)
    calls = _patch_container(monkeypatch)

    app = _app_with_supervisor()
    resp = await _post_migrate(app, target)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["moved_to"] == str(target / BUNDLE)
    assert (target / BUNDLE / "token").read_text() == TOKEN
    assert not (base / BUNDLE).exists()
    cfg._config_cache = None
    assert cfg.get_store_bundle_dir() == target.resolve()
    assert cfg.get_recordings_dir() == mountpoint
    assert calls["detach"] == [{"target": str(mountpoint), "force": False}]


async def test_verb_legacy_plaintext_path_unchanged(tmp_path, monkeypatch):
    """Container OFF (legacy custom-dir) → the shipped SCR-228 rename path fires."""
    import screencap.config as cfg

    base = tmp_path / "screencap"
    (base / "run").mkdir(parents=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", base / "recordings")
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    cfg._config_cache = None
    source = cfg.get_recordings_dir()
    (source / "rec-1").mkdir(parents=True)

    target = tmp_path / "plaintext-new"

    # Prove the SCR-228 plaintext path is the one taken (not the vault bundle
    # path): spy on both.
    plaintext_calls: list = []

    def _fake_migrate(src, tgt, commit):
        plaintext_calls.append((src, tgt))
        return sm.MigrationOutcome(ok=True, moved_from=str(src), moved_to=str(tgt))

    monkeypatch.setattr(sm, "migrate", _fake_migrate)
    monkeypatch.setattr(
        sl, "relocate_bundle", lambda *a, **k: pytest.fail("vault path taken when off")
    )

    from screencap.daemon.app import _is_vault_migration_install

    assert _is_vault_migration_install() is False

    app = _app_with_supervisor()
    resp = await _post_migrate(app, target)

    assert resp.status_code == 200, resp.text
    assert len(plaintext_calls) == 1  # the shipped rename path fired
