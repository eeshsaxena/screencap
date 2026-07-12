"""U2 — ``screencap e2ee`` CLI verb group (E2EE for shared copies, SCR-220).

Pins the enable-ordering safety rule (key first, flag second — R2: a key
failure must leave the flag untouched, never half-configured), the explicit
``false`` written by ``disable`` (an *unset* value would be silently
re-enrolled by the Stage 3 default flip — U9's precedence guarantee depends
on the literal), the read-only nature of ``status``, and the daemon-freshness
seam: the supervisor's key-staging read re-loads config.toml so a toggle
flipped between recordings reaches a long-running daemon without restart.

JSON shapes are asserted key-exact here; the Swift toggle (U4) parses them.

Mirrors the ``settings intelligence`` test shape (isolated config.toml,
CliRunner invoke) and the ``test_cloud_crypto`` fake-keyring pattern.
"""

from __future__ import annotations

import asyncio
import json
import types

import keyring
import keyring.errors
import pytest
import tomllib
from click.testing import CliRunner

from screencap import cloud_crypto as cc
from screencap.cli import cli

pytestmark = pytest.mark.privacy


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    import screencap.config

    cfg_path = tmp_path / "config.toml"
    monkeypatch.setattr(screencap.config, "_CONFIG_PATH", cfg_path)
    monkeypatch.setattr(screencap.config, "_DEFAULT_BASE", tmp_path)
    monkeypatch.setattr(screencap.config, "_config_cache", None)
    # The flag getter honors the env var first — clear it so tests read what
    # the CLI persisted, not an ambient override.
    monkeypatch.delenv("SCREENCAP_CLOUD_E2EE", raising=False)
    yield


@pytest.fixture
def fake_keyring(monkeypatch):
    """In-memory keyring; ``sets`` counts mints so no-second-mint is pinned.

    The shared access group is forced un-entitled so the KEK resolves to this
    in-memory legacy home and the test never touches the real Security
    framework on an entitled machine (mirrors tests/test_cloud_crypto.py).
    """
    from screencap import keychain_group as kg

    def _unentitled(*_a, **_k):
        raise kg.MissingEntitlement(kg.errSecMissingEntitlement, "test")

    monkeypatch.setattr(kg, "load", _unentitled)
    monkeypatch.setattr(kg, "store", _unentitled)

    store: dict[tuple[str, str], str] = {}
    sets: list[tuple[str, str]] = []

    def _set(s, a, v):
        sets.append((s, a))
        store[(s, a)] = v

    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    monkeypatch.setattr(keyring, "set_password", _set)
    return types.SimpleNamespace(store=store, sets=sets)


def _read_cfg() -> dict:
    from screencap.config import _CONFIG_PATH

    if not _CONFIG_PATH.exists():
        return {}
    return tomllib.loads(_CONFIG_PATH.read_text())


def _invoke(*args):
    return CliRunner().invoke(cli, ["e2ee", *args, "--json"], catch_exceptions=False)


def _payload(result) -> dict:
    return json.loads(result.output.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# enable: key first, flag second (R2)
# --------------------------------------------------------------------------


def test_enable_key_failure_leaves_flag_untouched(monkeypatch):
    """AE2 (partial): Keychain failure → non-zero exit, machine-readable
    error, and the config flag is never written (not even ``false``)."""
    from screencap.config import _CONFIG_PATH

    # Pre-existing config the failed enable must not disturb.
    _CONFIG_PATH.write_text("audio_default = true\n")

    def boom(_s, _a):
        raise keyring.errors.KeyringError("keychain locked")

    monkeypatch.setattr(keyring, "get_password", boom)

    result = _invoke("enable")
    assert result.exit_code != 0
    payload = _payload(result)
    assert set(payload) == {"error", "enabled", "key_present"}
    assert payload["enabled"] is False
    assert payload["key_present"] is False
    assert payload["error"]

    cfg = _read_cfg()
    assert "cloud_e2ee_enabled" not in cfg  # flag untouched, not merely off
    assert cfg["audio_default"] is True  # rest of the file undisturbed


def test_enable_is_idempotent_same_key_id(fake_keyring):
    first = _invoke("enable")
    second = _invoke("enable")
    assert first.exit_code == 0 and second.exit_code == 0
    p1, p2 = _payload(first), _payload(second)
    assert p1 == p2
    assert p1["enabled"] is True and p1["key_present"] is True
    assert p1["key_id"] == p2["key_id"]
    assert len(fake_keyring.sets) == 1  # key minted exactly once
    assert _read_cfg()["cloud_e2ee_enabled"] is True


# --------------------------------------------------------------------------
# disable: explicit false, key kept
# --------------------------------------------------------------------------


def test_disable_writes_literal_false_and_reenable_reuses_key(fake_keyring):
    from screencap.config import _CONFIG_PATH

    first = _invoke("enable")
    kid = _payload(first)["key_id"]

    result = _invoke("disable")
    assert result.exit_code == 0
    payload = _payload(result)
    assert payload == {"enabled": False, "key_present": True, "key_id": kid}

    # The literal ``false`` must be in the file — an unset key would be
    # silently re-enrolled by a future default flip (U9 precedence).
    raw = _CONFIG_PATH.read_text()
    assert "cloud_e2ee_enabled = false" in raw
    assert fake_keyring.store  # key kept

    reenabled = _invoke("enable")
    assert _payload(reenabled)["key_id"] == kid  # reused, not re-minted
    assert len(fake_keyring.sets) == 1  # still exactly one mint


# --------------------------------------------------------------------------
# status: reports, never creates
# --------------------------------------------------------------------------


def test_status_reflects_all_three_states(fake_keyring):
    # off / no key — and status must not mint one.
    payload = _payload(_invoke("status"))
    assert payload == {"enabled": False, "key_present": False, "key_id": None}
    assert not fake_keyring.store  # read-only: no key created
    assert not fake_keyring.sets

    # on / key.
    kid = _payload(_invoke("enable"))["key_id"]
    payload = _payload(_invoke("status"))
    assert payload == {"enabled": True, "key_present": True, "key_id": kid}

    # off / key (disable keeps the key).
    _invoke("disable")
    payload = _payload(_invoke("status"))
    assert payload == {"enabled": False, "key_present": True, "key_id": kid}


# --------------------------------------------------------------------------
# JSON shapes are stable — the Swift toggle (U4) parses these exact keys
# --------------------------------------------------------------------------


def test_json_shapes_stable(fake_keyring):
    assert set(_payload(_invoke("enable"))) == {"enabled", "key_present", "key_id"}
    assert set(_payload(_invoke("status"))) == {"enabled", "key_present", "key_id"}
    assert set(_payload(_invoke("disable"))) == {"enabled", "key_present", "key_id"}
    assert isinstance(_payload(_invoke("status"))["key_id"], str)  # hex string


# --------------------------------------------------------------------------
# Daemon freshness: the staging read sees a flag flipped after daemon start
# --------------------------------------------------------------------------


def test_supervisor_staging_read_picks_up_flag_flip(tmp_path, fake_keyring):
    """The config cache is per-process with no cross-process invalidation, so
    the supervisor must re-read config.toml at every staging decision — a
    toggle flipped between recordings takes effect without daemon restart."""
    from screencap import config
    from screencap.daemon.supervisor import Supervisor

    # Key exists (created by a foreground `e2ee enable` on another process).
    key = cc.get_or_create_cloud_kek()

    # Daemon-process cache primed while the flag was off...
    config._CONFIG_PATH.write_text("")
    assert config.get_cloud_e2ee_enabled() is False
    # ...then another process (the CLI) flips it on. No in-process invalidation.
    config._CONFIG_PATH.write_text("cloud_e2ee_enabled = true\n")
    assert config._config_cache is not None  # stale-cache precondition holds

    sup = Supervisor.__new__(Supervisor)
    sup._engine_cloud_key_file = None
    request = types.SimpleNamespace(cloud_intent=True)
    capture_dir = tmp_path / "recordings" / "rec-001"
    capture_dir.mkdir(parents=True)

    env = asyncio.run(Supervisor._stage_engine_cloud_key(sup, request, capture_dir))

    assert env is not None, "staging read used the stale cached flag"
    key_file = env[cc.ENGINE_CLOUD_KEY_FILE_ENV]
    import base64

    assert base64.b64decode(open(key_file, "rb").read().strip()) == key
