"""U2 — Keychain-backed container key lifecycle (SCR-236, KTD-5).

The container passphrase read path is strictly read-only and can never
orphan an existing store: ``get_container_key`` never creates, and
``create_container_key`` refuses when the bundle already exists. A locked
Keychain maps to a retryable failure (``EX_TEMPFAIL``), not a hard stop.

Privacy-marked + Vision-free so the CI privacy lane runs it; ``keyring`` is
monkeypatched (no real Keychain access).
"""

from __future__ import annotations

import base64

import keyring
import keyring.errors
import pytest

from screencap import container

pytestmark = pytest.mark.privacy


@pytest.fixture
def fake_keychain(monkeypatch):
    """An in-memory stand-in for the login Keychain."""
    store: dict[tuple[str, str], str] = {}

    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    monkeypatch.setattr(keyring, "set_password", lambda s, a, v: store.__setitem__((s, a), v))
    return store


def test_get_returns_none_when_missing_and_never_writes(fake_keychain):
    assert container.get_container_key() is None
    assert fake_keychain == {}


def test_create_then_get_round_trips_unchanged(fake_keychain, tmp_path):
    bundle = tmp_path / "store.sparsebundle"  # does not exist yet
    key = container.create_container_key(bundle)

    # get returns exactly what create stored — no encoding drift between
    # create-time and attach-time (would fail every attach otherwise).
    assert container.get_container_key() == key

    # passphrase is ASCII base64 of 32 random bytes: newline/NUL-free
    # (KTD-6 safe on hdiutil stdin).
    assert b"\n" not in key
    assert key == key.strip()
    assert len(base64.b64decode(key)) == container._KEY_RANDOM_BYTES


def test_create_refuses_when_bundle_exists(fake_keychain, tmp_path):
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()  # a store already lives here
    with pytest.raises(container.FatalContainerError, match="orphan"):
        container.create_container_key(bundle)
    # never wrote a key that would orphan the existing store
    assert fake_keychain == {}


def test_get_is_read_only_even_with_bundle_present(fake_keychain, tmp_path):
    """AE2 precursor: bundle present, key gone → ``get`` returns ``None``
    (never re-creates). U4's ``ensure_store_mounted`` turns this ``None``
    into ``ContainerKeyMissingError`` (a loud hard stop), leaving the
    bundle bytes untouched."""
    bundle = tmp_path / "store.sparsebundle"
    bundle.mkdir()
    assert container.get_container_key() is None
    assert fake_keychain == {}


def test_key_missing_error_is_fatal():
    assert container.ContainerKeyMissingError().exit_code == 1
    assert issubclass(container.ContainerKeyMissingError, container.FatalContainerError)


def test_get_keyring_locked_maps_to_retryable(monkeypatch):
    def locked(*a, **k):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(keyring, "get_password", locked)
    with pytest.raises(container.ContainerKeyLockedError) as excinfo:
        container.get_container_key()
    # daemon exits 75 (launchd retries), not 1
    assert excinfo.value.exit_code == container.EX_TEMPFAIL == 75


def test_create_keyring_locked_maps_to_retryable(monkeypatch, tmp_path):
    bundle = tmp_path / "store.sparsebundle"

    def locked(*a, **k):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(keyring, "set_password", locked)
    with pytest.raises(container.ContainerKeyLockedError):
        container.create_container_key(bundle)


def test_get_generic_keyring_error_maps_to_retryable(monkeypatch):
    """A non-locked KeyringError (e.g. a headless ACL prompt the daemon can't
    answer — the cross-binary case) maps to a retryable failure so serve()
    exits EX_TEMPFAIL cleanly instead of crashing on an uncaught exception."""
    def boom(*a, **k):
        raise keyring.errors.KeyringError("interaction not allowed")

    monkeypatch.setattr(keyring, "get_password", boom)
    with pytest.raises(container.ContainerKeyLockedError) as excinfo:
        container.get_container_key()
    assert excinfo.value.exit_code == container.EX_TEMPFAIL
