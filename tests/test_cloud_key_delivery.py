"""U4 — cloud-E2EE flag, key resolver, and daemon→engine key delivery."""

from __future__ import annotations

import base64
import os
import stat

import keyring
import pytest

from screencap import cloud_crypto as cc
from screencap import config

pytestmark = pytest.mark.privacy

KEY = b"K" * 32
ENV = cc.ENGINE_CLOUD_KEY_FILE_ENV


# --------------------------------------------------------------------------
# Flag: env > config.toml > default-off
# --------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("SCREENCAP_CLOUD_E2EE", raising=False)
    monkeypatch.setattr(config, "_load_toml", lambda: {})
    assert config.get_cloud_e2ee_enabled() is False


def test_flag_env_on(monkeypatch):
    monkeypatch.setenv("SCREENCAP_CLOUD_E2EE", "true")
    assert config.get_cloud_e2ee_enabled() is True


def test_flag_toml_on(monkeypatch):
    monkeypatch.delenv("SCREENCAP_CLOUD_E2EE", raising=False)
    monkeypatch.setattr(config, "_load_toml", lambda: {"cloud_e2ee_enabled": True})
    assert config.get_cloud_e2ee_enabled() is True


# --------------------------------------------------------------------------
# Resolver: engine reads the delivered file; every other context reads Keychain
# --------------------------------------------------------------------------


def test_resolve_uses_keychain_when_not_engine(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    store = {(cc.CLOUD_SERVICE, cc.CLOUD_KEK_ACCOUNT): base64.b64encode(KEY).decode()}
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    assert cc.resolve_cloud_key() == KEY


def test_resolve_uses_delivered_file_in_engine(tmp_path, monkeypatch):
    p = tmp_path / "engine-cloud-key-rec.b64"
    p.write_bytes(base64.b64encode(KEY))
    os.chmod(p, 0o600)
    monkeypatch.setenv(ENV, str(p))
    # In the engine context the Keychain must NOT be consulted.
    monkeypatch.setattr(
        keyring, "get_password", lambda s, a: pytest.fail("engine read the Keychain")
    )
    assert cc.read_delivered_cloud_key() == KEY
    assert cc.resolve_cloud_key() == KEY


def test_read_delivered_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV, str(tmp_path / "does-not-exist.b64"))
    assert cc.read_delivered_cloud_key() is None


def test_read_delivered_empty_returns_none(tmp_path, monkeypatch):
    p = tmp_path / "empty.b64"
    p.write_bytes(b"")
    monkeypatch.setenv(ENV, str(p))
    assert cc.read_delivered_cloud_key() is None


def test_resolve_none_when_env_unset_and_no_keychain(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: None)
    assert cc.resolve_cloud_key() is None


# --------------------------------------------------------------------------
# Daemon-side hardened write (Supervisor._write_engine_cloud_key_file)
# --------------------------------------------------------------------------


def test_write_is_0600_and_round_trips(tmp_path, monkeypatch):
    from screencap.daemon.supervisor import Supervisor

    path = tmp_path / "engine-cloud-key-rec.b64"
    Supervisor._write_engine_cloud_key_file(path, KEY)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    monkeypatch.setenv(ENV, str(path))
    assert cc.read_delivered_cloud_key() == KEY


def test_write_refuses_pre_planted_symlink(tmp_path):
    from screencap.daemon.supervisor import Supervisor

    path = tmp_path / "engine-cloud-key-rec.b64"
    # The writer lands bytes at "<path>.tmp" with O_NOFOLLOW; a symlink there is
    # a same-EUID redirect attempt and must be refused rather than followed.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.symlink_to(tmp_path / "attacker-target")
    with pytest.raises(OSError):
        Supervisor._write_engine_cloud_key_file(path, KEY)
