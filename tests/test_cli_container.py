"""SCR-258 U5 — CLI ``storage`` group + encrypted-store funnel guard.

Pure-logic, ``@pytest.mark.privacy``, Vision-free. Everything hardware- or
Keychain- or LocalAuthentication-bound is MOCKED — **no real ``hdiutil``, no real
Keychain, no real LocalAuthentication (objc)**:

* ``container.create_container_key`` / ``create_bundle`` / ``get_container_key`` /
  ``require_container_key`` are patched (the key/bundle seam).
* ``screencap.cli._evaluate_local_authentication`` is stubbed to pass / fail /
  no-surface (the present-user gate).
* ``screencap.cli._daemon_is_reachable`` is forced False so the CLI-local
  seal/unseal FALLBACK path runs (its role: only when no daemon responds).

The two proof-first invariants (per the unit brief):

1. **Funnel guard never mkdirs plaintext** — ``view`` / ``export`` / ``info`` on
   a sealed store render a locked-state error and exit non-zero, and NO plaintext
   directory appears at the recordings mountpoint.
2. **LA-fail keeps the sentinel** — a CLI-local unseal whose present-user auth
   fails or is cancelled leaves the sealed sentinel intact and exits non-zero.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import screencap.cli as cli_mod
from screencap import container
from screencap.cli import cli

pytestmark = pytest.mark.privacy


def _norm(output: str) -> str:
    """Collapse rich's line-wrapping so multi-word phrase assertions are
    wrap-insensitive.

    An 80-col ``Console`` (no TTY under ``CliRunner``) can split a phrase across
    a newline when a long tmp path pushes the wrap boundary — a macOS
    ``/private/var/folders/…`` path is long enough to break ``reused the\\nexisting
    key``, while a short Linux ``/tmp/…`` path wraps elsewhere. That made a raw
    substring check a macOS-CI-only failure; normalizing whitespace fixes it.
    """
    return " ".join(output.lower().split())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Isolate the base dir + recordings mountpoint with the container ON.

    ``store_bundle_path`` / ``sealed_sentinel_path`` / ``mount_lock_path`` all
    resolve through ``config.get_base_dir`` (``_DEFAULT_BASE``), so redirecting it
    at a tmp tree isolates the whole store. ``SCREENCAP_CONTAINER_ENABLED=1``
    drives ``get_container_enabled``; the mountpoint is a tmp path that must NOT
    be created by a guarded command on a sealed store.
    """
    import screencap.config as cfg

    base = tmp_path / "screencap"
    (base / "run").mkdir(parents=True)
    # Force the darwin shared-group key path so store-touching commands don't
    # take the keyring fallback and hit a real (absent) keyring backend on a
    # Linux CI runner — the vault key channels are macOS-only in practice.
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    mountpoint = tmp_path / "recordings"  # deliberately NOT created here
    # Point the mountpoint via the default, NOT SCREENCAP_RECORDINGS_DIR — the
    # env override is the plaintext bypass seam, so setting it would (correctly)
    # make the container inactive and the funnel guard a no-op.
    monkeypatch.setattr(cfg, "_DEFAULT_RECORDINGS", mountpoint)
    monkeypatch.delenv("SCREENCAP_RECORDINGS_DIR", raising=False)
    monkeypatch.setenv("SCREENCAP_CONTAINER_ENABLED", "1")
    # Key-file channel off + never touch a real Keychain by accident.
    monkeypatch.delenv(container.CONTAINER_KEY_FILE_ENV, raising=False)
    cfg.invalidate_config_cache()
    yield type("Env", (), {"base": base, "mountpoint": mountpoint})
    cfg.invalidate_config_cache()


def _bundle_path() -> Path:
    from screencap.daemon import store_lifecycle as sl

    return sl.store_bundle_path()


def _sentinel_path() -> Path:
    from screencap.daemon import store_lifecycle as sl

    return sl.sealed_sentinel_path()


def _make_bundle() -> Path:
    """Create a stand-in bundle directory (the resolver only ``.exists()``-gates)."""
    bundle = _bundle_path()
    (bundle / "bands").mkdir(parents=True)
    (bundle / "bands" / "0").write_bytes(b"ENCRYPTED-BAND")
    return bundle


def _plant_sentinel() -> Path:
    sentinel = _sentinel_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("sealed\n")
    return sentinel


# ---------------------------------------------------------------------------
# storage init
# ---------------------------------------------------------------------------


def test_init_absent_creates_key_and_bundle(store_env, monkeypatch):
    """ABSENT store -> mints a key + creates the bundle (both seam calls fire)."""
    calls = {}

    def _create_key(bundle_path):
        calls["key"] = bundle_path
        return b"k" * 32

    def _create_bundle(bundle_path, key, **kw):
        calls["bundle"] = (bundle_path, key)
        Path(bundle_path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(container, "get_container_key", lambda: None)
    monkeypatch.setattr(container, "create_container_key", _create_key)
    monkeypatch.setattr(container, "create_bundle", _create_bundle)

    result = CliRunner().invoke(cli, ["storage", "init"])

    assert result.exit_code == 0, result.output
    assert calls["key"] == str(_bundle_path())
    assert calls["bundle"][0] == str(_bundle_path())
    assert calls["bundle"][1] == b"k" * 32
    assert "created" in result.output.lower()


def test_init_refuses_plaintext_library_at_mountpoint(store_env, monkeypatch):
    """A non-empty plaintext recordings dir refuses a bare init (needs migration).

    Creating the bundle while a plaintext library occupies the mountpoint wedges
    every subsequent daemon start into the occupied-mountpoint error — the
    library must go through the encrypt-migration flow instead. ``storage init``
    is the single creation path (KTD-5), so this one guard also covers the app's
    onboarding auto-init and its "Set up encrypted storage" action."""
    (store_env.mountpoint / "rec-a").mkdir(parents=True)
    (store_env.mountpoint / "rec-a" / "recording.db").write_bytes(b"AAA")

    def _boom(*a, **k):
        raise AssertionError("must not create a bundle over a plaintext library")

    monkeypatch.setattr(container, "create_container_key", _boom)
    monkeypatch.setattr(container, "create_bundle", _boom)

    result = CliRunner().invoke(cli, ["storage", "init"])

    assert result.exit_code == 1
    assert not _bundle_path().exists()
    assert "existing recordings" in _norm(result.output)
    assert "storage encrypt start" in _norm(result.output)


def test_init_idempotent_noop_on_existing_store(store_env, monkeypatch):
    """An already-initialized store is a no-op — no key/bundle creation."""
    _make_bundle()

    def _boom(*a, **k):
        raise AssertionError("must not create on an existing store")

    monkeypatch.setattr(container, "create_container_key", _boom)
    monkeypatch.setattr(container, "create_bundle", _boom)

    result = CliRunner().invoke(cli, ["storage", "init"])

    assert result.exit_code == 0, result.output
    assert "already initialized" in _norm(result.output)


def test_init_bundle_absent_key_present_reuses_key(store_env, monkeypatch):
    """bundle-absent + key-present -> reuse the existing key, never mint a new one."""
    existing = b"E" * 32
    minted = {"called": False}

    def _no_mint(*a, **k):
        minted["called"] = True
        raise AssertionError("must not mint a second key")

    seen = {}

    def _create_bundle(bundle_path, key, **kw):
        seen["key"] = key
        Path(bundle_path).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(container, "get_container_key", lambda: existing)
    monkeypatch.setattr(container, "create_container_key", _no_mint)
    monkeypatch.setattr(container, "create_bundle", _create_bundle)

    result = CliRunner().invoke(cli, ["storage", "init"])

    assert result.exit_code == 0, result.output
    assert minted["called"] is False
    assert seen["key"] == existing
    assert "reused the existing key" in _norm(result.output)


# ---------------------------------------------------------------------------
# CLI-local seal fallback (no daemon)
# ---------------------------------------------------------------------------


def test_local_seal_writes_sentinel_0600(store_env, monkeypatch):
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: False)

    result = CliRunner().invoke(cli, ["storage", "lock"])

    assert result.exit_code == 0, result.output
    sentinel = _sentinel_path()
    assert sentinel.exists()
    assert (sentinel.stat().st_mode & 0o777) == 0o600


def test_local_seal_rejects_preplanted_symlink(store_env, monkeypatch):
    """A symlink AT the sentinel path is refused (O_NOFOLLOW), never followed."""
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: False)

    sentinel = _sentinel_path()
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    target = store_env.base / "attacker-target"
    sentinel.symlink_to(target)

    result = CliRunner().invoke(cli, ["storage", "lock"])

    assert result.exit_code != 0
    # The symlink target must never be written through.
    assert not target.exists()
    assert sentinel.is_symlink()  # the link itself is left untouched


def test_lock_uses_daemon_verb_when_daemon_running(store_env, monkeypatch):
    """SCR-258 U9: a live daemon runs the safe stop→quiesce→detach→seal chain via
    ``POST /v0/storage.lock`` (the fallback local seal runs ONLY with no daemon).
    """
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: True)

    called = {"lock": 0}

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def storage_lock(self):
            called["lock"] += 1
            return {"ok": True, "store_state": "locked", "sealed": True}

    import screencap.cli._daemon_client as dc

    monkeypatch.setattr(dc, "DaemonHTTPClient", lambda *a, **k: _FakeClient())

    result = CliRunner().invoke(cli, ["storage", "lock"])

    assert result.exit_code == 0, result.output
    assert called["lock"] == 1
    # The CLI-local fallback sentinel is NOT written — the daemon owns the seal.
    assert not _sentinel_path().exists()
    assert "locked" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI-local unseal — present-user (LocalAuthentication) gate (AE4 CLI arm)
# ---------------------------------------------------------------------------


def test_local_unseal_la_pass_clears_sentinel(store_env, monkeypatch):
    _plant_sentinel()
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: False)
    monkeypatch.setattr(cli_mod, "_evaluate_local_authentication", lambda reason: None)

    result = CliRunner().invoke(cli, ["storage", "unlock"])

    assert result.exit_code == 0, result.output
    assert not _sentinel_path().exists()
    assert "unlocked" in result.output.lower()


def test_local_unseal_la_fail_keeps_sentinel(store_env, monkeypatch):
    """AE4: a failed / cancelled present-user auth leaves the sentinel intact."""
    sentinel = _plant_sentinel()
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: False)

    def _deny(reason):
        raise PermissionError("user cancelled")

    monkeypatch.setattr(cli_mod, "_evaluate_local_authentication", _deny)

    result = CliRunner().invoke(cli, ["storage", "unlock"])

    assert result.exit_code != 0
    assert sentinel.exists()  # still sealed
    assert "stays sealed" in _norm(result.output)


def test_local_unseal_no_la_surface_refuses_with_recovery_route(store_env, monkeypatch):
    """No LA surface (SSH / headless) -> refuse, name present-user + recovery route."""
    sentinel = _plant_sentinel()
    monkeypatch.setattr(cli_mod, "_daemon_is_reachable", lambda: False)

    def _no_surface(reason):
        raise cli_mod._NoLocalAuthSurface("no console session")

    monkeypatch.setattr(cli_mod, "_evaluate_local_authentication", _no_surface)

    result = CliRunner().invoke(cli, ["storage", "unlock"])

    assert result.exit_code != 0
    assert sentinel.exists()
    out = " ".join(result.output.lower().split())  # collapse rich wrapping
    assert "present-user" in out
    assert "local" in out  # names the recovery route (local session)


# ---------------------------------------------------------------------------
# Funnel guard — sealed store never mkdirs plaintext at the mountpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cmd", [["info", "rec1"], ["view", "rec1"], ["export", "rec1"]])
def test_funnel_guard_sealed_store_no_plaintext_mkdir(store_env, monkeypatch, cmd):
    _make_bundle()
    _plant_sentinel()

    # Any mount / key touch while sealed is a bug — make it loud.
    def _boom(*a, **k):
        raise AssertionError("sealed store must not attempt a mount / key read")

    monkeypatch.setattr(container, "attach", _boom)
    monkeypatch.setattr(container, "require_container_key", _boom)

    result = CliRunner().invoke(cli, cmd)

    assert result.exit_code != 0, result.output
    assert "locked" in result.output.lower()
    # The load-bearing invariant: no plaintext directory at the mountpoint.
    assert not store_env.mountpoint.exists()


def test_funnel_guard_absent_store_names_init(store_env, monkeypatch):
    # Container on, no bundle, no sentinel -> ABSENT.
    def _boom(*a, **k):
        raise AssertionError("absent store must not attempt a mount")

    monkeypatch.setattr(container, "attach", _boom)

    result = CliRunner().invoke(cli, ["info", "rec1"])

    assert result.exit_code != 0
    assert "storage init" in _norm(result.output)
    assert not store_env.mountpoint.exists()


def test_funnel_guard_key_missing_vs_entitlement(store_env, monkeypatch):
    """ERROR reasons render distinct guidance (R10 / KTD-22), no plaintext dir."""
    _make_bundle()

    # Genuine key loss.
    monkeypatch.setattr(
        container,
        "require_container_key",
        lambda: (_ for _ in ()).throw(container.ContainerKeyMissingError("gone")),
    )
    monkeypatch.setattr(container, "attach", lambda *a, **k: pytest.fail("attached"))
    res_missing = CliRunner().invoke(cli, ["info", "rec1"])
    assert res_missing.exit_code != 0
    # Collapse rich's line-wrapping before matching.
    missing_out = " ".join(res_missing.output.lower().split())
    assert "key is missing" in missing_out

    # Entitlement mismatch.
    monkeypatch.setattr(
        container,
        "require_container_key",
        lambda: (_ for _ in ()).throw(container.ContainerKeyUnreachableError("mismatch")),
    )
    res_mismatch = CliRunner().invoke(cli, ["info", "rec1"])
    assert res_mismatch.exit_code != 0
    mismatch_out = " ".join(res_mismatch.output.lower().split())
    assert "not entitled" in mismatch_out
    assert not store_env.mountpoint.exists()


# ---------------------------------------------------------------------------
# Flag OFF -> zero new behavior (regression)
# ---------------------------------------------------------------------------


def test_flag_off_guard_is_noop(tmp_path, monkeypatch):
    """With the container disabled, the guard never resolves store state."""
    import screencap.config as cfg

    base = tmp_path / "screencap"
    base.mkdir(parents=True)
    monkeypatch.setattr(cfg, "_DEFAULT_BASE", base)
    monkeypatch.setattr(cfg, "_CONFIG_PATH", base / "config.toml")
    monkeypatch.setenv("SCREENCAP_RECORDINGS_DIR", str(tmp_path / "recordings"))
    monkeypatch.delenv("SCREENCAP_CONTAINER_ENABLED", raising=False)
    cfg.invalidate_config_cache()

    from screencap.daemon import store_lifecycle as sl

    def _boom(*a, **k):
        raise AssertionError("resolve_store_state must not run with the flag off")

    monkeypatch.setattr(sl, "resolve_store_state", _boom)

    # ``info`` on a missing recording exits 1 with the ORDINARY not-found message,
    # never the store-state path — proving the guard is a no-op when off.
    result = CliRunner().invoke(cli, ["info", "does-not-exist"])
    assert result.exit_code != 0
    assert "not found" in _norm(result.output)
    cfg.invalidate_config_cache()


def test_cli_help_does_not_eager_import_heavy(monkeypatch):
    """``screencap --help`` stays lazy — no daemon/container/LA import at load."""
    import subprocess
    import sys

    code = (
        "import sys; import screencap.cli;"
        "bad=[m for m in ('screencap.daemon','screencap.container',"
        "'LocalAuthentication') if m in sys.modules];"
        "print('LOADED:'+','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert "LOADED:" in out.stdout
    assert out.stdout.strip() == "LOADED:", out.stdout
