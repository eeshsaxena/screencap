"""Corpus crypto (search guardrails U1): key lifecycle + AES-256-GCM helpers.

Vision-free and Keychain-free — the shared-group / keyring / env-file channels are
all stubbed so the whole file runs in the CI ``pytest -m privacy`` lane on any host.
"""

from __future__ import annotations

import os
import stat

import pytest

from screencap import corpus_crypto as cc

pytestmark = pytest.mark.privacy


# --------------------------------------------------------------------------
# Channel fixtures (mirror tests/test_auth.py's fake_group / fake_keyring)
# --------------------------------------------------------------------------


@pytest.fixture
def _no_env_key(monkeypatch):
    """Ensure the env-file channel is off so the Keychain channels are exercised."""
    monkeypatch.delenv(cc.CORPUS_KEY_FILE_ENV, raising=False)


@pytest.fixture
def group_backend(monkeypatch, tmp_path, _no_env_key):
    """Entitled path: in-memory shared-group store; keyring is a spy that must stay
    unused. Returns the backing dict."""
    import keyring

    from screencap import keychain_group

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_lock_path", lambda: tmp_path / "corpus-key.lock")

    group: dict = {}
    monkeypatch.setattr(
        keychain_group, "store", lambda s, a, sec, g: group.__setitem__((s, a, g), sec)
    )
    monkeypatch.setattr(keychain_group, "load", lambda s, a, g: group.get((s, a, g)))

    def _keyring_forbidden(*_a, **_k):
        raise AssertionError("keyring must not be touched on the entitled group path")

    monkeypatch.setattr(keyring, "get_password", _keyring_forbidden)
    monkeypatch.setattr(keyring, "set_password", _keyring_forbidden)
    return group


@pytest.fixture
def keyring_fallback(monkeypatch, tmp_path, _no_env_key):
    """Un-entitled path: the shared group raises MissingEntitlement, so the module
    falls back to an in-memory keyring. Returns the backing dict."""
    import keyring

    from screencap import keychain_group

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_lock_path", lambda: tmp_path / "corpus-key.lock")

    def _missing(*_a, **_k):
        raise keychain_group.MissingEntitlement(keychain_group.errSecMissingEntitlement, "test")

    monkeypatch.setattr(keychain_group, "store", _missing)
    monkeypatch.setattr(keychain_group, "load", _missing)

    store: dict = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, a, pw: store.__setitem__((s, a), pw))
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    return store


# --------------------------------------------------------------------------
# Symmetric primitives
# --------------------------------------------------------------------------


def test_encrypt_decrypt_jpeg_round_trip():
    key = os.urandom(32)
    jpeg = b"\xff\xd8\xff\xe0" + os.urandom(200_000) + b"\xff\xd9"  # ~200KB JPEG-ish
    aad = cc.corpus_aad("2026-07-11-rec", "1720000000000.jpg")

    token = cc.encrypt(jpeg, key, aad)
    # Ciphertext is framed (magic + nonce prefix) and is NOT the plaintext.
    assert cc.is_encrypted(token)
    assert not token.startswith(b"\xff\xd8")  # no longer a valid JPEG until decrypted
    assert jpeg not in token

    assert cc.decrypt(token, key, aad) == jpeg


def test_tampered_ciphertext_raises_no_partial_plaintext():
    key = os.urandom(32)
    aad = cc.corpus_aad("rec", "a.jpg")
    token = bytearray(cc.encrypt(b"top secret payload", key, aad))
    token[-1] ^= 0x01  # flip a bit in the GCM tag / ciphertext tail

    with pytest.raises(cc.CorpusDecryptError):
        cc.decrypt(bytes(token), key, aad)


def test_bad_magic_raises():
    key = os.urandom(32)
    aad = cc.corpus_aad("rec", "a.jpg")
    with pytest.raises(cc.CorpusDecryptError):
        cc.decrypt(b"\xff\xd8not-encrypted", key, aad)


def test_aad_mismatch_across_recordings_fails():
    """A still swapped into a different recording (or renamed) must not decrypt."""
    key = os.urandom(32)
    token = cc.encrypt(b"frame bytes", key, cc.corpus_aad("rec-A", "5.jpg"))

    with pytest.raises(cc.CorpusDecryptError):
        cc.decrypt(token, key, cc.corpus_aad("rec-B", "5.jpg"))  # wrong recording
    with pytest.raises(cc.CorpusDecryptError):
        cc.decrypt(token, key, cc.corpus_aad("rec-A", "9.jpg"))  # wrong frame name


def test_wrong_key_fails():
    aad = cc.corpus_aad("rec", "a.jpg")
    token = cc.encrypt(b"payload", os.urandom(32), aad)
    with pytest.raises(cc.CorpusDecryptError):
        cc.decrypt(token, os.urandom(32), aad)


def test_encrypt_rejects_wrong_key_length():
    with pytest.raises(ValueError):
        cc.encrypt(b"x", b"short", cc.corpus_aad("r", "n"))


# --------------------------------------------------------------------------
# Key lifecycle — env-file channel
# --------------------------------------------------------------------------


def test_env_file_channel_generates_persists_and_reloads(monkeypatch, tmp_path):
    key_file = tmp_path / "corpus.key"
    monkeypatch.setenv(cc.CORPUS_KEY_FILE_ENV, str(key_file))
    monkeypatch.setattr(cc, "_lock_path", lambda: tmp_path / "corpus-key.lock")

    assert cc.load_corpus_key() is None  # absent before first create
    key = cc.get_or_create_corpus_key()
    assert len(key) == 32
    assert key_file.exists()
    # 0600 perms (owner rw only).
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600

    assert cc.load_corpus_key() == key  # reload sees the same key
    assert cc.get_or_create_corpus_key() == key  # idempotent


# --------------------------------------------------------------------------
# Key lifecycle — shared group + keyring fallback
# --------------------------------------------------------------------------


def test_group_channel_persists_across_store_load(group_backend):
    key = cc.get_or_create_corpus_key()
    assert len(key) == 32
    assert cc.load_corpus_key() == key  # persisted to the shared group
    assert cc.get_or_create_corpus_key() == key  # idempotent, no second mint


def test_missing_entitlement_falls_back_to_keyring(keyring_fallback):
    """errSecMissingEntitlement → keyring fallback stores/loads the same key."""
    key = cc.get_or_create_corpus_key()
    assert len(key) == 32
    # It landed in the keyring store, not the (raising) group.
    assert keyring_fallback  # non-empty
    assert cc.load_corpus_key() == key
    assert cc.get_or_create_corpus_key() == key


def test_corrupt_stored_key_treated_as_absent(group_backend):
    # A stored value that is not a valid 32-byte base64 key is treated as absent
    # (gate degrades to not-ready) rather than crashing the reader.
    group_backend[(cc.CORPUS_KEYCHAIN_SERVICE, cc.CORPUS_KEYCHAIN_ACCOUNT, cc.KEYCHAIN_ACCESS_GROUP)] = "not-base64!!"
    assert cc.load_corpus_key() is None


def test_corpus_key_unavailable_when_all_channels_absent(monkeypatch, tmp_path, _no_env_key):
    """No env file, group un-entitled, keyring write fails → CorpusKeyUnavailable."""
    import keyring

    from screencap import keychain_group

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc, "_lock_path", lambda: tmp_path / "corpus-key.lock")

    def _missing(*_a, **_k):
        raise keychain_group.MissingEntitlement(keychain_group.errSecMissingEntitlement, "test")

    monkeypatch.setattr(keychain_group, "store", _missing)
    monkeypatch.setattr(keychain_group, "load", _missing)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: None)

    def _keyring_write_fails(*_a, **_k):
        raise RuntimeError("no keyring backend")

    monkeypatch.setattr(keyring, "set_password", _keyring_write_fails)

    assert cc.load_corpus_key() is None
    with pytest.raises(cc.CorpusKeyUnavailable):
        cc.get_or_create_corpus_key()


def test_load_returns_none_on_group_keychain_error_never_regenerates(monkeypatch, tmp_path, _no_env_key):
    """A transient (non-entitlement) Keychain read failure is 'absent', never a
    silent regenerate — load stays read-only."""
    from screencap import keychain_group

    monkeypatch.setattr(cc.sys, "platform", "darwin")

    def _boom(*_a, **_k):
        raise keychain_group.KeychainError(-25308, "SecItemCopyMatching")

    monkeypatch.setattr(keychain_group, "load", _boom)
    assert cc.load_corpus_key() is None
