"""U1 (SCR-220) — cloud-KEK custody: shared access group first (KTD-1) with the
legacy-``keyring`` fallback for un-entitled binaries, and the one-time
write-verify-then-delete legacy migration (KTD-2).

Security-critical: unlike the re-mintable auth token, a lost or forked KEK is
permanently undecryptable ciphertext — so these tests pin that an existing
group item always wins (never overwritten), that the legacy item survives any
unverified copy, and that the migration only ever uses the non-interactive
legacy read (never prompts).
"""

from __future__ import annotations

import base64
import logging
import sys

import keyring
import pytest

from screencap import cloud_crypto as cc
from screencap import keychain_group as kg

pytestmark = pytest.mark.privacy

KEY_G = b"G" * 32  # a key living in the shared group
KEY_L = b"L" * 32  # a key living in the legacy keyring home


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode("ascii")


def _gkey() -> tuple[str, str, str]:
    return (cc.CLOUD_SERVICE, cc.CLOUD_KEK_ACCOUNT, cc.KEYCHAIN_ACCESS_GROUP)


def _lkey() -> tuple[str, str]:
    return (cc.CLOUD_SERVICE, cc.CLOUD_KEK_ACCOUNT)


class _FakeHomes:
    """Both KEK homes: the shared-group store and the legacy keyring/login item
    (`keyring` writes and the non-interactive legacy SecItem read see the same
    login-keychain item, so one dict backs both surfaces)."""

    def __init__(self) -> None:
        self.group: dict[tuple[str, str, str], str] = {}
        self.legacy: dict[tuple[str, str], str] = {}
        self.legacy_deletes = 0

    # -- keychain_group public surface (entitled path) --
    def load(self, service: str, account: str, access_group: str, *, synchronizable: bool = False):
        return self.group.get((service, account, access_group))

    def store(
        self, service: str, account: str, secret: str, access_group: str, *, synchronizable: bool = False
    ) -> None:
        self.group[(service, account, access_group)] = secret

    def load_legacy_noninteractive(self, service: str, account: str):
        return self.legacy.get((service, account))

    def delete_legacy_noninteractive(self, service: str, account: str) -> bool:
        self.legacy_deletes += 1
        self.legacy.pop((service, account), None)
        return True


@pytest.fixture
def homes(monkeypatch: pytest.MonkeyPatch) -> _FakeHomes:
    h = _FakeHomes()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(kg, "load", h.load)
    monkeypatch.setattr(kg, "store", h.store)
    monkeypatch.setattr(kg, "load_legacy_noninteractive", h.load_legacy_noninteractive)
    monkeypatch.setattr(kg, "delete_legacy_noninteractive", h.delete_legacy_noninteractive)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: h.legacy.get((s, a)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, a, v: h.legacy.__setitem__((s, a), v)
    )
    return h


@pytest.fixture
def unentitled(homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch) -> _FakeHomes:
    """The pip/pyenv/Debug fork: every group operation raises MissingEntitlement."""

    def _raise(*_a: object, **_k: object):
        raise kg.MissingEntitlement(kg.errSecMissingEntitlement, "test")

    monkeypatch.setattr(kg, "load", _raise)
    monkeypatch.setattr(kg, "store", _raise)
    return homes


# --------------------------------------------------------------------------
# Group-first read (KTD-1)
# --------------------------------------------------------------------------


def test_group_first_read_wins_when_both_homes_hold_a_key(homes: _FakeHomes) -> None:
    homes.group[_gkey()] = _b64(KEY_G)
    homes.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_G
    # No migration ran: the legacy item is untouched.
    assert homes.legacy[_lkey()] == _b64(KEY_L)
    assert homes.legacy_deletes == 0


def test_group_absent_everywhere_returns_none(homes: _FakeHomes) -> None:
    assert cc.get_cloud_kek() is None
    assert homes.group == {}


def test_create_lands_in_group_not_keyring(homes: _FakeHomes) -> None:
    created = cc.get_or_create_cloud_kek()
    assert homes.group[_gkey()] == _b64(created)
    assert homes.legacy == {}
    assert cc.get_cloud_kek() == created  # not re-minted


# --------------------------------------------------------------------------
# MissingEntitlement → legacy keyring fallback for read AND write
# --------------------------------------------------------------------------


def test_missing_entitlement_falls_back_to_keyring_read(unentitled: _FakeHomes) -> None:
    unentitled.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_L


def test_missing_entitlement_falls_back_to_keyring_write(unentitled: _FakeHomes) -> None:
    created = cc.get_or_create_cloud_kek()
    assert unentitled.legacy[_lkey()] == _b64(created)
    assert unentitled.group == {}


# --------------------------------------------------------------------------
# One-time legacy migration (KTD-2)
# --------------------------------------------------------------------------


def test_migration_copies_verifies_then_deletes_legacy(homes: _FakeHomes) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    assert cc.get_cloud_kek() == KEY_L
    assert homes.group[_gkey()] == _b64(KEY_L)  # copied into the group
    assert _lkey() not in homes.legacy  # legacy removed after verified readback
    assert homes.legacy_deletes == 1


def test_migration_aborts_with_legacy_intact_when_readback_fails(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    # The group write is silently lost → the readback cannot verify the copy.
    monkeypatch.setattr(kg, "store", lambda *a, **k: None)
    assert cc.get_cloud_kek() == KEY_L  # fail-open: legacy key still serves
    assert homes.legacy[_lkey()] == _b64(KEY_L)  # legacy NOT deleted
    assert homes.legacy_deletes == 0


def test_migration_never_prompts_only_noninteractive_legacy_read(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)

    def _interactive(*_a: object) -> str:
        raise AssertionError("interactive legacy keyring read during migration")

    monkeypatch.setattr(keyring, "get_password", _interactive)
    # Succeeds via load_legacy_noninteractive alone — the promptable path is dead.
    assert cc.get_cloud_kek() == KEY_L
    assert homes.group[_gkey()] == _b64(KEY_L)


# --------------------------------------------------------------------------
# Prefer-existing: an already-present group KEK is never overwritten (KTD-2)
# --------------------------------------------------------------------------


def _racy_load(homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch) -> list:
    """Group empty on the first read, concurrently populated with KEY_G after;
    records every group store attempt (there must be none)."""
    calls = {"n": 0}

    def load(service: str, account: str, access_group: str, **_k: object):
        calls["n"] += 1
        if calls["n"] > 1:
            homes.group.setdefault((service, account, access_group), _b64(KEY_G))
        return homes.group.get((service, account, access_group))

    stores: list = []
    monkeypatch.setattr(kg, "load", load)
    monkeypatch.setattr(kg, "store", lambda *a, **k: stores.append(a))
    return stores


def test_concurrent_group_key_wins_over_fresh_mint(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _racy_load(homes, monkeypatch)
    assert cc.get_or_create_cloud_kek() == KEY_G  # existing item's bytes win
    assert stores == []  # never overwritten
    assert homes.group[_gkey()] == _b64(KEY_G)


def test_migration_never_overwrites_a_concurrent_group_key(
    homes: _FakeHomes, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    stores = _racy_load(homes, monkeypatch)
    assert cc.get_cloud_kek() == KEY_G  # the group key wins over the legacy one
    assert stores == []
    # The legacy item stays — its ciphertext may still need it.
    assert homes.legacy[_lkey()] == _b64(KEY_L)
    assert homes.legacy_deletes == 0


# --------------------------------------------------------------------------
# key_id home-independence + no key material in logs
# --------------------------------------------------------------------------


def test_cloud_key_id_stable_across_homes(homes: _FakeHomes) -> None:
    homes.group[_gkey()] = _b64(KEY_G)
    from_group = cc.get_cloud_kek()
    homes.group.clear()
    homes.legacy[_lkey()] = _b64(KEY_G)  # same key, legacy home (migrates on read)
    from_legacy = cc.get_cloud_kek()
    assert from_group == from_legacy
    assert cc.cloud_key_id(from_group) == cc.cloud_key_id(from_legacy)


def test_migration_logs_never_contain_key_material(
    homes: _FakeHomes, caplog: pytest.LogCaptureFixture
) -> None:
    homes.legacy[_lkey()] = _b64(KEY_L)
    with caplog.at_level(logging.DEBUG):
        assert cc.get_cloud_kek() == KEY_L
    assert _b64(KEY_L) not in caplog.text
    assert repr(KEY_L) not in caplog.text
