"""Tests for :mod:`screencap.container`'s U2 key management (split-custody, KTD-22).

Pure-logic, ``@pytest.mark.privacy``, Vision-free. ``keyring`` and
``keychain_group`` are always MOCKED — no test here touches the real Keychain.
The mocks replace ``screencap.keychain_group.load`` / ``.store`` (which
``container`` imports lazily as ``from screencap import keychain_group``, so
patching the module attributes intercepts every call) and inject a fake
``keyring`` module for the off-Mac branch.

The crux is the KTD-22 split: on a bundle-present store, an ``errSecMissingEntitlement``
read yields the *entitlement-mismatch* diagnosis (:class:`ContainerKeyUnreachableError`,
NOT a "key missing" message), while a genuinely-absent key yields the *genuine-loss*
diagnosis (:class:`ContainerKeyMissingError`). The two must never be confused — a
false data-loss alarm for a developer running an un-entitled pip/Debug daemon is the
exact failure KTD-22 forbids.
"""

from __future__ import annotations

import sys
import types

import pytest

from screencap import container, keychain_group

pytestmark = pytest.mark.privacy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_key_file_env(monkeypatch):
    """Ensure the key-file channel is off so tests exercise the Keychain path."""
    monkeypatch.delenv(container.CONTAINER_KEY_FILE_ENV, raising=False)


def _patch_keychain(monkeypatch, *, load=None, store=None):
    """Replace ``keychain_group.load`` / ``.store`` with the given callables.

    Any unpatched side (default) raises to make an unexpected call loud — e.g. a
    read-only test that accidentally writes.

    Forces the darwin shared-group branch so these tests exercise the
    ``keychain_group`` path (where the KTD-22 entitlement logic lives) on any
    runner — otherwise a Linux CI host takes the ``keyring`` fallback and hits a
    real (absent) keyring backend. The keyring branch has its own dedicated test.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    if load is None:
        def load(*a, **k):  # noqa: ANN001
            raise AssertionError("keychain_group.load must not be called")
    if store is None:
        def store(*a, **k):  # noqa: ANN001
            raise AssertionError("keychain_group.store must not be called")
    monkeypatch.setattr(keychain_group, "load", load, raising=True)
    monkeypatch.setattr(keychain_group, "store", store, raising=True)


def _write_sentinel_bundle(tmp_path):
    """Create a stand-in ``store.sparsebundle`` with known bytes; return (path, bytes)."""
    bundle = tmp_path / container.BUNDLE_NAME
    payload = b"\x00encrypted-store-do-not-touch\xff"
    bundle.write_bytes(payload)
    return bundle, payload


# ---------------------------------------------------------------------------
# get_container_key: read-only, genuinely-absent
# ---------------------------------------------------------------------------


def test_get_container_key_absent_returns_none_and_never_writes(monkeypatch):
    """Genuinely absent (Keychain has no item) → None, and nothing is written."""
    _patch_keychain(monkeypatch, load=lambda *a, **k: None)  # store default = asserts
    assert container.get_container_key() is None


# ---------------------------------------------------------------------------
# create_container_key: never orphan an existing store
# ---------------------------------------------------------------------------


def test_create_container_key_refuses_when_bundle_exists(monkeypatch, tmp_path):
    """A bundle already on disk → refuse (raise); the bundle bytes are untouched
    and no key is ever persisted."""
    bundle, payload = _write_sentinel_bundle(tmp_path)
    _patch_keychain(monkeypatch)  # both load + store assert-if-called

    with pytest.raises(container.ContainerError) as excinfo:
        container.create_container_key(str(bundle))

    assert isinstance(excinfo.value, container.ContainerOperatorError)
    assert excinfo.value.retryable is False
    assert bundle.read_bytes() == payload  # bytes untouched


def test_create_container_key_mints_and_persists_when_bundle_absent(monkeypatch, tmp_path):
    """No bundle → mint a 256-bit key and persist it once to the shared group."""
    stored: dict[str, str] = {}

    def fake_store(service, account, secret, group, *, synchronizable=False):  # noqa: ANN001
        assert service == container.CONTAINER_KEYCHAIN_SERVICE
        assert account == container.CONTAINER_KEYCHAIN_ACCOUNT
        assert group == container.KEYCHAIN_ACCESS_GROUP
        assert synchronizable is False  # device-local, never iCloud-synced
        stored["v"] = secret

    _patch_keychain(monkeypatch, store=fake_store)
    key = container.create_container_key(str(tmp_path / container.BUNDLE_NAME))

    assert len(key) == 32
    assert "v" in stored  # persisted exactly once


# ---------------------------------------------------------------------------
# KTD-22 split: entitlement mismatch vs. genuine loss on a bundle-present store
# ---------------------------------------------------------------------------


def test_ktd22_entitlement_mismatch_is_not_key_missing(monkeypatch, tmp_path):
    """MissingEntitlement on a bundle-present store → ContainerKeyUnreachableError.

    The message must name the entitlement mismatch and NOT read as data loss; the
    bundle bytes stay untouched; the error is operator-class (exit 1) but distinct
    from :class:`ContainerKeyMissingError`.
    """
    bundle, payload = _write_sentinel_bundle(tmp_path)

    def raise_missing_entitlement(*a, **k):  # noqa: ANN001
        raise keychain_group.MissingEntitlement(
            keychain_group.errSecMissingEntitlement, "SecItemCopyMatching"
        )

    _patch_keychain(monkeypatch, load=raise_missing_entitlement)

    with pytest.raises(container.ContainerKeyUnreachableError) as excinfo:
        container.require_container_key()

    # Distinct from the genuine-loss type (must not be caught as "key missing").
    assert not isinstance(excinfo.value, container.ContainerKeyMissingError)
    msg = str(excinfo.value).lower()
    assert "entitle" in msg  # names the cause
    assert "not data loss" in msg  # explicitly reassures it is not loss
    assert "key missing" not in msg  # does NOT read as the genuine-loss diagnosis
    assert bundle.read_bytes() == payload  # bundle untouched
    assert excinfo.value.exit_code == 1


def test_ktd22_genuinely_absent_is_key_missing(monkeypatch, tmp_path):
    """No key in any channel on a bundle-present store → ContainerKeyMissingError.

    The genuine-loss path: the message reads as key loss, and — the KTD-22 point —
    it is a *different* type than the entitlement-mismatch case. Bundle untouched.
    """
    bundle, payload = _write_sentinel_bundle(tmp_path)
    _patch_keychain(monkeypatch, load=lambda *a, **k: None)

    with pytest.raises(container.ContainerKeyMissingError) as excinfo:
        container.require_container_key()

    assert not isinstance(excinfo.value, container.ContainerKeyUnreachableError)
    msg = str(excinfo.value).lower()
    assert "missing" in msg or "lost" in msg  # genuine-loss framing
    assert "entitle" not in msg  # NOT the entitlement diagnosis
    assert bundle.read_bytes() == payload  # bundle untouched
    assert excinfo.value.exit_code == 1


def test_ktd22_get_container_key_surfaces_entitlement_never_falls_back(monkeypatch):
    """KTD-22 core: get_container_key surfaces MissingEntitlement — it does NOT
    silently fall back to a legacy keyring item and hand back a different key."""
    def raise_missing_entitlement(*a, **k):  # noqa: ANN001
        raise keychain_group.MissingEntitlement(
            keychain_group.errSecMissingEntitlement, "SecItemCopyMatching"
        )

    # A poisoned keyring: if the container ever fell back to it, it would return a
    # WRONG key and this test would fail to raise.
    fake_keyring = types.ModuleType("keyring")
    fake_keyring.errors = types.SimpleNamespace(KeyringLocked=type("KeyringLocked", (Exception,), {}))
    fake_keyring.get_password = lambda *a, **k: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAg="
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)

    _patch_keychain(monkeypatch, load=raise_missing_entitlement)

    with pytest.raises(container.ContainerKeyUnreachableError):
        container.get_container_key()


# ---------------------------------------------------------------------------
# Locked Keychain / keyring → retryable class (exit 75)
# ---------------------------------------------------------------------------


def test_locked_shared_group_maps_to_retryable(monkeypatch):
    """The shared-group locked equivalent (errSecInteractionNotAllowed) →
    KeychainLockedError (retryable, exit 75), not an operator hard stop."""
    def raise_locked(*a, **k):  # noqa: ANN001
        raise keychain_group.KeychainError(
            keychain_group.errSecInteractionNotAllowed, "SecItemCopyMatching"
        )

    _patch_keychain(monkeypatch, load=raise_locked)

    with pytest.raises(container.KeychainLockedError) as excinfo:
        container.get_container_key()

    assert isinstance(excinfo.value, container.ContainerRetryableError)
    assert excinfo.value.retryable is True
    assert excinfo.value.exit_code == 75


def test_keyring_locked_maps_to_retryable(monkeypatch):
    """The off-Mac ``keyring.errors.KeyringLocked`` also maps to the retryable class."""

    class KeyringLocked(Exception):
        pass

    fake_keyring = types.ModuleType("keyring")
    fake_keyring.errors = types.SimpleNamespace(KeyringLocked=KeyringLocked)

    def get_password(*a, **k):  # noqa: ANN001
        raise KeyringLocked("locked")

    fake_keyring.get_password = get_password
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)
    monkeypatch.setitem(sys.modules, "keyring.errors", fake_keyring.errors)
    monkeypatch.setattr(sys, "platform", "linux")  # force the keyring branch

    with pytest.raises(container.KeychainLockedError) as excinfo:
        container.get_container_key()

    assert excinfo.value.retryable is True
    assert excinfo.value.exit_code == 75


# ---------------------------------------------------------------------------
# Key round-trips through the passphrase pipe with no encoding drift
# ---------------------------------------------------------------------------


def test_key_roundtrips_through_passphrase_pipe_unchanged(monkeypatch, tmp_path):
    """The bytes create_container_key mints/stores are exactly what get_container_key
    returns AND exactly what the hdiutil passphrase helper (KTD-6) would pipe — no
    base64/newline drift anywhere in the create → store → load → attach path."""
    stored: dict[str, str] = {}

    def fake_store(service, account, secret, group, *, synchronizable=False):  # noqa: ANN001
        stored["v"] = secret

    def fake_load(service, account, group, *, synchronizable=False):  # noqa: ANN001
        return stored.get("v")

    _patch_keychain(monkeypatch, load=fake_load, store=fake_store)

    key = container.create_container_key(str(tmp_path / container.BUNDLE_NAME))
    assert len(key) == 32
    # Round-trips through the Keychain (base64) with no drift.
    assert container.get_container_key() == key

    # And the exact bytes reach the passphrase pipe untouched (no trailing newline).
    piped: dict[str, bytes | None] = {}

    def fake_run(args, *, key=None, timeout=None, check=True):  # noqa: ANN001
        piped["key"] = key
        return container._HdiutilResult(0, b"", b"")

    monkeypatch.setattr(container, "_run_hdiutil", fake_run)
    container.create_bundle(str(tmp_path / "other.sparsebundle"), key, size_bytes=1024)

    assert piped["key"] == key
    assert not piped["key"].endswith(b"\n")
